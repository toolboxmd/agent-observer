"""Codex native adapter: parse session JSONL into the ledger.

Reads persisted Codex session records. The development verifier is kept
separate and is never imported or executed by this module.
Counter rule: each token_usage_record usage block is one atomic response.
turn_token_usage and thread_token_usage are overlapping checkpoints and are
stored per row but never summed. The compaction latest_token_usage_record is
overlapping evidence of an existing response and is never inserted as a row.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from .db import now

HARNESS = "codex"
SYNTHETIC_PREFIX = "<send_user_message_question_reply>"

CAPABILITIES = [
    # (family, supported, detail)
    ("model_usage", True, "token_usage_record per response_id; input includes cached input; output includes reasoning"),
    ("tool_calls", True, "response_item function_call/custom_tool_call with call_id join to outputs"),
    ("tool_results", True, "function_call_output/custom_tool_call_output joined by call_id; completed CommandExecution/McpToolCall retained with native ids"),
    ("compaction", True, "compacted record with window ids and compaction_response_id; latest usage is overlap"),
    ("lifecycle_task", True, "event_msg task_started/task_complete with turn timing; turn_aborted marks cancelled turns"),
    ("lifecycle_dispatch", True, "collaboration.spawn_agent call plus SubAgentActivity started; worker join needs explicit dispatch capture"),
    ("file_change", True, "FileChange items with path, change kind, content size/hash; contents never stored"),
    ("read_evidence", True, "CommandExecution parsed_cmd entries of type read; target/size/owner/window preserved"),
    ("skill_file_reads", True, "read events whose target ends with SKILL.md; mentions never count"),
    ("skill_invocation", False, "no native skill-invocation event observed in Codex sample; stays unknown"),
    ("timing_tool", True, "CommandExecution duration when reported; otherwise unknown, never invented"),
    ("quota", False, "rate-limit snapshots are account evidence, not task attribution; stays unknown at task level"),
]


def _sha256_file(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _get(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return default
    return d


def _text_of_user_message(payload: dict) -> str:
    parts = []
    for c in payload.get("content", []) or []:
        t = c.get("text", "")
        if t:
            parts.append(t)
    return "".join(parts)


def is_genuine_submission(payload: dict) -> bool:
    """A genuine submission is a user message authored by the user.

    response_item message role=user with content_item_kinds exactly
    ["user.text"] and text that is not a synthetic question reply.
    Skill/plugin/environment scaffolding has other kinds and is excluded.
    """
    if payload.get("type") != "message" or payload.get("role") != "user":
        return False
    kinds = (payload.get("internal_chat_message_metadata_passthrough") or {}).get(
        "content_item_kinds")
    if kinds != ["user.text"]:
        return False
    text = _text_of_user_message(payload).strip()
    if not text or text.startswith(SYNTHETIC_PREFIX):
        return False
    return True


def _fingerprint(*parts: object) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(repr(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def import_codex_file(con: sqlite3.Connection, path: str) -> dict:
    """Import one Codex session JSONL file. Idempotent; growing logs update.

    Malformed lines are recorded in import_errors and skipped. Previously
    valid data is never destroyed.
    """
    stats = {
        "lines": 0, "responses_inserted": 0, "responses_duplicate": 0,
        "submissions_inserted": 0, "events_inserted": 0,
        "events_duplicate": 0, "compactions": 0, "malformed": 0,
    }
    digest, raw_bytes = _sha256_file(path)
    cur = con.cursor()
    row = cur.execute(
        "SELECT id FROM sources WHERE harness=? AND sha256=?",
        (HARNESS, digest)).fetchone()
    # Whole-file content hash identifies the snapshot; a grown file is a new
    # snapshot that upserts the same natural keys, so reimport stays idempotent.
    cur.execute(
        "INSERT OR IGNORE INTO sources(harness, path, sha256, imported_at) "
        "VALUES(?,?,?,?)", (HARNESS, path, digest, now()))
    source_id = cur.execute(
        "SELECT id FROM sources WHERE harness=? AND sha256=?",
        (HARNESS, digest)).fetchone()["id"]

    max_ordinal = -1
    session_id = None
    cli_version = None
    thread_id = None

    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh):
            if not line.strip():
                continue
            stats["lines"] += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                stats["malformed"] += 1
                cur.execute(
                    "INSERT INTO import_errors(source_path, ordinal_num, error,"
                    " line_excerpt, created_at) VALUES(?,?,?,?,?)",
                    (path, None, f"json_error: {exc}", line[:200], now()))
                continue
            try:
                _ingest_record(cur, source_id, obj, stats)
            except (KeyError, TypeError, ValueError) as exc:
                stats["malformed"] += 1
                cur.execute(
                    "INSERT INTO import_errors(source_path, ordinal_num, error,"
                    " line_excerpt, created_at) VALUES(?,?,?,?,?)",
                    (path, obj.get("ordinal"),
                     f"schema_error: {exc}", line[:200], now()))
                continue
            if isinstance(obj.get("ordinal"), int):
                max_ordinal = max(max_ordinal, obj["ordinal"])
            payload = obj.get("payload", {}) if isinstance(obj, dict) else {}
            if obj.get("type") == "session_meta":
                session_id = payload.get("session_id") or session_id
                cli_version = payload.get("cli_version") or cli_version
            tid = payload.get("thread_id") if isinstance(payload, dict) else None
            if tid and thread_id is None:
                thread_id = tid

    cur.execute(
        "UPDATE sources SET ordinal_max=?, raw_bytes=?, imported_at=?, "
        "session_id=COALESCE(?, session_id), cli_version=COALESCE(?, cli_version),"
        " thread_id=COALESCE(?, thread_id) WHERE id=?",
        (max_ordinal, raw_bytes, now(), session_id, cli_version,
         thread_id, source_id))
    con.commit()
    stats.update({"source_id": source_id, "sha256": digest,
                  "ordinal_max": max_ordinal})
    return stats


def _ingest_record(cur: sqlite3.Cursor, source_id: int, obj: dict,
                   stats: dict) -> None:
    rtype = obj.get("type")
    if rtype not in ("session_meta", "event_msg", "response_item",
                     "token_usage_record", "turn_context", "compacted",
                     "world_state", "inter_agent_communication_metadata"):
        raise ValueError(f"unsupported record type: {rtype!r}")
    if rtype == "token_usage_record":
        _ingest_usage(cur, source_id, obj, stats)
    elif rtype == "response_item":
        _ingest_response_item(cur, source_id, obj, stats)
    elif rtype == "turn_context":
        _ingest_turn(cur, source_id, obj)
    elif rtype == "event_msg":
        _ingest_event_msg(cur, source_id, obj, stats)
    elif rtype == "compacted":
        _ingest_compacted(cur, source_id, obj, stats)
    # session_meta, world_state, inter_agent_communication_metadata carry no
    # ledger rows beyond source metadata; retained privately in the raw file.


def _ingest_usage(cur: sqlite3.Cursor, source_id: int, obj: dict,
                  stats: dict) -> None:
    p = obj["payload"]
    for k in ("response_id", "usage"):
        if k not in p:
            raise ValueError(f"token_usage_record missing {k}")
    u = p["usage"] or {}
    tt = p.get("turn_token_usage") or {}
    th = p.get("thread_token_usage") or {}
    try:
        cur.execute(
            "INSERT OR IGNORE INTO responses(response_id, source_id, thread_id,"
            " turn_id, root_turn_id, session_id, ordinal_num, ts, input_tokens,"
            " cached_input_tokens, cache_write_input_tokens, output_tokens,"
            " reasoning_output_tokens, total_tokens, turn_total_tokens,"
            " thread_total_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["response_id"], source_id, p.get("thread_id"),
             p.get("turn_id"), p.get("root_turn_id"), p.get("session_id"),
             obj.get("ordinal"), _ts(obj),
             u.get("input_tokens"), u.get("cached_input_tokens"),
             u.get("cache_write_input_tokens"), u.get("output_tokens"),
             u.get("reasoning_output_tokens"), u.get("total_tokens"),
             tt.get("total_tokens"), th.get("total_tokens")))
    except sqlite3.IntegrityError:
        raise ValueError("response insert failed")
    if cur.rowcount == 0:
        # Same response_id seen again: verify counters agree, never re-sum.
        existing = cur.execute(
            "SELECT total_tokens FROM responses WHERE response_id=?",
            (p["response_id"],)).fetchone()
        if existing is None or existing["total_tokens"] != u.get("total_tokens"):
            raise ValueError(
                f"conflicting usage for {p['response_id']}")
        stats["responses_duplicate"] += 1
    else:
        stats["responses_inserted"] += 1
    # Ensure the turn row exists so later joins never invent one.
    if p.get("turn_id"):
        cur.execute(
            "INSERT OR IGNORE INTO turns(turn_id, source_id, root_turn_id,"
            " session_id) VALUES(?,?,?,?)",
            (p["turn_id"], source_id, p.get("root_turn_id"),
             p.get("session_id")))


def _ingest_turn(cur: sqlite3.Cursor, source_id: int, obj: dict) -> None:
    p = obj["payload"]
    if "turn_id" not in p:
        raise ValueError("turn_context missing turn_id")
    cur.execute(
        "INSERT OR IGNORE INTO turns(turn_id, source_id, root_turn_id,"
        " session_id, model_observed, effort_observed) VALUES(?,?,?,?,?,?)",
        (p["turn_id"], source_id, p.get("root_turn_id"),
         p.get("session_id"), p.get("model"), p.get("effort")))
    cur.execute(
        "UPDATE turns SET model_observed=COALESCE(model_observed, ?), "
        "effort_observed=COALESCE(effort_observed, ?) WHERE turn_id=?",
        (p.get("model"), p.get("effort"), p["turn_id"]))


def _ingest_response_item(cur: sqlite3.Cursor, source_id: int, obj: dict,
                          stats: dict) -> None:
    p = obj["payload"]
    ptype = p.get("type")
    turn_id = (p.get("internal_chat_message_metadata_passthrough") or {}).get(
        "turn_id")
    if turn_id:
        cur.execute(
            "INSERT OR IGNORE INTO turns(turn_id, source_id) VALUES(?,?)",
            (turn_id, source_id))
    if ptype == "message" and p.get("role") == "user":
        text = _text_of_user_message(p)
        genuine = 1 if is_genuine_submission(p) else 0
        cur.execute(
            "INSERT OR IGNORE INTO submissions(native_id, source_id, turn_id,"
            " ordinal_num, ts, text_hash, text_excerpt, is_genuine)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (p.get("id"), source_id, turn_id, obj.get("ordinal"), _ts(obj),
             hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
             text[:300], genuine))
        if cur.rowcount:
            stats["submissions_inserted"] += 1
        return
    elif ptype in ("function_call", "custom_tool_call"):
        call_id = p.get("call_id")
        name = p.get("name") or (
            p.get("custom_tool_call") or {}).get("name", "unknown")
        args = p.get("arguments") if ptype == "function_call" else p.get("input")
        _insert_event(cur, source_id, obj, stats, family="tool_call",
                      native_id=call_id or p.get("id"), turn_id=turn_id,
                      name=f"{p.get('namespace', '') + '.' if p.get('namespace') else ''}{name}",
                      status=p.get("status"),
                      fingerprint=_fingerprint(name, str(args)[:2000]),
                      detail={"kind": ptype, "args_chars": len(str(args or "")),
                              "item_id": p.get("id")})
    elif ptype in ("function_call_output", "custom_tool_call_output"):
        call_id = p.get("call_id")
        out = p.get("output")
        if isinstance(out, list):
            size = sum(len(str(c.get("text", ""))) for c in out
                       if isinstance(c, dict))
        else:
            size = len(str(out or ""))
        _insert_event(cur, source_id, obj, stats, family="tool_result",
                      native_id=call_id or p.get("id"), turn_id=turn_id,
                      name=ptype, size_bytes=size,
                      detail={"kind": ptype, "item_id": p.get("id")})


def _ingest_event_msg(cur: sqlite3.Cursor, source_id: int, obj: dict,
                      stats: dict) -> None:
    p = obj["payload"]
    etype = p.get("type")
    if etype == "task_started":
        cur.execute(
            "INSERT OR IGNORE INTO turns(turn_id, source_id) VALUES(?,?)",
            (p.get("turn_id"), source_id))
    elif etype == "task_complete":
        cur.execute(
            "INSERT OR IGNORE INTO turns(turn_id, source_id) VALUES(?,?)",
            (p.get("turn_id"), source_id))
        cur.execute(
            "UPDATE turns SET completed_at=?, duration_ms=?, state='complete'"
            " WHERE turn_id=?",
            (_ts(obj), p.get("duration_ms"), p.get("turn_id")))
        _insert_event(cur, source_id, obj, stats, family="lifecycle",
                      native_id=f"task_complete:{p.get('turn_id')}",
                      turn_id=p.get("turn_id"), name="task_complete",
                      status="completed", duration_ms=p.get("duration_ms"),
                      detail={"time_to_first_token_ms":
                              p.get("time_to_first_token_ms")})
    elif etype == "item_completed":
        item = p.get("item", {}) or {}
        itype = item.get("type")
        if itype == "CommandExecution":
            _ingest_command(cur, source_id, obj, item, stats)
        elif itype == "McpToolCall":
            result = item.get("result") or {}
            content = result.get("content") or []
            size = sum(len(str(c.get("text", ""))) for c in content
                       if isinstance(c, dict))
            _insert_event(cur, source_id, obj, stats, family="tool_result",
                          native_id=item.get("id"), name=(
                              f"mcp.{item.get('server')}.{item.get('tool')}"),
                          status=item.get("status"), size_bytes=size,
                          detail={"call_id_match": "exact native id retained; "
                                  "join to calls only on equal call_id"})
        elif itype == "SubAgentActivity":
            _insert_event(cur, source_id, obj, stats, family="lifecycle",
                          native_id=item.get("id"), name="subagent_activity",
                          status=item.get("kind"),
                          detail={"agent_thread_id":
                                  item.get("agent_thread_id"),
                                  "agent_path": item.get("agent_path")})
        elif itype == "FileChange":
            # File-change notification: paths and change kinds are evidence
            # for reread diagnostics. File contents stay out of the ledger.
            changes = item.get("changes") or {}
            summary = {}
            for path, ch in changes.items():
                if isinstance(ch, dict):
                    body = str(ch.get("content") or "")
                    summary[path] = {
                        "type": ch.get("type"),
                        "content_chars": len(body),
                        "content_sha": hashlib.sha256(
                            body.encode("utf-8", "replace")).hexdigest()[:16],
                    }
                else:
                    summary[path] = {"type": "unknown"}
            _insert_event(cur, source_id, obj, stats, family="file_change",
                          native_id=item.get("id"), name="file_change",
                          turn_id=p.get("turn_id"),
                          fingerprint=_fingerprint(sorted(summary)),
                          detail={"paths": summary})
        elif itype == "ContextCompaction":
            # Sparse native compaction marker: boundary identity only.
            # Detail beyond the window link stays unknown, never invented.
            stats["compactions"] += 1
            _insert_event(cur, source_id, obj, stats, family="compaction",
                          native_id=item.get("id"), name="context_compaction",
                          turn_id=item.get("turn_id") or p.get("turn_id"),
                          detail={"native": "ContextCompaction marker; "
                                  "window detail unknown"})
        elif itype in ("UserMessage", "AgentMessage", "Reasoning", "Extension"):
            # Content payload, not an operational event; user text identity is
            # already captured from response_item. Suppress to avoid double
            # counting wrapper/inner representations.
            return
        else:
            raise ValueError(f"unsupported completed item: {itype!r}")
    elif etype == "token_count":
        # Overlapping checkpoint of the same counters; never summed.
        return
    elif etype == "thread_settings_applied":
        return
    elif etype == "turn_aborted":
        # Interrupted turn: provisional account, never complete.
        cur.execute(
            "INSERT OR IGNORE INTO turns(turn_id, source_id) VALUES(?,?)",
            (p.get("turn_id"), source_id))
        cur.execute(
            "UPDATE turns SET completed_at=?, duration_ms=?, state='cancelled'"
            " WHERE turn_id=?",
            (_ts(obj), p.get("duration_ms"), p.get("turn_id")))
        _insert_event(cur, source_id, obj, stats, family="lifecycle",
                      native_id=f"turn_aborted:{p.get('turn_id')}",
                      turn_id=p.get("turn_id"), name="turn_aborted",
                      status="cancelled", duration_ms=p.get("duration_ms"),
                      detail={"reason": p.get("reason")})
    else:
        raise ValueError(f"unsupported event_msg type: {etype!r}")


def _ingest_command(cur: sqlite3.Cursor, source_id: int, obj: dict,
                    item: dict, stats: dict) -> None:
    cmd = item.get("command") or []
    output = item.get("stdout") or item.get("output") or ""
    size = len(str(output))
    turn_id = None
    _insert_event(cur, source_id, obj, stats, family="tool_result",
                  native_id=item.get("id"),
                  name=f"exec:{(cmd[-1] if cmd else '')[:120]}",
                  status=item.get("status"),
                  duration_ms=_duration(item),
                  size_bytes=size,
                  truncated=1 if item.get("truncated") else None,
                  fingerprint=_fingerprint(cmd),
                  detail={"command": cmd[:3] if isinstance(cmd, list) else cmd,
                          "cwd": item.get("cwd"),
                          "exit_code": item.get("exit_code"),
                          "parsed_cmd": item.get("parsed_cmd")})
    # Observed file reads come only from parsed_cmd entries, never mentions.
    for entry in item.get("parsed_cmd") or []:
        if isinstance(entry, dict) and entry.get("type") == "read":
            target = entry.get("path") or entry.get("name") or "unknown"
            fam = ("skill_read" if str(target).endswith("SKILL.md")
                   else "read")
            _insert_event(cur, source_id, obj, stats, family=fam,
                          native_id=f"{item.get('id')}:{target}",
                          name=target, status=item.get("status"),
                          duration_ms=_duration(item), size_bytes=size,
                          fingerprint=_fingerprint(target),
                          detail={"cmd": entry.get("cmd"),
                                  "observed_bytes": size,
                                  "agent_turn": turn_id,
                                  "evidence": "parsed_cmd"})


def _ingest_compacted(cur: sqlite3.Cursor, source_id: int, obj: dict,
                      stats: dict) -> None:
    p = obj["payload"]
    if "window_id" not in p:
        raise ValueError("compacted record missing window_id")
    stats["compactions"] += 1
    latest = p.get("latest_token_usage_record") or {}
    rid = latest.get("response_id")
    # The embedded latest usage repeats an already counted response and is
    # never inserted as a new row. Record whether its response_id resolves
    # to a known response so overlap stays checkable, not double counted.
    resolves = None
    if rid:
        resolves = cur.execute(
            "SELECT 1 FROM responses WHERE response_id=?", (rid,)).fetchone()
    _insert_event(cur, source_id, obj, stats, family="compaction",
                  native_id=p.get("window_id"), name="context_compaction",
                  detail={k: p.get(k) for k in (
                      "window_number", "first_window_id", "previous_window_id",
                      "window_id", "compaction_response_id")} | {
                      "latest_usage_response_id": rid,
                      "latest_usage_resolves": bool(resolves) if rid else None,
                      "overlap_rule": "embedded usage never summed"})
    return


def _insert_event(cur, source_id, obj, stats, family, native_id, name=None,
                  turn_id=None, status=None, duration_ms=None, size_bytes=None,
                  truncated=None, fingerprint=None, detail=None) -> None:
    if not native_id:
        raise ValueError(f"{family} event missing native identity")
    try:
        cur.execute(
            "INSERT OR IGNORE INTO events(source_id, ordinal_num, ts, family,"
            " native_id, turn_id, name, status, duration_ms, size_bytes,"
            " truncated, fingerprint, detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,"
            "?,?,?)",
            (source_id, obj.get("ordinal"), _ts(obj), family, str(native_id),
             turn_id, name, status, duration_ms, size_bytes, truncated,
             fingerprint,
             json.dumps(detail, sort_keys=True)[:4000] if detail else None))
    except sqlite3.IntegrityError:
        raise ValueError(f"duplicate event {family}:{native_id}")
    if cur.rowcount == 0:
        stats["events_duplicate"] += 1
    else:
        stats["events_inserted"] += 1


def _ts(obj: dict):
    import datetime
    t = obj.get("timestamp")
    if not t:
        return None
    try:
        return datetime.datetime.fromisoformat(
            str(t).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _duration(item: dict):
    d = item.get("duration") or item.get("duration_ms") or item.get("elapsed")
    if isinstance(d, (int, float)):
        return int(d * 1000) if d < 100000 and d != int(d) else int(d)
    return None
