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
    ("tool_results", True, "tool results joined on callID; completed maps to ok, error to error; running has no result yet"),
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
            rows = native.execute(
                "SELECT id, parent_id, directory, title, version,"
                " time_created, time_updated, time_compacting"
                " FROM session ORDER BY id").fetchall()
        except sqlite3.Error as exc:
            totals["failed"].append({"path": abs_path, "error": str(exc)})
            return totals
        for row in rows:
            try:
                stats = import_session(con, native, abs_path, dict(row),
                                       full=full)
            except (OSError, sqlite3.DatabaseError) as exc:
                totals["failed"].append(
                    {"path": f"{abs_path}#{row['id']}", "error": str(exc)})
                continue
            totals["sources"] += 1
            totals["unchanged"] += 1 if stats.get("unchanged") else 0
            for key in ("responses_inserted", "events_inserted",
                        "submissions_inserted", "malformed"):
                totals[key] += stats.get(key, 0)
    finally:
        native.close()
    return totals


def _oops(con: sqlite3.Connection, stats: dict, src_path: str,
          ordinal, message: str, excerpt: str = "") -> None:
    stats["malformed"] += 1
    con.execute(
        "INSERT INTO import_errors(harness, source_path, ordinal_num, error,"
        " line_excerpt, created_at) VALUES(?,?,?,?,?,?)",
        (HARNESS, src_path, ordinal, message, (excerpt or "")[:200],
         db.now()))


def _target(tool_input: dict) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    for key in ("filePath", "path"):
        if tool_input.get(key):
            return str(tool_input[key])[:500]
    if tool_input.get("command"):
        return str(tool_input["command"])[:500]
    if tool_input.get("pattern"):
        return str(tool_input["pattern"])[:500]
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
    msg_row = native.execute(
        "SELECT COUNT(*) n, MAX(time_updated) m FROM message"
        " WHERE session_id=?", (sess_id,)).fetchone()
    part_row = native.execute(
        "SELECT COUNT(*) n, MAX(time_updated) m FROM part"
        " WHERE session_id=?", (sess_id,)).fetchone()
    fp = fingerprint(sess.get("time_updated"),
                     msg_row["m"] if msg_row else None,
                     part_row["m"] if part_row else None,
                     msg_row["n"] if msg_row else 0,
                     part_row["n"] if part_row else 0)
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
        messages = native.execute(
            "SELECT id, time_created, time_updated, data FROM message"
            " WHERE session_id=? ORDER BY time_created, id",
            (sess_id,)).fetchall()
    except sqlite3.Error as exc:
        _oops(con, stats, abs_db_path, None, f"message_read: {exc}", sess_id or "")
        messages = []
    try:
        parts = native.execute(
            "SELECT id, message_id, time_created, time_updated, data FROM part"
            " WHERE session_id=? ORDER BY time_created, id",
            (sess_id,)).fetchall()
    except sqlite3.Error as exc:
        _oops(con, stats, abs_db_path, None, f"part_read: {exc}", sess_id or "")
        parts = []
    by_message: dict[str, list] = {}
    for part in parts:
        by_message.setdefault(part["message_id"], []).append(part)
    roles: dict[str, str | None] = {}
    parsed_messages: list[tuple] = []
    for index, msg in enumerate(messages):
        try:
            data = json.loads(msg["data"])
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
            _oops(con, stats, abs_db_path, index, f"message_json: {exc}",
                  str(msg["data"])[:200])
            continue
        if not isinstance(data, dict):
            _oops(con, stats, abs_db_path, index, "message_shape",
                  str(msg["data"])[:200])
            continue
        parsed_messages.append((index, msg, data))
        roles[msg["id"]] = data.get("role")
    ordinal = 0
    for index, msg, data in parsed_messages:
        ordinal += 1
        _ingest_message(con, native, stats, identity, abs_db_path, source_id,
                        session_key, sess, msg, data, index, ordinal,
                        by_message.get(msg["id"], []))
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
              "title": sess.get("title"),
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
    msg_id = msg["id"]
    timing = data.get("time") if isinstance(data.get("time"), dict) else {}
    completed = timing.get("completed")
    created = timing.get("created")
    ts = iso_ts(completed if completed is not None else
                (created if created is not None else msg["time_created"]))
    error = data.get("error")
    if isinstance(error, dict):
        native_name = error.get("name") or "error"
        detail = error.get("data") if isinstance(error.get("data"), dict) \
            else {}
        code = detail.get("statusCode")
        insert_event(con, stats, source_id=source_id,
                     session_key=session_key, family="lifecycle",
                     native_id=msg_id, ordinal=ordinal, ts=ts,
                     name="error",
                     status=str(code) if code is not None else None,
                     detail={"error": str(native_name)[:200],
                             "status_code": code})
    role = data.get("role")
    if role == "assistant":
        _ingest_response(con, stats, abs_db_path, source_id, session_key,
                         sess, msg, data, index, ts)
    elif role == "user":
        _ingest_submission(con, stats, identity, abs_db_path, source_id,
                           session_key, sess, msg, data, index, ts,
                           msg_parts)


def _ingest_response(con, stats, abs_db_path, source_id, session_key, sess,
                     msg, data, index, ts) -> None:
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
    cur = con.execute(
        "INSERT OR IGNORE INTO responses(response_id, source_id, harness,"
        " session_key, session_id, ordinal_num, ts, model, provider, effort,"
        " input_tokens, cached_input_tokens, cache_write_input_tokens,"
        " output_tokens, reasoning_output_tokens, total_tokens, semantics,"
        " cost_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{HARNESS}:{msg['id']}", source_id, HARNESS, session_key,
         sess.get("id"), index, ts, data.get("modelID"),
         data.get("providerID"), data.get("variant"),
         values["input_tokens"], values["cached_input_tokens"],
         values["cache_write_input_tokens"], values["output_tokens"],
         values["reasoning_output_tokens"], total, SEMANTICS, cost_usd))
    stats["responses_inserted" if cur.rowcount else "responses_duplicate"] += 1


def _ingest_submission(con, stats, identity, abs_db_path, source_id,
                       session_key, sess, msg, data, index, ts,
                       msg_parts) -> None:
    texts: list[str] = []
    flags: list[bool] = []
    for part in msg_parts:
        try:
            pdata = json.loads(part["data"])
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            continue
        if not isinstance(pdata, dict) or pdata.get("type") != "text":
            continue
        text = pdata.get("text")
        if not isinstance(text, str) or not text:
            continue
        texts.append(text)
        flags.append(pdata.get("synthetic") is True)
        identity.observe_text(text)
    if not texts:
        return
    body = "".join(texts)
    if sess.get("parent_id"):
        kind = "synthetic"
    elif flags and all(flags):
        kind = "synthetic"
    else:
        kind = "genuine"
    cur = con.execute(
        "INSERT OR IGNORE INTO submissions(native_id, source_id, session_key,"
        " ordinal_num, ts, kind, text_hash, text_excerpt, is_genuine)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (f"{HARNESS}:{msg['id']}", source_id, session_key, index, ts,
         kind, text_hash(body), body[:300], 1 if kind == "genuine" else 0))
    if cur.rowcount:
        stats["submissions_inserted"] += 1


def _ingest_part(con, stats, identity, abs_db_path, source_id, session_key,
                 sess, roles, part, ordinal) -> None:
    try:
        data = json.loads(part["data"])
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        _oops(con, stats, abs_db_path, ordinal, f"part_json: {exc}",
              str(part["data"])[:200])
        return
    if not isinstance(data, dict):
        _oops(con, stats, abs_db_path, ordinal, "part_shape",
              str(part["data"])[:200])
        return
    ptype = data.get("type")
    ts = iso_ts(part["time_created"])
    if ptype == "tool":
        _ingest_tool(con, stats, identity, source_id, session_key, part,
                     data, ordinal, ts)
    elif ptype == "patch":
        _ingest_patch(con, stats, source_id, session_key, part, data,
                      ordinal, ts)
    elif ptype == "compaction":
        insert_event(con, stats, source_id=source_id,
                     session_key=session_key, family="compaction",
                     native_id=part["id"], ordinal=ordinal, ts=ts,
                     name="compaction")
    # text, reasoning, step-start, step-finish and file parts carry no
    # ledger event: user text is handled per message, and file data URLs,
    # reasoning bodies and step checkpoints stay out of the ledger.


def _ingest_tool(con, stats, identity, source_id, session_key, part, data,
                 ordinal, ts) -> None:
    tool = data.get("tool") or "unknown"
    call_id = data.get("callID")
    if not call_id:
        _oops(con, stats, "", ordinal, "tool_missing_callID",
              json.dumps(data, default=str)[:200])
        return
    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    status = state.get("status")
    tool_input = state.get("input") if isinstance(state.get("input"), dict) \
        else {}
    output = state.get("output")
    title = state.get("title")
    target = _target(tool_input)
    try:
        arg_fp = fingerprint(tool, json.dumps(tool_input, sort_keys=True,
                                              default=str)[:4000])
    except (TypeError, ValueError):
        arg_fp = fingerprint(tool)
    detail = {"message_id": part["message_id"]}
    if isinstance(title, str) and title:
        detail["title"] = title[:200]
    insert_event(con, stats, source_id=source_id, session_key=session_key,
                 family="tool_call", native_id=call_id, ordinal=ordinal,
                 ts=ts, name=str(tool)[:200], target=target,
                 fingerprint=arg_fp, detail=detail)
    if status == "running" or status is None:
        # No terminal result yet; a later snapshot must add it.
        pass
    else:
        if status == "completed":
            result_status = "ok"
        elif status == "error":
            result_status = "error"
        else:
            result_status = None
        insert_event(con, stats, source_id=source_id, session_key=session_key,
                     family="tool_result", native_id=call_id, ordinal=ordinal,
                     ts=ts, name=str(tool)[:200], target=target,
                     status=result_status, duration_ms=_duration_ms(state),
                     size_bytes=_output_size(output),
                     detail={"tool": str(tool)[:200]})
    if tool == "read" and status == "completed" and target:
        identity.observe_path(target)
        skill = skill_from_path(target)
        family = "skill_read" if skill else "read"
        insert_event(con, stats, source_id=source_id,
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
        insert_event(con, stats, source_id=source_id,
                     session_key=session_key, family="skill_invoke",
                     native_id=call_id, ordinal=ordinal, ts=ts,
                     name=skill_name[:200], target=skill_name[:500],
                     detail={"skill": skill_name[:200]})
    if tool in ("edit", "write", "patch") and target:
        insert_event(con, stats, source_id=source_id,
                     session_key=session_key, family="file_change",
                     native_id=call_id, ordinal=ordinal, ts=ts,
                     name=str(tool)[:200], target=target,
                     fingerprint=fingerprint(tool, target),
                     detail={"tool": str(tool)[:200]})


def _ingest_patch(con, stats, source_id, session_key, part, data, ordinal,
                  ts) -> None:
    digest = data.get("hash")
    files = data.get("files")
    file_list = files if isinstance(files, list) else []
    target = str(file_list[0])[:500] if file_list and file_list[0] else None
    insert_event(con, stats, source_id=source_id, session_key=session_key,
                 family="file_change", native_id=part["id"], ordinal=ordinal,
                 ts=ts, name="patch", target=target,
                 fingerprint=fingerprint("patch", digest, file_list),
                 detail={"hash": digest} if isinstance(digest, str) else None)
