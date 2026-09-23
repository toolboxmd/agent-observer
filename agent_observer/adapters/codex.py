"""Codex native adapter: session rollout JSONL into the ledger.

Reads persisted Codex rollouts (`~/.codex/sessions/**/rollout-*.jsonl`).
Counter rule: each token_usage_record usage block is one atomic response.
turn_token_usage and thread_token_usage are overlapping checkpoints and are
stored per row but never summed. The compaction latest_token_usage_record is
overlapping evidence of an existing response and is never inserted as a row.
In this schema cached input is a subset of input and reasoning is part of
output, so total_tokens = input_tokens + output_tokens.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sqlite3

from .. import db
from ..identity import SessionIdentity
from ..ingest import JsonlSource, fingerprint, insert_event, iso_ts, text_hash

HARNESS = "codex"
SEMANTICS = "codex:input_includes_cached,output_includes_reasoning"
SYNTHETIC_PREFIX = "<send_user_message_question_reply>"
DEFAULT_ROOT = os.path.expanduser("~/.codex/sessions")

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
    ("skill_file_reads", True, "read events under an installed Skill directory; mentions never count"),
    ("skill_invocation", False, "no native skill-invocation event observed in Codex sample; stays unknown"),
    ("timing_tool", True, "CommandExecution duration when reported; otherwise unknown, never invented"),
    ("instruction_identity", True, "AgentsMD direction block in injected messages and versioned plugin paths read"),
    ("quota", False, "rate-limit snapshots are account evidence, not task attribution; stays unknown at task level"),
]


def discover(root: str | None = None) -> list[str]:
    root = root or DEFAULT_ROOT
    return sorted(glob.glob(os.path.join(root, "**", "rollout-*.jsonl"),
                            recursive=True))


def sync(con: sqlite3.Connection, root: str | None = None, full: bool = False,
         source: str | None = None) -> dict:
    """Import every rollout under root, or one source file."""
    paths = [source] if source else discover(root)
    totals = {"harness": HARNESS, "sources": 0, "unchanged": 0,
              "responses_inserted": 0, "events_inserted": 0,
              "submissions_inserted": 0, "malformed": 0, "failed": []}
    for path in paths:
        try:
            stats = import_codex_file(con, path, full=full)
        except (OSError, sqlite3.DatabaseError) as exc:
            totals["failed"].append({"path": path, "error": str(exc)})
            continue
        totals["sources"] += 1
        totals["unchanged"] += 1 if stats.get("unchanged") else 0
        for key in ("responses_inserted", "events_inserted",
                    "submissions_inserted", "malformed"):
            totals[key] += stats.get(key, 0)
    return totals


def _get(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return default
    return d


def _text_of_message(payload: dict) -> str:
    parts = []
    for c in payload.get("content", []) or []:
        if isinstance(c, dict):
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
    text = _text_of_message(payload).strip()
    if not text or text.startswith(SYNTHETIC_PREFIX):
        return False
    return True


def _submission_kind(payload: dict) -> str:
    if is_genuine_submission(payload):
        return "genuine"
    text = _text_of_message(payload).strip()
    if text.startswith(SYNTHETIC_PREFIX):
        return "synthetic"
    return "scaffolding"


class _Reader:
    def __init__(self, con: sqlite3.Connection, src: JsonlSource, stats: dict):
        self.con = con
        self.src = src
        self.stats = stats
        self.session_id = src.row["session_id"]
        self.thread_id = src.row["thread_id"]
        self.cli_version = src.row["cli_version"]
        self.identity = SessionIdentity()
        self.model = None
        self.effort = None
        self.first_ts = None
        self.last_ts = None
        self.meta: dict = {}

    @property
    def session_key(self) -> str:
        native = self.thread_id or self.session_id
        if native:
            return f"{HARNESS}:{native}"
        return f"{HARNESS}:file:{os.path.basename(self.src.path)}"

    def event(self, obj, family, native_id, **kw):
        insert_event(self.con, self.stats, source_id=self.src.source_id,
                     session_key=self.session_key, family=family,
                     native_id=native_id, ordinal=obj.get("ordinal"),
                     ts=iso_ts(obj.get("timestamp")), **kw)


def import_codex_file(con: sqlite3.Connection, path: str,
                      full: bool = False) -> dict:
    """Import one Codex rollout. Idempotent; growing logs update in place.

    Malformed lines are recorded in import_errors and skipped. Previously
    valid data is never destroyed.
    """
    stats = {
        "lines": 0, "responses_inserted": 0, "responses_duplicate": 0,
        "submissions_inserted": 0, "events_inserted": 0,
        "events_duplicate": 0, "compactions": 0, "malformed": 0,
    }
    src = JsonlSource(con, HARNESS, path, full=full)
    reader = _Reader(con, src, stats)
    for ordinal, obj, line in src.records():
        stats["lines"] += 1
        if obj is None:
            stats["malformed"] += 1
            src.error(ordinal, "json_error", line)
            continue
        try:
            _ingest_record(reader, obj)
        except (KeyError, TypeError, ValueError) as exc:
            stats["malformed"] += 1
            src.error(obj.get("ordinal", ordinal), f"schema_error: {exc}", line)
            continue
        ts = iso_ts(obj.get("timestamp"))
        if ts is not None:
            reader.first_ts = ts if reader.first_ts is None else min(reader.first_ts, ts)
            reader.last_ts = ts if reader.last_ts is None else max(reader.last_ts, ts)
    fields = {"started_at": reader.first_ts, "ended_at": reader.last_ts,
              **reader.meta, **reader.identity.fields(con)}
    db.upsert_session(con, reader.session_key, HARNESS,
                      reader.session_key.split(":", 1)[1], src.source_id,
                      **fields)
    stats.update(src.finish(session_id=reader.session_id,
                            thread_id=reader.thread_id,
                            cli_version=reader.cli_version))
    stats["session_key"] = reader.session_key
    con.commit()
    return stats


def _ingest_record(r: _Reader, obj: dict) -> None:
    rtype = obj.get("type")
    if rtype not in ("session_meta", "event_msg", "response_item",
                     "token_usage_record", "turn_context", "compacted",
                     "world_state", "inter_agent_communication_metadata"):
        raise ValueError(f"unsupported record type: {rtype!r}")
    payload = obj.get("payload", {}) if isinstance(obj, dict) else {}
    if rtype == "session_meta":
        r.session_id = payload.get("session_id") or payload.get("id") or r.session_id
        r.thread_id = payload.get("id") or r.thread_id
        r.cli_version = payload.get("cli_version") or r.cli_version
        git = payload.get("git") or {}
        r.meta.update({k: v for k, v in {
            "project_dir": payload.get("cwd"),
            "git_branch": git.get("branch") if isinstance(git, dict) else None,
            "client_version": payload.get("cli_version"),
            "entrypoint": payload.get("originator"),
        }.items() if v})
        return
    if isinstance(payload, dict):
        if payload.get("thread_id") and r.thread_id is None:
            r.thread_id = payload["thread_id"]
        if payload.get("session_id") and r.session_id is None:
            r.session_id = payload["session_id"]
    if rtype == "token_usage_record":
        _ingest_usage(r, obj)
    elif rtype == "response_item":
        _ingest_response_item(r, obj)
    elif rtype == "turn_context":
        _ingest_turn(r, obj)
    elif rtype == "event_msg":
        _ingest_event_msg(r, obj)
    elif rtype == "compacted":
        _ingest_compacted(r, obj)
    # world_state and inter_agent_communication_metadata carry no ledger rows
    # beyond source metadata; they stay private in the raw file.


def _ingest_usage(r: _Reader, obj: dict) -> None:
    p = obj["payload"]
    for k in ("response_id", "usage"):
        if k not in p:
            raise ValueError(f"token_usage_record missing {k}")
    u = p["usage"] or {}
    tt = p.get("turn_token_usage") or {}
    th = p.get("thread_token_usage") or {}
    rid = f"{HARNESS}:{p['response_id']}"
    turn_id = f"{HARNESS}:{p['turn_id']}" if p.get("turn_id") else None
    cur = r.con.execute(
        "INSERT OR IGNORE INTO responses(response_id, source_id, harness,"
        " session_key, thread_id, turn_id, root_turn_id, session_id,"
        " ordinal_num, ts, model, effort, input_tokens, cached_input_tokens,"
        " cache_write_input_tokens, output_tokens, reasoning_output_tokens,"
        " total_tokens, turn_total_tokens, thread_total_tokens, semantics)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, r.src.source_id, HARNESS, r.session_key, p.get("thread_id"),
         turn_id, p.get("root_turn_id"), p.get("session_id"),
         obj.get("ordinal"), iso_ts(obj.get("timestamp")), r.model, r.effort,
         u.get("input_tokens"), u.get("cached_input_tokens"),
         u.get("cache_write_input_tokens"), u.get("output_tokens"),
         u.get("reasoning_output_tokens"), u.get("total_tokens"),
         tt.get("total_tokens"), th.get("total_tokens"), SEMANTICS))
    if cur.rowcount == 0:
        # Same response_id seen again: verify counters agree, never re-sum.
        existing = r.con.execute(
            "SELECT total_tokens FROM responses WHERE response_id=?",
            (rid,)).fetchone()
        if existing is None or existing["total_tokens"] != u.get("total_tokens"):
            raise ValueError(f"conflicting usage for {p['response_id']}")
        r.stats["responses_duplicate"] += 1
    else:
        r.stats["responses_inserted"] += 1
    if turn_id:
        r.con.execute(
            "INSERT OR IGNORE INTO turns(turn_id, source_id, session_key,"
            " root_turn_id, session_id) VALUES(?,?,?,?,?)",
            (turn_id, r.src.source_id, r.session_key, p.get("root_turn_id"),
             p.get("session_id")))


def _ingest_turn(r: _Reader, obj: dict) -> None:
    p = obj["payload"]
    r.model = p.get("model") or r.model
    r.effort = p.get("effort") or r.effort
    if p.get("cwd") and "project_dir" not in r.meta:
        r.meta["project_dir"] = p["cwd"]
    if "turn_id" not in p:
        # Older rollouts carry configuration without a turn identity.
        return
    turn_id = f"{HARNESS}:{p['turn_id']}"
    r.con.execute(
        "INSERT OR IGNORE INTO turns(turn_id, source_id, session_key,"
        " root_turn_id, session_id, model_observed, effort_observed)"
        " VALUES(?,?,?,?,?,?,?)",
        (turn_id, r.src.source_id, r.session_key, p.get("root_turn_id"),
         p.get("session_id"), p.get("model"), p.get("effort")))
    r.con.execute(
        "UPDATE turns SET model_observed=COALESCE(model_observed, ?), "
        "effort_observed=COALESCE(effort_observed, ?) WHERE turn_id=?",
        (p.get("model"), p.get("effort"), turn_id))


def _ingest_response_item(r: _Reader, obj: dict) -> None:
    p = obj["payload"]
    ptype = p.get("type")
    raw_turn = (p.get("internal_chat_message_metadata_passthrough") or {}).get(
        "turn_id")
    turn_id = f"{HARNESS}:{raw_turn}" if raw_turn else None
    if turn_id:
        r.con.execute(
            "INSERT OR IGNORE INTO turns(turn_id, source_id, session_key)"
            " VALUES(?,?,?)", (turn_id, r.src.source_id, r.session_key))
    if ptype == "message" and p.get("role") in ("user", "developer"):
        text = _text_of_message(p)
        r.identity.observe_text(text)
        if p.get("role") != "user":
            return
        kind = _submission_kind(p)
        native = p.get("id") or f"ordinal:{obj.get('ordinal')}"
        cur = r.con.execute(
            "INSERT OR IGNORE INTO submissions(native_id, source_id,"
            " session_key, turn_id, ordinal_num, ts, kind, text_hash,"
            " text_excerpt, is_genuine) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"{HARNESS}:{native}", r.src.source_id, r.session_key, turn_id,
             obj.get("ordinal"), iso_ts(obj.get("timestamp")), kind,
             text_hash(text), text[:300], 1 if kind == "genuine" else 0))
        if cur.rowcount:
            r.stats["submissions_inserted"] += 1
        return
    if ptype == "message" and p.get("role") == "assistant":
        text = _text_of_message(p)
        if text:
            r.event(obj, "assistant_message", p.get("id") or f"ordinal:{obj.get('ordinal')}",
                    turn_id=turn_id, name="assistant_message",
                    size_bytes=len(text), fingerprint=text_hash(text),
                    detail={"excerpt": text[-400:]})
        return
    if ptype in ("function_call", "custom_tool_call"):
        call_id = p.get("call_id")
        name = p.get("name") or (
            p.get("custom_tool_call") or {}).get("name", "unknown")
        args = p.get("arguments") if ptype == "function_call" else p.get("input")
        r.event(obj, "tool_call", call_id or p.get("id"), turn_id=turn_id,
                name=f"{p.get('namespace') + '.' if p.get('namespace') else ''}{name}",
                status=p.get("status"),
                fingerprint=fingerprint(name, str(args)[:2000]),
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
        r.event(obj, "tool_result", call_id or p.get("id"), turn_id=turn_id,
                name=ptype, size_bytes=size,
                detail={"kind": ptype, "item_id": p.get("id")})


def _ingest_event_msg(r: _Reader, obj: dict) -> None:
    p = obj["payload"]
    etype = p.get("type")
    turn_id = f"{HARNESS}:{p['turn_id']}" if p.get("turn_id") else None
    if etype == "task_started":
        if turn_id:
            r.con.execute(
                "INSERT OR IGNORE INTO turns(turn_id, source_id, session_key)"
                " VALUES(?,?,?)", (turn_id, r.src.source_id, r.session_key))
            r.con.execute(
                "UPDATE turns SET started_at=COALESCE(started_at, ?)"
                " WHERE turn_id=?", (iso_ts(obj.get("timestamp")), turn_id))
    elif etype == "task_complete":
        if turn_id:
            r.con.execute(
                "INSERT OR IGNORE INTO turns(turn_id, source_id, session_key)"
                " VALUES(?,?,?)", (turn_id, r.src.source_id, r.session_key))
            r.con.execute(
                "UPDATE turns SET completed_at=?, duration_ms=?, state='complete'"
                " WHERE turn_id=?",
                (iso_ts(obj.get("timestamp")), p.get("duration_ms"), turn_id))
        r.event(obj, "lifecycle", f"task_complete:{p.get('turn_id')}",
                turn_id=turn_id, name="task_complete", status="completed",
                duration_ms=p.get("duration_ms"),
                detail={"time_to_first_token_ms": p.get("time_to_first_token_ms")})
    elif etype == "item_completed":
        item = p.get("item", {}) or {}
        itype = item.get("type")
        if itype == "CommandExecution":
            _ingest_command(r, obj, item, turn_id)
        elif itype == "McpToolCall":
            result = item.get("result") or {}
            content = result.get("content") or []
            size = sum(len(str(c.get("text", ""))) for c in content
                       if isinstance(c, dict))
            r.event(obj, "tool_result", item.get("id"), turn_id=turn_id,
                    name=f"mcp.{item.get('server')}.{item.get('tool')}",
                    status=item.get("status"), size_bytes=size,
                    detail={"call_id_match": "exact native id retained; "
                            "join to calls only on equal call_id"})
        elif itype == "SubAgentActivity":
            r.event(obj, "lifecycle", item.get("id"), turn_id=turn_id,
                    name="subagent_activity", status=item.get("kind"),
                    detail={"agent_thread_id": item.get("agent_thread_id"),
                            "agent_path": item.get("agent_path")})
        elif itype == "FileChange":
            # Paths and change kinds are reread evidence; contents stay out.
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
            r.event(obj, "file_change", item.get("id"), turn_id=turn_id,
                    name="file_change",
                    target=next(iter(sorted(summary)), None),
                    fingerprint=fingerprint(sorted(summary)),
                    detail={"paths": summary})
        elif itype == "ContextCompaction":
            # Sparse native compaction marker: boundary identity only.
            r.stats["compactions"] += 1
            r.event(obj, "compaction", item.get("id"),
                    turn_id=f"{HARNESS}:{item['turn_id']}" if item.get("turn_id") else turn_id,
                    name="context_compaction",
                    detail={"native": "ContextCompaction marker; "
                            "window detail unknown"})
        elif itype == "CollabAgentToolCall":
            # Sub-agent collaboration (spawn, send, wait): the dispatch edge
            # between threads, kept with its native thread identities.
            r.event(obj, "lifecycle", item.get("id"), turn_id=turn_id,
                    name=f"collab.{item.get('tool') or 'unknown'}",
                    status=item.get("status"),
                    detail={"sender_thread_id": item.get("sender_thread_id"),
                            "receiver_thread_ids": item.get("receiver_thread_ids"),
                            "receiver_agents": item.get("receiver_agents")})
        elif itype in ("ImageView", "WebSearch", "DynamicToolCall",
                       "FunctionCallOutput"):
            target = item.get("path") or item.get("query") or item.get("tool")
            r.event(obj, "tool_result", item.get("call_id") or item.get("id"),
                    turn_id=turn_id,
                    name={"ImageView": "image_view", "WebSearch": "web_search",
                          "FunctionCallOutput": "function_call_output"}.get(
                              itype, f"dynamic.{item.get('tool') or 'unknown'}"),
                    target=str(target)[:500] if target else None,
                    status=item.get("status"),
                    detail={"kind": itype})
        elif itype in ("Plan", "HookPrompt", "EnteredReviewMode",
                       "ExitedReviewMode"):
            r.event(obj, "lifecycle", item.get("id") or f"{itype}:{obj.get('ordinal')}",
                    turn_id=turn_id, name=itype, status=item.get("status"))
        elif itype in ("UserMessage", "AgentMessage", "Reasoning", "Extension"):
            # Content payload, not an operational event; captured from
            # response_item to avoid double counting wrapper and inner forms.
            return
        else:
            raise ValueError(f"unsupported completed item: {itype!r}")
    elif etype == "thread_goal_updated":
        r.event(obj, "lifecycle", f"thread_goal_updated:{obj.get('ordinal')}",
                turn_id=turn_id, name="thread_goal_updated")
    elif etype in ("token_count", "thread_settings_applied"):
        # Overlapping checkpoint or configuration echo; never summed.
        return
    elif etype == "turn_aborted":
        # Interrupted turn: provisional account, never complete.
        if turn_id:
            r.con.execute(
                "INSERT OR IGNORE INTO turns(turn_id, source_id, session_key)"
                " VALUES(?,?,?)", (turn_id, r.src.source_id, r.session_key))
            r.con.execute(
                "UPDATE turns SET completed_at=?, duration_ms=?, state='cancelled'"
                " WHERE turn_id=?",
                (iso_ts(obj.get("timestamp")), p.get("duration_ms"), turn_id))
        r.event(obj, "lifecycle", f"turn_aborted:{p.get('turn_id')}",
                turn_id=turn_id, name="turn_aborted", status="cancelled",
                duration_ms=p.get("duration_ms"),
                detail={"reason": p.get("reason")})
    else:
        raise ValueError(f"unsupported event_msg type: {etype!r}")


def _ingest_command(r: _Reader, obj: dict, item: dict, turn_id) -> None:
    cmd = item.get("command") or []
    output = item.get("stdout") or item.get("output") or ""
    size = len(str(output))
    shown = cmd[-1] if isinstance(cmd, list) and cmd else str(cmd)
    r.event(obj, "tool_result", item.get("id"), turn_id=turn_id,
            name="exec", target=str(shown)[:500],
            status=item.get("status"), duration_ms=_duration(item),
            size_bytes=size,
            truncated=1 if item.get("truncated") else None,
            fingerprint=fingerprint(cmd),
            detail={"command": cmd[:3] if isinstance(cmd, list) else cmd,
                    "cwd": item.get("cwd"),
                    "exit_code": item.get("exit_code"),
                    "parsed_cmd": item.get("parsed_cmd")})
    # Observed file reads come only from parsed_cmd entries, never mentions.
    for entry in item.get("parsed_cmd") or []:
        if isinstance(entry, dict) and entry.get("type") == "read":
            target = str(entry.get("path") or entry.get("name") or "unknown")
            r.identity.observe_path(target)
            fam = "skill_read" if (target.endswith("SKILL.md")
                                   or "/skills/" in target) else "read"
            r.event(obj, fam, f"{item.get('id')}:{target}", turn_id=turn_id,
                    name=os.path.basename(target), target=target,
                    status=item.get("status"),
                    duration_ms=_duration(item), size_bytes=size,
                    fingerprint=fingerprint(target),
                    detail={"cmd": entry.get("cmd"), "observed_bytes": size,
                            "evidence": "parsed_cmd"})


def _ingest_compacted(r: _Reader, obj: dict) -> None:
    p = obj["payload"]
    r.stats["compactions"] += 1
    if "window_id" not in p:
        # Older rollouts record a compaction with its replacement history
        # but no window identity; the boundary is kept by position.
        r.event(obj, "compaction", f"compacted:{obj.get('ordinal')}",
                name="context_compaction",
                detail={"native": "compacted record without window identity",
                        "replacement_items": len(p.get("replacement_history") or [])})
        return
    latest = p.get("latest_token_usage_record") or {}
    rid = latest.get("response_id")
    # The embedded latest usage repeats an already counted response and is
    # never inserted. Record whether it resolves so overlap stays checkable.
    resolves = None
    if rid:
        resolves = r.con.execute(
            "SELECT 1 FROM responses WHERE response_id=?",
            (f"{HARNESS}:{rid}",)).fetchone()
    r.event(obj, "compaction", p.get("window_id"), name="context_compaction",
            detail={k: p.get(k) for k in (
                "window_number", "first_window_id", "previous_window_id",
                "window_id", "compaction_response_id")} | {
                "latest_usage_response_id": rid,
                "latest_usage_resolves": bool(resolves) if rid else None,
                "overlap_rule": "embedded usage never summed"})


def _duration(item: dict):
    d = item.get("duration") or item.get("duration_ms") or item.get("elapsed")
    if isinstance(d, (int, float)):
        return int(d * 1000) if d < 100000 and d != int(d) else int(d)
    return None
