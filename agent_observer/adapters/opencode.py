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

import json
import os
import sqlite3
import time

from .. import db, privacy
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
    ("lifecycle_task", True, "message errors as lifecycle error events carrying the status code"),
    ("human_input", True, "user text parts as genuine/synthetic submissions; child sessions and synthetic flags told apart"),
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
    {"text", "reasoning", "file", "step-start", "step-finish"})
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
                    "submissions_inserted": 0, "malformed": 0, "failed": []}
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
            try:
                stats = import_session(con, native, abs_path, dict(row),
                                       full=full)
            except (OSError, sqlite3.DatabaseError) as exc:
                totals["failed"].append(
                    {"path": f"{abs_path}#{row.get('id')}", "error": str(exc)})
                continue
            totals["sources"] += 1
            totals["unchanged"] += 1 if stats.get("unchanged") else 0
            for key in ("responses_inserted", "events_inserted",
                        "submissions_inserted", "malformed"):
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


def _upsert_response(con: sqlite3.Connection, stats: dict, src_path: str,
                     *, response_id: str, source_id, session_key, session_id,
                     ordinal, ts, model, provider, effort, values: dict,
                     total, semantics, cost_usd, line: str = "") -> None:
    existing = con.execute(
        "SELECT * FROM responses WHERE response_id=?",
        (response_id,)).fetchone()
    if existing is None:
        con.execute(
            "INSERT INTO responses(response_id, source_id, harness,"
            " session_key, session_id, ordinal_num, ts, model, provider, effort,"
            " input_tokens, cached_input_tokens, cache_write_input_tokens,"
            " output_tokens, reasoning_output_tokens, total_tokens, semantics,"
            " cost_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (response_id, source_id, HARNESS, session_key,
             session_id, ordinal, ts, model,
             provider, effort,
             values["input_tokens"], values["cached_input_tokens"],
             values["cache_write_input_tokens"], values["output_tokens"],
             values["reasoning_output_tokens"], total, semantics, cost_usd))
        stats["responses_inserted"] = stats.get("responses_inserted", 0) + 1
        return
    # Immutable identity: the same response id must stay in its session.
    if (existing["session_key"] != session_key
            or existing["session_id"] != session_id):
        _oops(con, stats, src_path, ordinal, "usage_conflict", line)
        stats["responses_duplicate"] = stats.get("responses_duplicate", 0) + 1
        return
    new_fields = {
        "ordinal_num": ordinal, "ts": ts, "model": model, "provider": provider,
        "effort": effort, "input_tokens": values["input_tokens"],
        "cached_input_tokens": values["cached_input_tokens"],
        "cache_write_input_tokens": values["cache_write_input_tokens"],
        "output_tokens": values["output_tokens"],
        "reasoning_output_tokens": values["reasoning_output_tokens"],
        "total_tokens": total, "semantics": semantics, "cost_usd": cost_usd,
        "source_id": source_id,
    }
    changed = any(existing[col] != val for col, val in new_fields.items())
    if not changed:
        stats["responses_duplicate"] = stats.get("responses_duplicate", 0) + 1
        return
    con.execute(
        "UPDATE responses SET source_id=?, ordinal_num=?, ts=?, model=?,"
        " provider=?, effort=?, input_tokens=?, cached_input_tokens=?,"
        " cache_write_input_tokens=?, output_tokens=?,"
        " reasoning_output_tokens=?, total_tokens=?, semantics=?, cost_usd=?"
        " WHERE response_id=?",
        (source_id, ordinal, ts, model, provider, effort,
         values["input_tokens"], values["cached_input_tokens"],
         values["cache_write_input_tokens"], values["output_tokens"],
         values["reasoning_output_tokens"], total, semantics, cost_usd,
         response_id))
    stats["responses_inserted"] = stats.get("responses_inserted", 0) + 1


def _upsert_event(con: sqlite3.Connection, stats: dict, *, source_id: int,
                  session_key: str, family: str, native_id, ordinal=None,
                  ts=None, turn_id=None, name=None, target=None, status=None,
                  duration_ms=None, size_bytes=None, truncated=None,
                  fingerprint=None, detail=None) -> None:
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
    safe_name = privacy.filter_event_name(family, name)
    safe_status = privacy.filter_event_status(family, status)
    safe_target = privacy.filter_target(target) or None
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
                   "submissions_inserted": 0, "malformed": 0}
    sess_id = sess.get("id")
    src_path = f"{abs_db_path}#{sess_id}"
    session_key = f"{HARNESS}:{sess_id}"
    msg_n, msg_m = _count_max(native, "message", sess_id)
    part_n, part_m = _count_max(native, "part", sess_id)
    fp = fingerprint(sess.get("time_updated"), msg_m, part_m, msg_n, part_n)
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
            "UPDATE submissions SET text_excerpt=?, text_hash=?, kind=?,"
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
    ordinal = 0
    for index, msg, data in parsed_messages:
        ordinal += 1
        _ingest_message(con, native, stats, identity, src_path, source_id,
                        session_key, sess, msg, data, index, ordinal,
                        by_message.get(msg.get("id"), []),
                        privacy_stale=privacy_stale)
    for part in parts:
        ordinal += 1
        _ingest_part(con, stats, identity, src_path, source_id,
                      session_key, sess, roles, part, ordinal,
                      privacy_stale=privacy_stale)
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
    if privacy_stale:
        # Rule 7: native free-text titles are never stored; a version
        # change clears any title an older import kept.
        con.execute("UPDATE sessions SET title=NULL WHERE session_key=?",
                    (session_key,))
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
        (fp, size_bytes, db.now(),
         int((time.monotonic() - started) * 1000), sess_id,
         new_version, source_id))
    con.commit()
    stats["unchanged"] = False
    return stats


def _ingest_message(con, native, stats, identity, src_path, source_id,
                    session_key, sess, msg, data, index, ordinal,
                    msg_parts, privacy_stale: bool = False) -> None:
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
        # survives in the status column. update=True lets a privacy-stale
        # re-import replace or clear an older unsafe detail row.
        insert_event(con, stats, source_id=source_id,
                     session_key=session_key, family="lifecycle",
                     native_id=msg_id, ordinal=ordinal, ts=ts,
                     name="error",
                     status=str(code) if isinstance(
                         code, int) and not isinstance(code, bool) else None,
                     detail=None, update=privacy_stale)
    role = data.get("role")
    if role == "assistant":
        _ingest_response(con, stats, src_path, source_id, session_key,
                         sess, msg, data, index, ts, ordinal)
    elif role == "user":
        _ingest_submission(con, stats, identity, src_path, source_id,
                           session_key, sess, msg, data, index, ts,
                           msg_parts, ordinal)
    else:
        _oops(con, stats, src_path, ordinal, "unknown_record",
              _record_line(msg.get("data"), data))


def _ingest_response(con, stats, src_path, source_id, session_key, sess,
                     msg, data, index, ts, ordinal) -> None:
    timing = data.get("time") if isinstance(data.get("time"), dict) else {}
    if timing.get("completed") is None:
        return
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) \
        else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) \
        else {}
    values = {"input_tokens": _int_or_none(tokens.get("input")),
              "output_tokens": _int_or_none(tokens.get("output")),
              "reasoning_output_tokens": _int_or_none(
                  tokens.get("reasoning")),
              "cached_input_tokens": _int_or_none(cache.get("read")),
              "cache_write_input_tokens": _int_or_none(cache.get("write"))}
    if all(v == 0 for v in values.values()):
        return
    total = None
    if all(v is not None for v in values.values()):
        total = sum(values.values())
    cost = data.get("cost")
    cost_usd = cost if isinstance(cost, (int, float)) \
        and not isinstance(cost, bool) else None
    _upsert_response(
        con, stats, src_path, response_id=f"{HARNESS}:{msg.get('id')}",
        source_id=source_id, session_key=session_key,
        session_id=sess.get("id"), ordinal=index, ts=ts,
        model=data.get("modelID"), provider=data.get("providerID"),
        effort=data.get("variant"), values=values, total=total,
        semantics=SEMANTICS, cost_usd=cost_usd,
        line=_record_line(msg.get("data"), data))


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
        # No valid text remains: never leave a stale excerpt behind. Clear
        # any existing row in place with an empty excerpt and matching
        # non-genuine classification instead of returning early.
        existing = con.execute(
            "SELECT kind, text_hash, text_excerpt, is_genuine FROM submissions"
            " WHERE native_id=?", (native_id,)).fetchone()
        if existing is None:
            return
        digest = text_hash("")
        if (existing["kind"] != "synthetic" or existing["text_hash"] != digest
                or (existing["text_excerpt"] or "") != ""
                or existing["is_genuine"] != 0):
            con.execute(
                "UPDATE submissions SET source_id=?, session_key=?,"
                " ordinal_num=?, ts=?, kind=?, text_hash=?, text_excerpt=?,"
                " is_genuine=? WHERE native_id=?",
                (source_id, session_key, index, ts, "synthetic", digest, "",
                 0, native_id))
            stats["submissions_inserted"] += 1
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
        "SELECT kind, text_hash, text_excerpt, is_genuine FROM submissions"
        " WHERE native_id=?", (native_id,)).fetchone()
    if existing is None:
        con.execute(
            "INSERT INTO submissions(native_id, source_id, session_key,"
            " ordinal_num, ts, kind, text_hash, text_excerpt, is_genuine)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (native_id, source_id, session_key, index, ts,
             kind, digest, excerpt, is_genuine))
        stats["submissions_inserted"] += 1
        return
    if (existing["kind"] != kind or existing["text_hash"] != digest
            or (existing["text_excerpt"] or "") != excerpt
            or existing["is_genuine"] != is_genuine):
        con.execute(
            "UPDATE submissions SET source_id=?, session_key=?,"
            " ordinal_num=?, ts=?, kind=?, text_hash=?, text_excerpt=?,"
            " is_genuine=? WHERE native_id=?",
            (source_id, session_key, index, ts, kind, digest, excerpt,
             is_genuine, native_id))
        stats["submissions_inserted"] += 1


def _ingest_part(con, stats, identity, src_path, source_id, session_key,
                  sess, roles, part, ordinal,
                  privacy_stale: bool = False) -> None:
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
    if ptype == "tool":
        _ingest_tool(con, stats, identity, src_path, source_id,
                      session_key, sess, roles, part, data, ordinal, ts,
                      line=_record_line(raw, data))
    elif ptype == "patch":
        _ingest_patch(con, stats, source_id, session_key, part, data,
                      ordinal, ts)
    elif ptype == "compaction":
        # Rule 6 via the core writer: closed name set. update=True lets a
        # privacy-stale re-import repopulate a cleared row in place.
        insert_event(con, stats, source_id=source_id,
                     session_key=session_key, family="compaction",
                     native_id=part_id, ordinal=ordinal, ts=ts,
                     name="compaction", update=privacy_stale)
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


def _ingest_tool(con, stats, identity, src_path, source_id, session_key,
                  sess, roles, part, data, ordinal, ts,
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
    # Rule 6: the tool_call family keeps no detail; the message linkage
    # some older imports stored is dropped rather than persisted. The name
    # passes through privacy.filter_event_name inside _upsert_event, so
    # free-text tool names never persist.
    _upsert_event(con, stats, source_id=source_id, session_key=session_key,
                  family="tool_call", native_id=call_id, ordinal=ordinal,
                  ts=ts, name=tool[:200], target=target,
                  fingerprint=arg_fp, detail=None)
    if status is None or (
            isinstance(status, str) and status in _INCOMPLETE_TOOL_STATUSES):
        # No terminal result yet; a later snapshot must add it.
        pass
    elif status in ("completed", "error"):
        result_status = "ok" if status == "completed" else "error"
        _upsert_event(con, stats, source_id=source_id,
                      session_key=session_key, family="tool_result",
                      native_id=call_id, ordinal=ordinal, ts=ts,
                      name=tool[:200], target=target,
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
        skill = skill_from_path(target)
        family = "skill_read" if skill else "read"
        _upsert_event(con, stats, source_id=source_id,
                      session_key=session_key, family=family,
                      native_id=call_id, ordinal=ordinal, ts=ts,
                      name=os.path.basename(target)[:200], target=target,
                      status="ok", size_bytes=_output_size(output),
                      fingerprint=fingerprint(target),
                      detail={"skill": skill} if skill else None)
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
        if isinstance(skill_dir, str) and skill_dir:
            identity.observe_path(
                os.path.join(skill_dir, "SKILL.md"))
        # Rule 6: the skill_invoke family keeps no detail; the validated
        # skill name survives in the name and target columns, or neither
        # when the native value is free text.
        _upsert_event(con, stats, source_id=source_id,
                      session_key=session_key, family="skill_invoke",
                      native_id=call_id, ordinal=ordinal, ts=ts,
                      name=safe_skill, target=safe_skill,
                      detail=None)
    if tool in ("edit", "write", "patch") and target:
        _upsert_event(con, stats, source_id=source_id,
                      session_key=session_key, family="file_change",
                      native_id=call_id, ordinal=ordinal, ts=ts,
                      name=tool[:200], target=target,
                      fingerprint=fingerprint(tool, target),
                      detail=None)


def _ingest_patch(con, stats, source_id, session_key, part, data, ordinal,
                  ts) -> None:
    digest = data.get("hash")
    files = data.get("files")
    file_list = files if isinstance(files, list) else []
    # Only a non-empty native string selects the file target; anything
    # else is dropped, never stringified into the ledger.
    first = file_list[0] if file_list else None
    target = privacy.filter_target(first) \
        if isinstance(first, str) and first else None
    part_id = part.get("id") if isinstance(part, dict) else None
    if not isinstance(part_id, str) or not part_id:
        # The caller guarantees a valid part id; fail closed otherwise.
        return
    # Rule 6: the file_change family keeps only path lists, so an
    # arbitrary native hash is omitted, never coerced. The fingerprint
    # column keeps only our own fixed-format generated digest.
    _upsert_event(con, stats, source_id=source_id, session_key=session_key,
                  family="file_change", native_id=part_id,
                  ordinal=ordinal, ts=ts, name="patch", target=target,
                  fingerprint=fingerprint("patch", digest, file_list),
                  detail=None)
