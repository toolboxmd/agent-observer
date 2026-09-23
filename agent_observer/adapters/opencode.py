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
import re
import sqlite3
import time

from .. import db
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
# submissions.text_excerpt is empty unless the row is a genuine human
# submission from the main session. Genuine text loses every `<tag ...>`
# through its matching `</tag>` and every `<<<NAME>>>` through its matching
# `<<<END_NAME>>>`, fail-closed through the end of the text when the closer
# is missing. Identity observation still sees the full text so
# instructions_sha256 / preferences_sha256 keep working.
_DIRECTION_OPEN = "<<<AGENTSMD_PROJECT_DIRECTION_V1>>>"
_DIRECTION_CLOSE = "<<<END_AGENTSMD_PROJECT_DIRECTION_V1>>>"

# Closed import_errors.error categories. Every _oops call must use exactly
# one of these with nothing appended: no exception class names, messages,
# or record values.
IMPORT_ERROR_CATEGORIES = frozenset({
    "malformed_json",
    "unknown_record",
    "schema_error",
    "missing_id",
    "malformed_usage",
    "usage_conflict",
    "source_unreadable",
    "unsupported_schema",
})

# Whitelisted events.detail_json keys. Values must be numbers, booleans,
# hashes, native identifiers, file paths, shell commands, or closed-enum
# status/kind strings. Never titles, messages, error text, outputs,
# content, arguments or other free text.
_SAFE_DETAIL_KEYS = frozenset({"message_id", "status_code", "skill", "hash"})

_TAG_OPEN_RE = re.compile(r"<([A-Za-z_:][A-Za-z0-9_.:-]*)\b[^>]*?>")
_TRIPLE_OPEN_RE = re.compile(r"<<<([A-Za-z0-9_.:-]+)>>>")
_WS_COLLAPSE_RE = re.compile(r"\s+")
_SAFE_IDENT_RE = re.compile(r"[A-Za-z0-9_:.-]+\Z")
_SAFE_HASH_RE = re.compile(r"[A-Za-z0-9_.-]+\Z")

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


def _shape_keys(data) -> list[str] | None:
    if not isinstance(data, dict):
        return None
    try:
        return sorted(str(k) for k in data.keys())
    except (TypeError, ValueError):
        return []


def _sanitized_excerpt(native_id=None, data=None, role=None,
                       ptype=None) -> str:
    """Structure-only import_errors excerpt: sorted top-level key names.

    Names, never values. No native ids, roles, types, paths or any other
    value. Non-dict/missing records yield the empty structural list.
    Bounded to 200 characters.
    """
    keys = _shape_keys(data)
    if keys:
        return ",".join(keys)[:200]
    return "[]"


def _oops(con: sqlite3.Connection, stats: dict, src_path: str,
          ordinal, message: str, excerpt: str = "") -> None:
    if message not in IMPORT_ERROR_CATEGORIES:
        raise ValueError(f"unlisted import error category: {message!r}")
    stats["malformed"] += 1
    con.execute(
        "INSERT INTO import_errors(harness, source_path, ordinal_num, error,"
        " line_excerpt, created_at) VALUES(?,?,?,?,?,?)",
        (HARNESS, src_path, ordinal, message, (excerpt or "")[:200],
         db.now()))


def _remove_triple_blocks(text: str) -> str:
    """Remove every <<<NAME>>>..<<<END_NAME>>> span, fail-closed to end."""
    out = text
    pos = 0
    while True:
        match = _TRIPLE_OPEN_RE.search(out, pos)
        if match is None:
            return out
        name = match.group(1)
        if name.startswith("END_"):
            # Stray closer without an opener carries no block: skip it.
            pos = match.end()
            continue
        start = match.start()
        open_end = match.end()
        closer = f"<<<END_{name}>>>"
        # Nesting-aware: same NAME opens inside deepen the span.
        depth = 1
        cursor = open_end
        opener = f"<<<{name}>>>"
        while depth > 0:
            next_open = out.find(opener, cursor)
            next_close = out.find(closer, cursor)
            if next_close == -1:
                # Fail closed: opening through end of text.
                return out[:start]
            if next_open != -1 and next_open < next_close:
                depth += 1
                cursor = next_open + len(opener)
            else:
                depth -= 1
                cursor = next_close + len(closer)
        out = out[:start] + out[cursor:]
        pos = 0


def _remove_tagged_blocks(text: str) -> str:
    """Remove every <tag ...>..</tag> span for any tag, fail-closed."""
    out = text
    while True:
        match = _TAG_OPEN_RE.search(out)
        if match is None:
            return out
        tag = match.group(1)
        start = match.start()
        open_end = match.end()
        token = match.group(0)
        if token.endswith("/>"):
            # Self-closing tag carries only attributes: drop the token.
            out = out[:start] + out[open_end:]
            continue
        # Nesting-aware match for the same tag name.
        open_pat = re.compile(
            r"<" + re.escape(tag) + r"(?:\s[^>]*?)?>")
        close_pat = re.compile(r"</" + re.escape(tag) + r"\s*>")
        depth = 1
        cursor = open_end
        span_end = -1
        while depth > 0:
            next_open = open_pat.search(out, cursor)
            next_close = close_pat.search(out, cursor)
            if next_close is None:
                # Fail closed: opening through end of text.
                return out[:start]
            if next_open is not None and next_open.start() < next_close.start():
                # Skip self-closing nested opens.
                if next_open.group(0).endswith("/>"):
                    cursor = next_open.end()
                    continue
                depth += 1
                cursor = next_open.end()
            else:
                depth -= 1
                if depth == 0:
                    span_end = next_close.end()
                cursor = next_close.end()
        out = out[:start] + out[span_end:]


def _sanitize_user_text(text: str) -> str:
    """Human-only excerpt basis with all tagged/triple blocks removed.

    Every `<tag ...>`..`</tag>` and every `<<<NAME>>>`..`<<<END_NAME>>>`
    span is removed, fail-closed through the end of the text when the
    closer is missing. Whitespace is collapsed; the caller keeps the
    first 300 characters for the excerpt while the hash covers this full
    collapsed basis.
    """
    if not isinstance(text, str):
        return ""
    cleaned = _remove_triple_blocks(text)
    cleaned = _remove_tagged_blocks(cleaned)
    return _WS_COLLAPSE_RE.sub(" ", cleaned).strip()


def _sanitize_assistant_text(text: str) -> str:
    """Assistant excerpt basis with the same block removal applied."""
    return _sanitize_user_text(text)


def _safe_detail(detail) -> dict | None:
    """Whitelisted detail_json subset: safe keys and safe value types only.

    Keeps numbers, booleans, hashes, native identifiers, file paths, shell
    commands, or closed-enum status/kind strings. Drops titles, messages,
    error text, outputs, content, arguments and all other free text.
    """
    if not isinstance(detail, dict):
        return None
    kept: dict = {}
    for key, value in detail.items():
        if key not in _SAFE_DETAIL_KEYS:
            continue
        if key == "message_id":
            if isinstance(value, str) and value and len(value) <= 200 \
                    and _SAFE_IDENT_RE.fullmatch(value) is not None:
                kept[key] = value[:200]
        elif key == "status_code":
            if isinstance(value, int) and not isinstance(value, bool):
                kept[key] = value
        elif key == "skill":
            if isinstance(value, str) and value \
                    and _SAFE_IDENT_RE.fullmatch(value[:200]) is not None:
                kept[key] = value[:200]
        elif key == "hash":
            if isinstance(value, str) and value and len(value) <= 128 \
                    and _SAFE_HASH_RE.fullmatch(value) is not None:
                kept[key] = value
    return kept or None


def _target(tool_input: dict) -> str | None:
    # Rule 7: only identifiers, paths and commands. Patterns and other
    # free-text arguments never enter the ledger as targets.
    if not isinstance(tool_input, dict):
        return None
    for key in ("filePath", "path"):
        if tool_input.get(key):
            return str(tool_input[key])[:500]
    if tool_input.get("command"):
        return str(tool_input["command"])[:500]
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
                     total, semantics, cost_usd) -> None:
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
        _oops(con, stats, src_path, ordinal, "usage_conflict",
              _sanitized_excerpt(response_id,
                                 {"keys": "response", "session": session_key},
                                 role="assistant"))
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
    if not native_id:
        raise ValueError(f"{family} event missing native identity")
    native_id = str(native_id)
    safe = _safe_detail(detail)
    try:
        detail_json = json.dumps(safe, sort_keys=True)[:4000] \
            if safe else None
    except (TypeError, ValueError):
        detail_json = None
    existing = con.execute(
        "SELECT * FROM events WHERE session_key=? AND family=? AND native_id=?",
        (session_key, family, native_id)).fetchone()
    if existing is None:
        con.execute(
            "INSERT INTO events(source_id, session_key, ordinal_num, ts,"
            " family, native_id, turn_id, name, target, status, duration_ms,"
            " size_bytes, truncated, fingerprint, detail_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, session_key, ordinal, ts, family, native_id, turn_id,
             name, target, status, duration_ms, size_bytes, truncated,
             fingerprint, detail_json))
        stats["events_inserted"] = stats.get("events_inserted", 0) + 1
        return
    new_fields = {
        "source_id": source_id, "ordinal_num": ordinal, "ts": ts,
        "turn_id": turn_id, "name": name, "target": target, "status": status,
        "duration_ms": duration_ms, "size_bytes": size_bytes,
        "truncated": truncated, "fingerprint": fingerprint,
        "detail_json": detail_json,
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
        (source_id, ordinal, ts, turn_id, name, target, status, duration_ms,
         size_bytes, truncated, fingerprint, detail_json, session_key,
         family, native_id))
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
    if row["sha256"] == fp and not full:
        stats["unchanged"] = True
        con.commit()
        return stats
    source_id = row["id"]
    identity = SessionIdentity()
    try:
        messages = _fetch_dicts(
            native, "message",
            ["id", "session_id", "time_created", "time_updated", "data"],
            where="session_id=?", args=(sess_id,),
            order=["time_created", "id"])
    except sqlite3.Error:
        _oops(con, stats, abs_db_path, None,
              "source_unreadable",
              _sanitized_excerpt(sess_id, None, role="read"))
        messages = []
    try:
        parts = _fetch_dicts(
            native, "part",
            ["id", "message_id", "session_id", "time_created", "time_updated",
             "data"],
            where="session_id=?", args=(sess_id,),
            order=["time_created", "id"])
    except sqlite3.Error:
        _oops(con, stats, abs_db_path, None,
              "source_unreadable",
              _sanitized_excerpt(sess_id, None, role="read"))
        parts = []
    by_message: dict[str, list] = {}
    for part in parts:
        by_message.setdefault(part.get("message_id"), []).append(part)
    roles: dict[str, str | None] = {}
    parsed_messages: list[tuple] = []
    for index, msg in enumerate(messages):
        raw = msg.get("data")
        msg_id = msg.get("id")
        if not msg_id or raw is None:
            _oops(con, stats, abs_db_path, index, "schema_error",
                  _sanitized_excerpt(msg_id, None, role=None))
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            _oops(con, stats, abs_db_path, index,
                  "malformed_json",
                  _sanitized_excerpt(msg_id, None, role=None))
            continue
        if not isinstance(data, dict):
            _oops(con, stats, abs_db_path, index, "schema_error",
                  _sanitized_excerpt(msg_id, data, role=None))
            continue
        parsed_messages.append((index, msg, data))
        roles[msg_id] = data.get("role")
    ordinal = 0
    for index, msg, data in parsed_messages:
        ordinal += 1
        _ingest_message(con, native, stats, identity, abs_db_path, source_id,
                        session_key, sess, msg, data, index, ordinal,
                        by_message.get(msg.get("id"), []))
    for part in parts:
        ordinal += 1
        _ingest_part(con, stats, identity, abs_db_path, source_id,
                      session_key, sess, roles, part, ordinal)
    if sess.get("time_compacting") is not None:
        insert_event(con, stats, source_id=source_id,
                     session_key=session_key, family="compaction",
                     native_id="time_compacting", ordinal=ordinal,
                     ts=iso_ts(sess.get("time_compacting")),
                     name="time_compacting")
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
    con.execute(
        "UPDATE sources SET sha256=?, size_bytes=?, imported_at=?,"
        " import_ms=?, session_id=? WHERE id=?",
        (fp, size_bytes, db.now(),
         int((time.monotonic() - started) * 1000), sess_id, source_id))
    con.commit()
    stats["unchanged"] = False
    return stats


def _ingest_message(con, native, stats, identity, abs_db_path, source_id,
                    session_key, sess, msg, data, index, ordinal,
                    msg_parts) -> None:
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
        safe_detail = {"status_code": code} \
            if isinstance(code, int) and not isinstance(code, bool) else None
        insert_event(con, stats, source_id=source_id,
                     session_key=session_key, family="lifecycle",
                     native_id=msg_id, ordinal=ordinal, ts=ts,
                     name="error",
                     status=str(code) if isinstance(
                         code, int) and not isinstance(code, bool) else None,
                     detail=safe_detail)
    role = data.get("role")
    if role == "assistant":
        _ingest_response(con, stats, abs_db_path, source_id, session_key,
                         sess, msg, data, index, ts, ordinal)
    elif role == "user":
        _ingest_submission(con, stats, identity, abs_db_path, source_id,
                           session_key, sess, msg, data, index, ts,
                           msg_parts, ordinal)
    else:
        _oops(con, stats, abs_db_path, ordinal, "unknown_record",
              _sanitized_excerpt(msg_id, data, role=role))


def _ingest_response(con, stats, abs_db_path, source_id, session_key, sess,
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
        con, stats, abs_db_path, response_id=f"{HARNESS}:{msg.get('id')}",
        source_id=source_id, session_key=session_key,
        session_id=sess.get("id"), ordinal=index, ts=ts,
        model=data.get("modelID"), provider=data.get("providerID"),
        effort=data.get("variant"), values=values, total=total,
        semantics=SEMANTICS, cost_usd=cost_usd)


def _ingest_submission(con, stats, identity, abs_db_path, source_id,
                       session_key, sess, msg, data, index, ts,
                       msg_parts, ordinal) -> None:
    texts: list[str] = []
    flags: list[bool] = []
    for part in msg_parts:
        raw = part.get("data") if isinstance(part, dict) else None
        part_id = part.get("id") if isinstance(part, dict) else None
        if raw is None:
            _oops(con, stats, abs_db_path, ordinal, "schema_error",
                  _sanitized_excerpt(part_id, None, role="user",
                                     ptype="text"))
            continue
        try:
            pdata = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            _oops(con, stats, abs_db_path, ordinal,
                  "malformed_json",
                  _sanitized_excerpt(part_id, None, role="user",
                                     ptype="text"))
            continue
        if not isinstance(pdata, dict):
            _oops(con, stats, abs_db_path, ordinal, "schema_error",
                  _sanitized_excerpt(part_id, pdata, role="user"))
            continue
        if pdata.get("type") != "text":
            continue
        text = pdata.get("text")
        if not isinstance(text, str) or not text:
            _oops(con, stats, abs_db_path, ordinal, "malformed_usage",
                  _sanitized_excerpt(part_id, pdata, role="user",
                                     ptype="text"))
            continue
        texts.append(text)
        flags.append(pdata.get("synthetic") is True)
        # Identity sees the full valid text, including the direction block.
        identity.observe_text(text)
    if not texts:
        return
    body = "".join(texts)
    is_child = bool(sess.get("parent_id"))
    if is_child:
        kind = "synthetic"
    elif flags and all(flags):
        kind = "synthetic"
    else:
        kind = "genuine"
    # Child sessions are sub-agent turns: their prompts are agent-generated,
    # so no child text ever enters the human excerpt. The row/kind semantics
    # and identity observation above stay intact.
    if is_child:
        excerpt_src = ""
    else:
        # Excerpt uses only the human's own (non-synthetic) parts with all
        # tagged and triple-marker blocks removed, whitespace collapsed.
        human_texts = [t for t, flag in zip(texts, flags) if not flag]
        excerpt_src = _sanitize_user_text("".join(human_texts))
    excerpt = excerpt_src[:300]
    digest = text_hash(excerpt_src)
    native_id = f"{HARNESS}:{msg.get('id')}"
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


def _ingest_part(con, stats, identity, abs_db_path, source_id, session_key,
                 sess, roles, part, ordinal) -> None:
    raw = part.get("data") if isinstance(part, dict) else None
    part_id = part.get("id") if isinstance(part, dict) else None
    message_id = part.get("message_id") if isinstance(part, dict) else None
    role = roles.get(message_id) if isinstance(roles, dict) else None
    if raw is None:
        _oops(con, stats, abs_db_path, ordinal, "schema_error",
              _sanitized_excerpt(part_id, None, role=role))
        return
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        _oops(con, stats, abs_db_path, ordinal,
              "malformed_json",
              _sanitized_excerpt(part_id, None, role=role))
        return
    if not isinstance(data, dict):
        _oops(con, stats, abs_db_path, ordinal, "schema_error",
              _sanitized_excerpt(part_id, data, role=role))
        return
    ptype = data.get("type")
    ts = iso_ts(part.get("time_created"))
    if not part_id or (isinstance(part_id, str) and not part_id.strip()):
        _oops(con, stats, abs_db_path, ordinal, "missing_id",
              _sanitized_excerpt(part_id, data, role=role, ptype=ptype))
        return
    if ptype == "tool":
        _ingest_tool(con, stats, identity, abs_db_path, source_id,
                      session_key, sess, roles, part, data, ordinal, ts)
    elif ptype == "patch":
        _ingest_patch(con, stats, source_id, session_key, part, data,
                      ordinal, ts)
    elif ptype == "compaction":
        insert_event(con, stats, source_id=source_id,
                     session_key=session_key, family="compaction",
                     native_id=part_id, ordinal=ordinal, ts=ts,
                     name="compaction")
    elif ptype in _KNOWN_QUIET_PART_TYPES:
        # text, reasoning, step checkpoints and file data URLs carry no
        # ledger event: user text is handled per message, and file data,
        # reasoning bodies and step checkpoints stay out of the ledger.
        return
    else:
        _oops(con, stats, abs_db_path, ordinal, "unsupported_schema",
              _sanitized_excerpt(part_id, data, role=role, ptype=ptype))


def _ingest_tool(con, stats, identity, abs_db_path, source_id, session_key,
                 sess, roles, part, data, ordinal, ts) -> None:
    tool = data.get("tool") or "unknown"
    call_id = data.get("callID")
    part_id = part.get("id") if isinstance(part, dict) else None
    message_id = part.get("message_id") if isinstance(part, dict) else None
    role = roles.get(message_id) if isinstance(roles, dict) else None
    if not call_id:
        _oops(con, stats, abs_db_path, ordinal, "missing_id",
              _sanitized_excerpt(part_id, data, role=role, ptype="tool"))
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
    detail = {"message_id": message_id} \
        if isinstance(message_id, str) and message_id else None
    _upsert_event(con, stats, source_id=source_id, session_key=session_key,
                  family="tool_call", native_id=call_id, ordinal=ordinal,
                  ts=ts, name=str(tool)[:200], target=target,
                  fingerprint=arg_fp, detail=detail)
    if status in _INCOMPLETE_TOOL_STATUSES or status is None:
        # No terminal result yet; a later snapshot must add it.
        pass
    elif status in ("completed", "error"):
        result_status = "ok" if status == "completed" else "error"
        _upsert_event(con, stats, source_id=source_id,
                      session_key=session_key, family="tool_result",
                      native_id=call_id, ordinal=ordinal, ts=ts,
                      name=str(tool)[:200], target=target,
                      status=result_status, duration_ms=_duration_ms(state),
                      size_bytes=_output_size(output),
                      detail=None)
    else:
        _oops(con, stats, abs_db_path, ordinal, "unknown_record",
              _sanitized_excerpt(part_id, data, role=role, ptype="tool"))
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
        name = tool_input.get("name") if isinstance(tool_input, dict) \
            else None
        skill_name = str(name) if name else "unknown"
        metadata = state.get("metadata") if isinstance(
            state.get("metadata"), dict) else {}
        skill_dir = metadata.get("dir")
        if isinstance(skill_dir, str) and skill_dir:
            identity.observe_path(
                os.path.join(skill_dir, "SKILL.md"))
        _upsert_event(con, stats, source_id=source_id,
                      session_key=session_key, family="skill_invoke",
                      native_id=call_id, ordinal=ordinal, ts=ts,
                      name=skill_name[:200], target=skill_name[:500],
                      detail={"skill": skill_name[:200]})
    if tool in ("edit", "write", "patch") and target:
        _upsert_event(con, stats, source_id=source_id,
                      session_key=session_key, family="file_change",
                      native_id=call_id, ordinal=ordinal, ts=ts,
                      name=str(tool)[:200], target=target,
                      fingerprint=fingerprint(tool, target),
                      detail=None)


def _ingest_patch(con, stats, source_id, session_key, part, data, ordinal,
                  ts) -> None:
    digest = data.get("hash")
    files = data.get("files")
    file_list = files if isinstance(files, list) else []
    target = str(file_list[0])[:500] if file_list and file_list[0] else None
    _upsert_event(con, stats, source_id=source_id, session_key=session_key,
                  family="file_change", native_id=part.get("id"),
                  ordinal=ordinal, ts=ts, name="patch", target=target,
                  fingerprint=fingerprint("patch", digest, file_list),
                  detail={"hash": digest} if isinstance(digest, str) else None)
