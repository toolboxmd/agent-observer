"""OpenCode native adapter: opencode.db sessions into the ledger.

Reads the OpenCode SQLite database (`~/.local/share/opencode/opencode.db`)
read-only through a `mode=ro` URI so WAL content stays visible. One observer
`sources` row per native session (`<db path>#<session id>`) fingerprints the
imported snapshot; an unchanged fingerprint skips the session.

Counter rule: each assistant message with `time.completed` set is one
response. Input excludes cache reads and writes; reasoning is separate, so
total_tokens = input + output + reasoning + cache read + cache write. A
message whose counters are all zero carries unknown usage and stores no
response row, error or not.
"""

from __future__ import annotations

from functools import partial
import json
import os
import sqlite3
import time

from .. import db, native_response, privacy
from ..identity import SessionIdentity, skill_from_path
from ..ingest import fingerprint, insert_event, iso_ts, text_hash

HARNESS = "opencode"
SEMANTICS = "opencode:input_excludes_cache,reasoning_separate"
DEFAULT_ROOT = os.path.expanduser("~/.local/share/opencode/opencode.db")
DB_FILENAME = "opencode.db"

CAPABILITIES = [
    ("model_usage", True, "assistant message usage once completed; input excludes cache, reasoning separate; all-zero counters mean unknown"),
    ("tool_calls", True, "tool parts with callID join, name, argument fingerprint and target"),
    ("tool_results", True, "tool results joined on callID; completed maps to ok, error to error; pending and running have no result yet"),
    ("read_evidence", True, "read tool results with resolved path"),
    ("skill_file_reads", True, "reads under an installed Skill directory"),
    ("skill_invocation", True, "skill tool calls name the skill; skill dir observed as SKILL.md path"),
    ("file_change", True, "edit/write/patch tool calls and patch parts as file_change; patch parts keyed by part id with snapshot hash"),
    ("compaction", True, "compaction parts and session time_compacting as compaction boundaries"),
    ("lifecycle_task", True, "message errors carry the status code; MessageAbortedError records an interruption"),
    ("human_input", True, "user text parts as genuine/synthetic user messages; child sessions and synthetic flags told apart"),
    ("instruction_identity", True, "AgentsMD direction block from user text; versioned plugin paths read"),
    ("subagents", True, "child sessions as subagent sessions of their parent"),
]

# Privacy (ledger privacy spec, planner ruling 2026-09-23; refines
# docs/contracts.md rules 6 and 7). Fail closed: when in doubt, store less.
# The single implementation of rules 1 to 6 lives in
# agent_observer/privacy.py; this adapter keeps no private copies.
# submissions.text_excerpt is empty unless the row is a genuine human
# submission from the main session, in which case privacy.submission_excerpt
# keeps the human text only up to the first tag-like marker with no tag
# parsing. Identity observation still sees the full text so
# instructions_sha256 / preferences_sha256 keep working.
#
# Compatibility alias: tests reference opencode.IMPORT_ERROR_CATEGORIES. It
# aliases privacy.ERROR_CATEGORIES and must never diverge into a copy.
IMPORT_ERROR_CATEGORIES = privacy.ERROR_CATEGORIES

# Part types that are known to carry no ledger event. Anything else is
# quarantined as unknown_part_type instead of being silently dropped.
_KNOWN_QUIET_PART_TYPES = frozenset(
    {"reasoning", "file", "step-start", "step-finish"})
_INCOMPLETE_TOOL_STATUSES = frozenset({"running", "pending"})


def _resolve_db(path: str | None) -> str:
    """A root/source may name the database file or its directory."""
    candidate = path or DEFAULT_ROOT
    candidate = os.path.expanduser(candidate)
    if os.path.isdir(candidate):
        return os.path.join(candidate, DB_FILENAME)
    return candidate


def discover(root: str | None = None) -> list[str]:
    path = _resolve_db(root)
    return [path] if os.path.exists(path) else []


def _native_columns(native: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = native.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    cols: set[str] = set()
    for row in rows:
        try:
            cols.add(row["name"])
        except (KeyError, TypeError, IndexError):
            try:
                cols.add(row[1])
            except (IndexError, TypeError):
                continue
    return cols


def _fetch_dicts(native: sqlite3.Connection, table: str, want: list[str],
                 where: str | None = None, args: tuple = (),
                 order: list[str] | None = None) -> list[dict]:
    cols = _native_columns(native, table)
    if not cols or "id" not in cols and table in ("session", "message", "part"):
        # Without columns (or without an id on a core table) there is
        # nothing projectable; let the caller quarantine the read.
        if not cols:
            raise sqlite3.Error(f"no such table: {table}")
    present = [c for c in want if c in cols]
    if not present:
        return []
    sql = f"SELECT {', '.join(present)} FROM {table}"
    if where:
        # Only filter when the filter column exists; otherwise return all.
        sql += f" WHERE {where}"
    order_cols = [c for c in (order or []) if c in cols]
    if order_cols:
        sql += " ORDER BY " + ", ".join(order_cols)
    rows = native.execute(sql, args).fetchall()
    out: list[dict] = []
    for row in rows:
        item = {c: row[c] for c in present}
        for col in want:
            if col not in item:
                item[col] = None
        out.append(item)
    return out


def _count_max(native: sqlite3.Connection, table: str,
               sess_id: str | None) -> tuple[int, object]:
    cols = _native_columns(native, table)
    if not cols or "session_id" not in cols:
        return 0, None
    try:
        if "time_updated" in cols:
            row = native.execute(
                f"SELECT COUNT(*) n, MAX(time_updated) m FROM {table}"
                " WHERE session_id=?", (sess_id,)).fetchone()
            return (row["n"] if row else 0,
                    row["m"] if row and "m" in row.keys() else None)
        row = native.execute(
            f"SELECT COUNT(*) n FROM {table} WHERE session_id=?",
            (sess_id,)).fetchone()
        return (row["n"] if row else 0), None
    except sqlite3.Error:
        return 0, None


def sync(con: sqlite3.Connection, root: str | None = None, full: bool = False,
         source: str | None = None) -> dict:
    db_path = _resolve_db(source or root)
    totals: dict = {"harness": HARNESS, "sources": 0, "unchanged": 0,
                    "responses_inserted": 0, "events_inserted": 0,
                    "user_messages_inserted": 0, "malformed": 0, "failed": []}
    if not os.path.exists(db_path):
        totals["failed"].append({"path": db_path, "error": "not found"})
        return totals
    abs_path = os.path.abspath(db_path)
    try:
        native = sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        totals["failed"].append({"path": abs_path, "error": str(exc)})
        return totals
    native.row_factory = sqlite3.Row
    try:
        try:
            rows = _fetch_dicts(
                native, "session",
                ["id", "parent_id", "directory", "title", "version",
                 "time_created", "time_updated", "time_compacting"],
                order=["id"])
        except sqlite3.Error as exc:
            totals["failed"].append({"path": abs_path, "error": str(exc)})
            return totals
        for row in rows:
            con.execute("SAVEPOINT opencode_session")
            try:
                stats = import_session(con, native, abs_path, dict(row),
                                       full=full)
            except (OSError, sqlite3.DatabaseError) as exc:
                con.execute("ROLLBACK TO opencode_session")
                con.execute("RELEASE opencode_session")
                totals["failed"].append(
                    {"path": f"{abs_path}#{row.get('id')}", "error": str(exc)})
                continue
            totals["sources"] += 1
            totals["unchanged"] += 1 if stats.get("unchanged") else 0
            for key in ("responses_inserted", "events_inserted",
                        "user_messages_inserted", "malformed"):
                totals[key] += stats.get(key, 0)
    finally:
        native.close()
    return totals


def _record_line(raw, data=None) -> str:
    """Original JSON text for privacy.line_excerpt, or a serialization.

    Rule 5 keeps only the sorted top-level key names of the native record;
    privacy.line_excerpt yields an empty excerpt for anything else (bad
    JSON, lists, scalars, missing records), so no values ever persist.
    """
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(data, dict):
        try:
            return json.dumps(data, sort_keys=True)
        except (TypeError, ValueError):
            return ""
    return ""


def _oops(con: sqlite3.Connection, stats: dict, src_path: str,
          ordinal, category: str, line: str = "") -> None:
    """Quarantine one record: closed category plus structure-only excerpt.

    Rules 4 and 5 via agent_observer/privacy.py: error holds exactly one
    closed category (anything else maps to the fallback) and the excerpt
    holds only sorted top-level key names. The same record re-read under
    the same version never adds a duplicate row: rows match on source
    path, ordinal (NULL-safe), category and structural excerpt.
    """
    stats["malformed"] += 1
    safe = privacy.error_category(category)
    excerpt = privacy.line_excerpt(line)
    exists = con.execute(
        "SELECT 1 FROM import_errors WHERE harness=? AND source_path=?"
        " AND ordinal_num IS ? AND error=? AND line_excerpt=?",
        (HARNESS, src_path, ordinal, safe, excerpt)).fetchone()
    if exists is not None:
        return
    con.execute(
        "INSERT INTO import_errors(harness, source_path, ordinal_num, error,"
        " line_excerpt, created_at) VALUES(?,?,?,?,?,?)",
        (HARNESS, src_path, ordinal, safe, excerpt, db.now()))


def _target(tool_input: dict) -> str | None:
    # Rule 6/7: only non-empty native strings (paths, commands) become
    # targets, bounded through privacy.filter_target. Dicts, lists,
    # numbers, booleans and other objects are dropped, never stringified.
    if not isinstance(tool_input, dict):
        return None
    for key in ("filePath", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return privacy.filter_target(value)
    command = tool_input.get("command")
    if isinstance(command, str) and command:
        return privacy.filter_target(command)
    return None


def _duration_ms(state: dict):
    timing = state.get("time") if isinstance(state, dict) else None
    if not isinstance(timing, dict):
        return None
    start, end = timing.get("start"), timing.get("end")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        try:
            return int(end) - int(start)
        except (TypeError, ValueError):
            return None
    return None


def _output_size(output) -> int | None:
    if output is None:
        return None
    if isinstance(output, str):
        return len(output)
    try:
        return len(json.dumps(output, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return len(str(output))


def _int_or_none(value):
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else None


_MEASUREMENT_FIELDS = ("model", "provider", "effort", "input_tokens", "output_tokens",
                       "cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens",
                       "total_tokens", "cost_usd")


def _prepare_observations(con, session_key, source_id):
    # A snapshot replaces only this store's observations. Keep the projection
    # fields needed to restore a surviving copy when another store changes.
    con.execute("""CREATE TABLE IF NOT EXISTS opencode_response_observations (
        session_key TEXT NOT NULL, response_id TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES sources(id),
        parent TEXT, measurement TEXT NOT NULL, ts REAL, ordinal_num INTEGER,
        PRIMARY KEY(session_key,response_id,source_id)) WITHOUT ROWID""")
    con.execute("CREATE INDEX IF NOT EXISTS opencode_observation_source ON opencode_response_observations(source_id)")
    # Preserve the one payload a pre-upgrade ledger actually retained before
    # another copy can replace its projection. Only its own edge accompanies it.
    for row in con.execute(
            "SELECT r.* FROM responses r JOIN sources s ON s.id=r.source_id WHERE r.session_key=? "
            "AND r.source_id<>? AND s.sha256 NOT LIKE 'oc3:%' AND NOT EXISTS "
            "(SELECT 1 FROM opencode_response_observations o WHERE o.session_key=r.session_key "
            "AND o.response_id=r.response_id AND o.source_id=r.source_id)", (session_key, source_id)):
        parent = con.execute(
            "SELECT input_id FROM native_response_inputs WHERE session_key=? AND response_id=? AND source_id=?",
            (session_key, row["response_id"], row["source_id"])).fetchone()
        measurement = {name: row[name] for name in _MEASUREMENT_FIELDS}
        _observe_response(con, session_key, row["response_id"], row["source_id"], parent[0] if parent else None,
                          measurement, row["ts"], row["ordinal_num"])
    con.execute("DELETE FROM opencode_response_observations WHERE source_id=?", (source_id,))


def _project_observations(con, stats, session_key, session_id):
    # The path selects a stable locator only. Membership and measurements
    # are usable together only where the copies agree (response_witnesses).
    groups = {}
    for row in con.execute(
            "SELECT o.*,s.path FROM opencode_response_observations o JOIN sources s ON s.id=o.source_id "
            "WHERE session_key=? ORDER BY response_id,s.path", (session_key,)):
        groups.setdefault(row["response_id"], []).append(row)
    for rid, copies in groups.items():
        copy = copies[0]
        data = json.loads(copy["measurement"])
        for other in copies[1:]:
            for field, value in json.loads(other["measurement"]).items():
                if data[field] != value:
                    data[field] = None
        _project_response(con, stats, rid, session_key, session_id, copy, data)
    # Previously observed ids without a surviving copy retain their historical
    # ledger row. Their input witness has been removed in this transaction.


def response_witnesses(con, session_key):
    """Resolve the parent and the measured payload as a single source observation."""
    witnesses = {}
    conflicts = set()
    payload_sources = {}
    observations = {}
    if db._has_table(con, "opencode_response_observations"):
        for rid, source, parent, measurement in con.execute(
                "SELECT o.response_id,o.source_id,o.parent,o.measurement FROM opencode_response_observations o "
                "JOIN responses r USING(session_key,response_id) WHERE o.session_key=? AND r.is_overlap=0", (session_key,)):
            payload_sources.setdefault(rid, set()).add(source)
            observations.setdefault(rid, set()).add((parent, measurement))
            if parent:
                witnesses.setdefault(rid, set()).add(parent)
        for rid, variants in observations.items():
            # Competing proved parents are returned together. A missing parent
            # or incompatible payload additionally invalidates the measurement.
            if len(variants) > 1 and (any(parent is None for parent, _ in variants)
                                      or len({payload for _, payload in variants}) > 1):
                conflicts.add(rid)

    # Old imports recorded edges but kept only the last copy's payload.
    # An edge from a copy whose measurement was lost cannot vouch for it.
    measured = dict(con.execute("SELECT response_id,source_id FROM responses WHERE session_key=?", (session_key,)))
    for rid, input_id, source, measured_source in native_response.stored_inputs(con, session_key):
        if source not in payload_sources.get(rid, {measured_source}):
            conflicts.add(rid)
        witnesses.setdefault(rid, set()).add(input_id)
    if db._has_table(con, "native_response_events"):
        for rid, source in con.execute(
                "SELECT DISTINCT e.response_id,e.source_id FROM native_response_events e "
                "JOIN sources s ON s.id=e.source_id WHERE e.session_key=? AND s.sha256 NOT LIKE 'oc3:%'",
                (session_key,)):
            if rid in measured and source not in payload_sources.get(rid, {measured[rid]}):
                conflicts.add(rid)
    return witnesses, conflicts


def _project_response(con, stats, rid, session_key, session_id, locator, measurement):
    fields = ("source_id", "ordinal_num", "ts", "semantics", *_MEASUREMENT_FIELDS)
    values = (locator["source_id"], locator["ordinal_num"], locator["ts"], SEMANTICS,
              *(measurement[k] for k in _MEASUREMENT_FIELDS))
    # Identity was validated before the source observation was accepted.
    columns = ",".join(fields)
    row = con.execute(f"SELECT {columns} FROM responses WHERE response_id=?", (rid,)).fetchone()
    if row and tuple(row) == values:
        stats["responses_duplicate"] += 1
        return
    if row:
        con.execute(f"UPDATE responses SET ({columns})=({','.join('?' for _ in fields)}) WHERE response_id=?",
                    (*values, rid))
    else:
        con.execute(f"INSERT INTO responses(response_id,harness,session_key,session_id,{columns}) "
                    f"VALUES({','.join('?' for _ in range(4 + len(fields)))})",
                    (rid, HARNESS, session_key, session_id, *values))
    stats["responses_inserted"] += 1


def _upsert_event(con: sqlite3.Connection, stats: dict, *, source_id: int,
                  session_key: str, family: str, native_id, ordinal=None,
                  ts=None, turn_id=None, name=None, target=None, status=None,
                  duration_ms=None, size_bytes=None, truncated=None,
                  fingerprint=None, detail=None, response_id=None) -> None:
    """Insert one event under its natural key; mutable rows update in place.

    Rule 6 via agent_observer/privacy.py for every protected field:
    native_id via filter_native_id (wrong types raise, quarantined as
    missing_id by the caller), name via filter_event_name, status via
    filter_event_status, targets via filter_target and detail via
    filter_detail, so free-text tool or skill names and skill titles never
    persist. The full-field comparison updates a changed row in place, so
    same-version mutable OpenCode behavior (pending tools completing,
    tool payloads finalizing) keeps working and a privacy-stale re-import
    replaces outdated names, statuses, targets and detail instead of
    leaving them stale.
    """
    native = privacy.filter_native_id(family, native_id)
    if not native:
        raise ValueError(f"{family} event missing native identity")
    if response_id:
        native_response.record_event(con, source_id, session_key, family, native, response_id)
    safe_name = privacy.filter_event_name(family, name)
    safe_status = privacy.filter_event_status(family, status)
    safe_target = privacy.filter_target(target, family) or None
    filtered = privacy.filter_detail(family, detail)
    try:
        detail_json = json.dumps(filtered, sort_keys=True) \
            if filtered else None
    except (TypeError, ValueError):
        detail_json = None
    existing = con.execute(
        "SELECT * FROM events WHERE session_key=? AND family=? AND native_id=?",
        (session_key, family, native)).fetchone()
    if existing is None:
        con.execute(
            "INSERT INTO events(source_id, session_key, ordinal_num, ts,"
            " family, native_id, turn_id, name, target, status, duration_ms,"
            " size_bytes, truncated, fingerprint, detail_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, session_key, ordinal, ts, family, native, turn_id,
             safe_name, safe_target, safe_status, duration_ms, size_bytes,
             truncated, fingerprint, detail_json))
        stats["events_inserted"] = stats.get("events_inserted", 0) + 1
        return
    new_fields = {
        "source_id": source_id, "ordinal_num": ordinal, "ts": ts,
        "turn_id": turn_id, "name": safe_name, "target": safe_target,
        "status": safe_status, "duration_ms": duration_ms,
        "size_bytes": size_bytes, "truncated": truncated,
        "fingerprint": fingerprint, "detail_json": detail_json,
    }
    changed = any(existing[col] != val for col, val in new_fields.items())
    if not changed:
        stats["events_duplicate"] = stats.get("events_duplicate", 0) + 1
        return
    con.execute(
        "UPDATE events SET source_id=?, ordinal_num=?, ts=?, turn_id=?,"
        " name=?, target=?, status=?, duration_ms=?, size_bytes=?,"
        " truncated=?, fingerprint=?, detail_json=? WHERE session_key=?"
        " AND family=? AND native_id=?",
        (source_id, ordinal, ts, turn_id, safe_name, safe_target,
         safe_status, duration_ms, size_bytes, truncated, fingerprint,
         detail_json, session_key, family, native))
    stats["events_inserted"] = stats.get("events_inserted", 0) + 1


def import_session(con: sqlite3.Connection, native: sqlite3.Connection,
                   abs_db_path: str, sess: dict,
                   full: bool = False) -> dict:
    started = time.monotonic()
    stats: dict = {"responses_inserted": 0, "responses_duplicate": 0,
                   "events_inserted": 0, "events_duplicate": 0,
                   "user_messages_inserted": 0, "malformed": 0}
    sess_id = sess.get("id")
    src_path = f"{abs_db_path}#{sess_id}"
    session_key = f"{HARNESS}:{sess_id}"
    msg_n, msg_m = _count_max(native, "message", sess_id)
    part_n, part_m = _count_max(native, "part", sess_id)
    fp = "oc3:" + fingerprint(sess.get("time_updated"), msg_m, part_m, msg_n, part_n)
    row = con.execute(
        "SELECT * FROM sources WHERE harness=? AND path=?",
        (HARNESS, src_path)).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES(?,?,?,?)", (HARNESS, src_path, "", db.now()))
        row = con.execute(
            "SELECT * FROM sources WHERE harness=? AND path=?",
            (HARNESS, src_path)).fetchone()
    stored_version = (row["privacy_version"]
                      if "privacy_version" in row.keys() else None)
    # Rule 3: a source imported under older privacy rules is fully
    # re-imported; existing rows are updated in place and that source's
    # import_errors are replaced instead of duplicated. The legacy
    # database-level path is cleared too: errors used to share it across
    # sessions, while new rows key to this source's own path.
    privacy_stale = stored_version != privacy.PRIVACY_VERSION
    if privacy_stale:
        con.execute(
            "DELETE FROM import_errors WHERE harness=? AND source_path=?",
            (HARNESS, src_path))
        con.execute(
            "DELETE FROM import_errors WHERE harness=? AND source_path=?",
            (HARNESS, abs_db_path))
    if row["sha256"] == fp and not full and not privacy_stale:
        stats["unchanged"] = True
        con.commit()
        return stats
    source_id = row["id"]
    identity = SessionIdentity()
    if privacy_stale:
        # Reconcile every row this source owns: clear sensitive fields
        # before reprocessing so a deleted, malformed or invalid native
        # record cannot leave an old excerpt, name, status, target or
        # detail behind. Valid records recompute/update these fields in
        # place below.
        con.execute(
            "UPDATE user_messages SET text_excerpt=?, text_hash=?, kind=?,"
            " is_genuine=? WHERE source_id=?",
            ("", text_hash(""), "synthetic", 0, source_id))
        con.execute(
            "UPDATE events SET name=NULL, target=NULL, status=NULL,"
            " detail_json=NULL WHERE source_id=?",
            (source_id,))
    complete = True
    try:
        messages = _fetch_dicts(
            native, "message",
            ["id", "session_id", "time_created", "time_updated", "data"],
            where="session_id=?", args=(sess_id,),
            order=["time_created", "id"])
    except sqlite3.Error:
        _oops(con, stats, src_path, None, "source_unreadable")
        messages = []
        complete = False
    try:
        parts = _fetch_dicts(
            native, "part",
            ["id", "message_id", "session_id", "time_created", "time_updated",
             "data"],
            where="session_id=?", args=(sess_id,),
            order=["time_created", "id"])
    except sqlite3.Error:
        _oops(con, stats, src_path, None, "source_unreadable")
        parts = []
        complete = False
    by_message: dict[str, list] = {}
    for part in parts:
        by_message.setdefault(part.get("message_id"), []).append(part)
    roles: dict[str, str | None] = {}
    parsed_messages: list[tuple] = []
    for index, msg in enumerate(messages):
        raw = msg.get("data")
        msg_id = msg.get("id")
        if not msg_id or raw is None:
            _oops(con, stats, src_path, index, "schema_error")
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            _oops(con, stats, src_path, index, "malformed_json",
                  _record_line(raw))
            continue
        if not isinstance(data, dict):
            _oops(con, stats, src_path, index, "schema_error",
                  _record_line(raw, data))
            continue
        parsed_messages.append((index, msg, data))
        roles[msg_id] = data.get("role")
    native_response.prepare(con, source_id, replace=True)
    native_response.prepare_events(con, source_id, replace=True)
    _prepare_observations(con, session_key, source_id)
    ordinal = 0
    for index, msg, data in parsed_messages:
        ordinal += 1
        _ingest_message(con, native, stats, identity, src_path, source_id,
                        session_key, sess, msg, data, index, ordinal,
                        by_message.get(msg.get("id"), []),
                        roles=roles, privacy_stale=privacy_stale)
    _project_observations(con, stats, session_key, sess_id)
    for part in parts:
        ordinal += 1
        _ingest_part(con, stats, identity, src_path, source_id,
                      session_key, roles, part, ordinal)
    if sess.get("time_compacting") is not None:
        insert_event(con, stats, source_id=source_id,
                     session_key=session_key, family="compaction",
                     native_id="time_compacting", ordinal=ordinal,
                     ts=iso_ts(sess.get("time_compacting")),
                     name="time_compacting", update=privacy_stale)
    fields = {"project_dir": sess.get("directory"),
              "client_version": sess.get("version"),
              "started_at": iso_ts(sess.get("time_created")),
              "ended_at": iso_ts(sess.get("time_updated")),
              **identity.fields(con)}
    if sess.get("parent_id"):
        fields["parent_session_key"] = f"{HARNESS}:{sess['parent_id']}"
        fields["role"] = "subagent"
    db.upsert_session(con, session_key, HARNESS, sess_id, source_id,
                      **fields)
    try:
        size_bytes = os.path.getsize(abs_db_path)
    except OSError:
        size_bytes = 0
    # Only a complete read advances the privacy version. An unreadable or
    # partially read source keeps its old version so the next sync retries.
    new_version = privacy.PRIVACY_VERSION if complete else stored_version
    con.execute(
        "UPDATE sources SET sha256=?, size_bytes=?, imported_at=?,"
        " import_ms=?, session_id=?, privacy_version=? WHERE id=?",
        (fp if complete else "", size_bytes, db.now(),
         int((time.monotonic() - started) * 1000), sess_id,
         new_version, source_id))
    con.commit()
    stats["unchanged"] = False
    return stats


def _ingest_message(con, native, stats, identity, src_path, source_id,
                    session_key, sess, msg, data, index, ordinal,
                    msg_parts, *, roles, privacy_stale: bool = False) -> None:
    msg_id = msg.get("id")
    timing = data.get("time") if isinstance(data.get("time"), dict) else {}
    completed = timing.get("completed")
    created = timing.get("created")
    ts = iso_ts(completed if completed is not None else
                (created if created is not None else msg.get("time_created")))
    error = data.get("error")
    if isinstance(error, dict):
        detail = error.get("data") if isinstance(error.get("data"), dict) \
            else {}
        code = detail.get("statusCode")
        # Rule 6: the lifecycle family keeps no detail, so error names,
        # messages and other free text never persist; the numeric status
        # survives in the status column. Native messages are mutable, so a
        # later cancellation updates the same lifecycle identity in place.
        _upsert_event(con, stats, source_id=source_id,
                     session_key=session_key, family="lifecycle",
                     native_id=msg_id, ordinal=ordinal, ts=ts,
                     response_id=_assistant_id(roles, msg_id),
                     name="turn_aborted" if error.get("name") == "MessageAbortedError" else "error",
                     status="interrupted" if error.get("name") == "MessageAbortedError" else
                     (str(code) if isinstance(code, int) and not isinstance(code, bool) else None),
                     detail=None)
    role = data.get("role")
    if role == "assistant":
        _ingest_response(con, stats, src_path, source_id, session_key,
                         sess, msg, data, index, ts, ordinal, roles)
    elif role == "user":
        _ingest_submission(con, stats, identity, src_path, source_id,
                           session_key, sess, msg, data, index, ts,
                           msg_parts, ordinal)
    else:
        _oops(con, stats, src_path, ordinal, "unknown_record",
              _record_line(msg.get("data"), data))


def _ingest_response(con, stats, src_path, source_id, session_key, sess,
                     msg, data, index, ts, ordinal, roles) -> None:
    timing = data.get("time") if isinstance(data.get("time"), dict) else {}
    if timing.get("completed") is None:
        return
    raw_tokens = data.get("tokens")
    if raw_tokens is None:
        tokens: dict = {}
    elif isinstance(raw_tokens, dict):
        tokens = raw_tokens
    else:
        # A present non-object tokens block is malformed usage, never
        # silently unknown; quarantine before any response insert.
        _oops(con, stats, src_path, ordinal, "malformed_usage",
              _record_line(msg.get("data"), data))
        return
    raw_cache = tokens.get("cache")
    if raw_cache is None:
        cache: dict = {}
    elif isinstance(raw_cache, dict):
        cache = raw_cache
    else:
        _oops(con, stats, src_path, ordinal, "malformed_usage",
              _record_line(msg.get("data"), data))
        return
    # A present counter must be a non-boolean integer; only a missing
    # key stays unknown and maps to NULL. An explicitly present null,
    # bool, string, float, list or other non-integer quarantines the
    # record before the all-zero check, the total or any response
    # insert, and the import continues.
    values: dict = {}
    for field, container, key in (
            ("input_tokens", tokens, "input"),
            ("output_tokens", tokens, "output"),
            ("reasoning_output_tokens", tokens, "reasoning"),
            ("cached_input_tokens", cache, "read"),
            ("cache_write_input_tokens", cache, "write")):
        if key not in container:
            values[field] = None
            continue
        raw = container[key]
        if not isinstance(raw, int) or isinstance(raw, bool):
            _oops(con, stats, src_path, ordinal, "malformed_usage",
                  _record_line(msg.get("data"), data))
            return
        values[field] = raw
    if all(v == 0 for v in values.values()):
        return
    total = None
    if all(v is not None for v in values.values()):
        total = sum(values.values())
    cost = data.get("cost")
    cost_usd = float(cost) if isinstance(cost, (int, float)) \
        and not isinstance(cost, bool) else None
    parent = native_response.identifier(data.get("parentID"))
    if roles.get(parent) != "user" or not native_response.identifier(msg["id"]):
        parent = None
    rid = f"{HARNESS}:{msg['id']}"
    existing = con.execute("SELECT session_key,session_id FROM responses WHERE response_id=?", (rid,)).fetchone()
    if existing and tuple(existing) != (session_key, sess.get("id")):
        _oops(con, stats, src_path, ordinal, "usage_conflict", _record_line(msg.get("data"), data))
        stats["responses_duplicate"] += 1
        return
    if any(data.get(k) is not None and not isinstance(data[k], str) for k in ("modelID", "providerID", "variant")):
        _oops(con, stats, src_path, ordinal, "malformed_usage", _record_line(msg.get("data"), data))
        return
    measurement = dict(values, total_tokens=total, model=data.get("modelID"), provider=data.get("providerID"),
                       effort=data.get("variant"), cost_usd=cost_usd)
    _observe_response(con, session_key, rid, source_id, parent, measurement, ts, index)


def _observe_response(con, session_key, rid, source_id, parent, measurement, ts, index):
    con.execute("INSERT INTO opencode_response_observations"
                "(session_key,response_id,source_id,parent,measurement,ts,ordinal_num) VALUES(?,?,?,?,?,?,?)",
                (session_key, rid, source_id, parent, json.dumps(measurement, sort_keys=True), ts, index))


def _ingest_submission(con, stats, identity, src_path, source_id,
                       session_key, sess, msg, data, index, ts,
                       msg_parts, ordinal) -> None:
    texts: list[str] = []
    flags: list[bool] = []
    for part in msg_parts:
        raw = part.get("data") if isinstance(part, dict) else None
        if raw is None:
            _oops(con, stats, src_path, ordinal, "schema_error")
            continue
        try:
            pdata = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            _oops(con, stats, src_path, ordinal, "malformed_json",
                  _record_line(raw))
            continue
        if not isinstance(pdata, dict):
            _oops(con, stats, src_path, ordinal, "schema_error",
                  _record_line(raw, pdata))
            continue
        if pdata.get("type") != "text":
            continue
        text = pdata.get("text")
        if not isinstance(text, str) or not text:
            _oops(con, stats, src_path, ordinal, "malformed_usage",
                  _record_line(raw, pdata))
            continue
        texts.append(text)
        # Provenance is a native JSON boolean only: missing keeps the
        # ordinary non-synthetic default, true means synthetic, false
        # means non-synthetic, and any present non-boolean value (for
        # example the string "false") fails closed as synthetic so it
        # never enters human_texts or a genuine excerpt.
        if "synthetic" not in pdata:
            flags.append(False)
        elif pdata["synthetic"] is True:
            flags.append(True)
        elif pdata["synthetic"] is False:
            flags.append(False)
        else:
            flags.append(True)
        # Identity sees the full valid text, including the direction block.
        # The full text stays transient: only the rule-1 excerpt persists.
        identity.observe_text(text)
    native_id = f"{HARNESS}:{msg.get('id')}"
    is_child = bool(sess.get("parent_id"))
    if not texts:
        kind = "synthetic" if is_child else "unknown"
        # No valid text remains: never leave a stale excerpt behind. Clear
        # any existing row in place with an empty excerpt and matching
        # non-genuine classification instead of returning early.
        existing = con.execute(
            "SELECT kind, text_hash, text_excerpt, is_genuine FROM user_messages"
            " WHERE native_id=?", (native_id,)).fetchone()
        if existing is None:
            # A native user message can contain only files or images. Keep
            # its identity even though no text proves an actor or excerpt.
            con.execute(
                "INSERT INTO user_messages(native_id,source_id,session_key,"
                "ordinal_num,ts,kind,text_hash,text_excerpt,is_genuine)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (native_id, source_id, session_key, index, ts,
                 kind, text_hash(""), "", 0))
            stats["user_messages_inserted"] += 1
            return
        digest = text_hash("")
        if (existing["kind"] != kind or existing["text_hash"] != digest
                or (existing["text_excerpt"] or "") != ""
                or existing["is_genuine"] != 0):
            con.execute(
                "UPDATE user_messages SET source_id=?, session_key=?,"
                " ordinal_num=?, ts=?, kind=?, text_hash=?, text_excerpt=?,"
                " is_genuine=? WHERE native_id=?",
                (source_id, session_key, index, ts, kind, digest, "",
                 0, native_id))
            stats["user_messages_inserted"] += 1
        return
    if is_child:
        kind = "synthetic"
    elif flags and all(flags):
        kind = "synthetic"
    else:
        kind = "genuine"
    # Rule 1 via agent_observer/privacy.py: only a genuine main-session
    # human submission keeps an excerpt, truncated at the first tag-like
    # marker with no tag parsing (a quoted '>' still ends the excerpt at
    # the earlier '<'). Child sessions are sub-agent turns: their prompts
    # are agent-generated, so no child text ever enters the human excerpt.
    # The row/kind semantics and identity observation above stay intact.
    human_texts = [t for t, flag in zip(texts, flags) if not flag]
    excerpt = privacy.submission_excerpt(
        "".join(human_texts), is_genuine=kind == "genuine",
        is_main_session=not is_child)
    digest = text_hash(excerpt)
    is_genuine = 1 if kind == "genuine" else 0
    existing = con.execute(
        "SELECT kind, text_hash, text_excerpt, is_genuine FROM user_messages"
        " WHERE native_id=?", (native_id,)).fetchone()
    if existing is None:
        con.execute(
            "INSERT INTO user_messages(native_id, source_id, session_key,"
            " ordinal_num, ts, kind, text_hash, text_excerpt, is_genuine)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (native_id, source_id, session_key, index, ts,
             kind, digest, excerpt, is_genuine))
        stats["user_messages_inserted"] += 1
        return
    if (existing["kind"] != kind or existing["text_hash"] != digest
            or (existing["text_excerpt"] or "") != excerpt
            or existing["is_genuine"] != is_genuine):
        con.execute(
            "UPDATE user_messages SET source_id=?, session_key=?,"
            " ordinal_num=?, ts=?, kind=?, text_hash=?, text_excerpt=?,"
            " is_genuine=? WHERE native_id=?",
            (source_id, session_key, index, ts, kind, digest, excerpt,
             is_genuine, native_id))
        stats["user_messages_inserted"] += 1


def _assistant_id(roles, message_id):
    if roles.get(message_id) == "assistant" and native_response.identifier(message_id):
        return f"{HARNESS}:{message_id}"
    return None


def _ingest_part(con, stats, identity, src_path, source_id, session_key,
                  roles, part, ordinal) -> None:
    raw = part.get("data") if isinstance(part, dict) else None
    part_id = part.get("id") if isinstance(part, dict) else None
    if raw is None:
        _oops(con, stats, src_path, ordinal, "schema_error")
        return
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        _oops(con, stats, src_path, ordinal, "malformed_json",
              _record_line(raw))
        return
    if not isinstance(data, dict):
        _oops(con, stats, src_path, ordinal, "schema_error",
              _record_line(raw, data))
        return
    ptype = data.get("type")
    ts = iso_ts(part.get("time_created"))
    # Native identity must be a non-empty string: dicts, lists, numbers
    # and other objects are quarantined, never stringified into the ledger.
    if not isinstance(part_id, str) or not part_id.strip():
        _oops(con, stats, src_path, ordinal, "missing_id",
              _record_line(raw, data))
        return
    event = partial(_upsert_event, con, stats, source_id=source_id, session_key=session_key,
                    response_id=_assistant_id(roles, part.get("message_id")), ordinal=ordinal, ts=ts)
    if ptype == "text":
        if roles.get(part.get("message_id")) != "assistant":
            return  # User text is indexed once under its message id.
        text = data.get("text")
        if not isinstance(text, str):
            _oops(con, stats, src_path, ordinal, "schema_error", _record_line(raw, data))
            return
        # Empty text is a valid clear. Reconcile an existing row so search
        # drops an excerpt the native part no longer contains. A part that
        # was always empty still creates no event.
        native = privacy.filter_native_id("assistant_message", part_id)
        exists = bool(native) and con.execute(
            "SELECT 1 FROM events WHERE session_key=? AND family=? AND native_id=?",
            (session_key, "assistant_message", native)).fetchone()
        if text or exists:
            excerpt = privacy.assistant_excerpt(text)
            event(family="assistant_message", native_id=part_id,
                  name="assistant_message",
                  size_bytes=len(text), fingerprint=text_hash(text),
                  detail={"excerpt": excerpt} if excerpt else None)
    elif ptype == "tool":
        _ingest_tool(con, stats, identity, src_path, event, data, ordinal,
                     line=_record_line(raw, data))
    elif ptype == "patch":
        _ingest_patch(event, part, data)
    elif ptype == "compaction":
        event(family="compaction", native_id=part_id, name="compaction")
    elif isinstance(ptype, str) and ptype in _KNOWN_QUIET_PART_TYPES:
        # text, reasoning, step checkpoints and file data URLs carry no
        # ledger event: user text is handled per message, and file data,
        # reasoning bodies and step checkpoints stay out of the ledger.
        return
    elif not isinstance(ptype, str):
        # Non-string or unhashable JSON type values never reach a set
        # membership test (which would raise TypeError and abort the
        # import); they are quarantined under the fixed category.
        _oops(con, stats, src_path, ordinal, "schema_error",
              _record_line(raw, data))
    else:
        _oops(con, stats, src_path, ordinal, "unsupported_schema",
              _record_line(raw, data))


def _ingest_tool(con, stats, identity, src_path, event, data, ordinal,
                  line: str = "") -> None:
    # Native tool/call/skill names must be strings: dicts, lists,
    # booleans, numbers and other objects fall back to "unknown" and are
    # never stringified into names, targets or the skill detail.
    raw_tool = data.get("tool")
    tool = raw_tool if isinstance(raw_tool, str) and raw_tool else "unknown"
    call_id = data.get("callID")
    if not isinstance(call_id, str) or not call_id.strip():
        _oops(con, stats, src_path, ordinal, "missing_id", line)
        return
    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    status = state.get("status")
    tool_input = state.get("input") if isinstance(state.get("input"), dict) \
        else {}
    output = state.get("output")
    target = _target(tool_input)
    try:
        arg_fp = fingerprint(tool, json.dumps(tool_input, sort_keys=True,
                                              default=str)[:4000])
    except (TypeError, ValueError):
        arg_fp = fingerprint(tool)
    # The message identity belongs to the source witness. Tool detail stays empty.
    event(family="tool_call", native_id=call_id, name=tool[:200], target=target,
          fingerprint=arg_fp, detail=None)
    if status is None or (
            isinstance(status, str) and status in _INCOMPLETE_TOOL_STATUSES):
        # No terminal result yet; a later snapshot must add it.
        pass
    elif status in ("completed", "error"):
        result_status = "ok" if status == "completed" else "error"
        event(family="tool_result", native_id=call_id, name=tool[:200], target=target,
              status=result_status, duration_ms=_duration_ms(state),
              size_bytes=_output_size(output),
              detail=None)
    elif isinstance(status, str):
        _oops(con, stats, src_path, ordinal, "unknown_record", line)
    else:
        # A non-string or unhashable status never reaches a set
        # membership test (which would raise TypeError and abort the
        # import); it is quarantined under the fixed category.
        _oops(con, stats, src_path, ordinal, "schema_error", line)
    if tool == "read" and status == "completed" and target:
        identity.observe_path(target)
        raw_skill = skill_from_path(target)
        safe_skill = privacy.filter_target(raw_skill, family="skill_read") \
            if raw_skill else None
        looks_like_skill = target.endswith("SKILL.md") or "/skills/" in target
        family = "skill_read" if (safe_skill or looks_like_skill) else "read"
        if family == "skill_read":
            # Rule 6: skill_read target holds only the validated skill
            # identifier, never the installed path. The file path lives
            # only in detail skill_path.
            event(family=family, native_id=call_id, name=os.path.basename(target)[:200], target=safe_skill,
                  status="ok", size_bytes=_output_size(output),
                  fingerprint=fingerprint(target),
                  detail={"skill": raw_skill, "skill_path": target})
        else:
            event(family=family, native_id=call_id, name=os.path.basename(target)[:200], target=target,
                  status="ok", size_bytes=_output_size(output),
                  fingerprint=fingerprint(target),
                  detail=None)
    if tool == "skill":
        raw_name = tool_input.get("name")
        if isinstance(raw_name, str) and raw_name:
            # Rule 6/7: identifier-shaped skill names only. A free-text
            # title such as "skill title" must not survive merely because
            # filter_target accepts bounded strings, so the name is
            # validated with the event-name filter and stored as None when
            # unsafe. The same safe value feeds both name and target.
            safe_skill = privacy.filter_event_name(
                "skill_invoke", raw_name)
        else:
            safe_skill = privacy.filter_event_name(
                "skill_invoke", "unknown")
        metadata = state.get("metadata") if isinstance(
            state.get("metadata"), dict) else {}
        skill_dir = metadata.get("dir")
        skill_path = None
        if isinstance(skill_dir, str) and skill_dir:
            skill_path = os.path.join(skill_dir, "SKILL.md")
            identity.observe_path(skill_path)
        # Rule 6: skill_invoke target holds only the validated skill
        # identifier; the installed SKILL.md path lives only in detail
        # skill_path when the native invocation supplies a directory.
        event(family="skill_invoke", native_id=call_id, name=safe_skill, target=safe_skill,
              detail={"skill_path": skill_path} if skill_path else None)
    if tool in ("edit", "write", "patch") and target:
        event(family="file_change", native_id=call_id, name=tool[:200], target=target,
              fingerprint=fingerprint(tool, target),
              detail=None)


def _ingest_patch(event, part, data) -> None:
    digest = data.get("hash")
    files = data.get("files")
    file_list = files if isinstance(files, list) else []
    # Only a non-empty native string selects the file target; anything
    # else is dropped, never stringified into the ledger.
    first = file_list[0] if file_list else None
    target = privacy.filter_target(first) \
        if isinstance(first, str) and first else None
    # Every valid native path survives in detail so reread invalidation
    # and test-edit detection see each changed file; the shared writer
    # validates the list through privacy.filter_detail. The native hash
    # and invalid path values are omitted, never coerced.
    valid_paths = sorted({p for p in file_list
                          if isinstance(p, str) and p})
    part_id = part.get("id") if isinstance(part, dict) else None
    if not isinstance(part_id, str) or not part_id:
        # The caller guarantees a valid part id; fail closed otherwise.
        return
    # Rule 6: the file_change family keeps only path lists, so an
    # arbitrary native hash is omitted, never coerced. The fingerprint
    # column keeps only our own fixed-format generated digest.
    event(family="file_change", native_id=part_id,
          name="patch", target=target,
          fingerprint=fingerprint("patch", digest, file_list),
          detail={"paths": valid_paths} if valid_paths else None)
