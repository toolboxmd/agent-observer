"""Grok Build native adapter: session directories into the ledger.

Reads `~/.grok/sessions/<url-encoded cwd>/<session id>/` directories:
`updates.jsonl` (session/update notifications), `events.jsonl` (turn, tool
and permission lifecycle), `chat_history.jsonl` (prompt texts and synthetic
marks) and `summary.json` (project, branch, model). `usage.json` holds
session totals for cross-checking only and is never imported.

Counter rule: exactly one responses row per turn_completed, keyed
`grok:<session id>:<prompt_id>`, holding the harness's own per-prompt
totals. Input includes cached reads and output includes reasoning, so raw
buckets are never added across harnesses and no total is derived when the
native totalTokens is unknown. Per-inference records (`unified.jsonl` under
`~/.grok/logs`) and `usage.json` session totals are never imported on top:
that would double count.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.parse

from .. import db
from ..identity import SessionIdentity, skill_from_path
from ..ingest import JsonlSource, fingerprint, insert_event, iso_ts, text_hash

HARNESS = "grok"
SEMANTICS = "grok:input_includes_cached,output_includes_reasoning"
DEFAULT_ROOT = os.path.expanduser("~/.grok/sessions")

CAPABILITIES = [
    ("model_usage", True, "one responses row per turn_completed prompt_id; input includes cached reads, output includes reasoning"),
    ("tool_calls", True, "tool_call records with title, safe target and argument fingerprint; raw input never stored"),
    ("tool_results", True, "tool_call_update status joined with events.jsonl tool_completed duration and outcome on toolCallId"),
    ("read_evidence", True, "tool_call_update kind=read locations as read events with paths and line metadata"),
    ("skill_file_reads", True, "reads under an installed Skill directory as skill_read with the skill name"),
    ("skill_invocation", False, "no native skill-invocation event observed in Grok records; stays unknown"),
    ("compaction", True, "auto_compact_started and auto_compact_completed as compaction events"),
    ("lifecycle_task", True, "turn_started/turn_ended, retry/subagent/task/hook/recap extension kinds; phase_changed skipped"),
    ("human_input", True, "user_message_chunk prompts per promptIndex; synthetic marks from chat_history"),
    ("instruction_identity", True, "direction block and <user_rule> bodies from prompt texts; read paths observed"),
    ("subagents", True, "subagent_spawned links the child session row to its parent"),
]

METHODS = ("session/update", "_x.ai/session/update")
EXTENSION_KINDS = frozenset({
    "retry_state", "subagent_spawned", "subagent_finished",
    "task_backgrounded", "task_completed", "auto_compact_started",
    "auto_compact_completed", "compaction_checkpoint", "hook_execution",
    "session_recap", "plan", "background_tasks", "image_compressed",
    "current_mode_update", "rewind_marker",
})
COMPACTION_KINDS = frozenset({"auto_compact_started", "auto_compact_completed"})
TERMINAL_TOOL_STATUS = {"completed": "ok", "failed": "error", "success": "ok"}
# High-volume run-loop noise with no task evidence; never stored.
SKIP_EVENT_TYPES = frozenset({"phase_changed", "loop_started", "first_token"})

USER_RULE_RE = re.compile(r"<user_rule>(.*?)</user_rule>", re.S)
TARGET_KEYS = ("target_file", "path", "file_path", "file", "command",
               "url", "pattern")
EXCERPT_LEN = 300


def discover(root: str | None = None) -> list[str]:
    """Session directories under `<url-encoded cwd>/<session id>`."""
    root = root or DEFAULT_ROOT
    if _looks_like_session(root):
        return [root]
    try:
        groups = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for group in groups:
        group_dir = os.path.join(root, group)
        if not os.path.isdir(group_dir):
            continue
        try:
            children = sorted(os.listdir(group_dir))
        except OSError:
            continue
        for child in children:
            session_dir = os.path.join(group_dir, child)
            if os.path.isdir(session_dir) and _looks_like_session(session_dir):
                out.append(session_dir)
    return out


def _looks_like_session(path: str) -> bool:
    return any(os.path.isfile(os.path.join(path, name)) for name in
               ("updates.jsonl", "events.jsonl", "summary.json",
                "chat_history.jsonl"))


def sync(con: sqlite3.Connection, root: str | None = None, full: bool = False,
         source: str | None = None) -> dict:
    """Import every session under root, or one session directory."""
    paths = _resolve_source(source, root) if source else discover(root)
    totals = {"harness": HARNESS, "sources": 0, "unchanged": 0,
              "responses_inserted": 0, "events_inserted": 0,
              "submissions_inserted": 0, "malformed": 0, "failed": []}
    for path in paths:
        try:
            stats = import_grok_session(con, path, full=full)
        except (OSError, sqlite3.DatabaseError, UnicodeDecodeError) as exc:
            totals["failed"].append({"path": path, "error": str(exc)})
            continue
        totals["sources"] += 1
        totals["unchanged"] += 1 if stats.get("unchanged") else 0
        for key in ("responses_inserted", "events_inserted",
                    "submissions_inserted", "malformed"):
            totals[key] += stats.get(key, 0)
    return totals


def _resolve_source(source: str, root: str | None) -> list[str]:
    if os.path.isdir(source):
        if _looks_like_session(source):
            return [source]
        # A <url-encoded cwd> group directory: every session beneath it.
        try:
            children = sorted(os.listdir(source))
        except OSError:
            return [source]
        sessions = [os.path.join(source, c) for c in children
                    if os.path.isdir(os.path.join(source, c))
                    and _looks_like_session(os.path.join(source, c))]
        return sessions or [source]
    # A single file inside a session directory.
    parent = os.path.dirname(os.path.abspath(source))
    if _looks_like_session(parent):
        return [parent]
    return discover(root)


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(content, list):
        return "".join(_content_text(c) for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _safe_target(raw) -> str | None:
    if not isinstance(raw, dict):
        return None
    for key in TARGET_KEYS:
        value = raw.get(key)
        if value:
            return str(value)[:500]
    return None


def _scalars(obj: dict) -> dict:
    """Top-level scalar fields only; nested tool output never survives.

    Free text is bounded: lifecycle detail keeps evidence, never contents.
    """
    out = {}
    for key, value in obj.items():
        if isinstance(value, str):
            out[key] = value[:300]
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
    return out


class _Reader:
    def __init__(self, con, session_key, native_sid, stats):
        self.con = con
        self.session_key = session_key
        self.native_sid = native_sid
        self.stats = stats
        self.identity = SessionIdentity()
        self.first_ts = None
        self.last_ts = None
        self.meta: dict = {}
        self.parent_key = None
        self.updates_src = None
        self.events_src = None

    def note_ts(self, ts) -> None:
        if ts is None:
            return
        self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
        self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

    def event(self, src: JsonlSource, family, native_id, ordinal, ts, **kw):
        insert_event(self.con, self.stats, source_id=src.source_id,
                     session_key=self.session_key, family=family,
                     native_id=native_id, ordinal=ordinal, ts=ts, **kw)


def import_grok_session(con: sqlite3.Connection, session_dir: str,
                        full: bool = False) -> dict:
    """Import one Grok session directory. Idempotent; growing logs resume."""
    stats = {"lines": 0, "responses_inserted": 0, "responses_duplicate": 0,
             "submissions_inserted": 0, "events_inserted": 0,
             "events_duplicate": 0, "compactions": 0, "malformed": 0}
    updates_path = os.path.join(session_dir, "updates.jsonl")
    events_path = os.path.join(session_dir, "events.jsonl")
    r = _Reader(con, f"{HARNESS}:{os.path.basename(session_dir.rstrip(os.sep))}",
                os.path.basename(session_dir.rstrip(os.sep)), stats)

    summary = _read_json(os.path.join(session_dir, "summary.json")) or {}
    info = summary.get("info") if isinstance(summary.get("info"), dict) else {}
    if info.get("id"):
        r.native_sid = str(info["id"])
        r.session_key = f"{HARNESS}:{r.native_sid}"
    group_name = os.path.basename(os.path.dirname(session_dir.rstrip(os.sep)))
    fallback_dir = urllib.parse.unquote(group_name)
    project_dir = (summary.get("git_root_dir") or info.get("cwd")
                   or fallback_dir or None)
    if isinstance(project_dir, str):
        project_dir = project_dir.rstrip("/") or None
    if summary.get("head_branch"):
        r.meta["git_branch"] = str(summary["head_branch"])
    if project_dir:
        r.meta["project_dir"] = str(project_dir)
    if summary.get("session_kind") == "subagent":
        r.meta["role"] = "subagent"
    model_fallback = summary.get("current_model_id")
    effort = summary.get("reasoning_effort")
    for key in ("created_at", "last_active_at", "updated_at"):
        ts = iso_ts(summary.get(key))
        if ts is not None:
            r.note_ts(ts)

    updates_src = (JsonlSource(con, HARNESS, updates_path, full=full)
                   if os.path.isfile(updates_path) else None)
    events_src = (JsonlSource(con, HARNESS, events_path, full=full)
                  if os.path.isfile(events_path) else None)
    r.updates_src = updates_src
    r.events_src = events_src
    known = con.execute("SELECT 1 FROM sessions WHERE session_key=?",
                        (r.session_key,)).fetchone()
    updates_new = full or updates_src is None or not updates_src.unchanged
    events_new = full or events_src is None or not events_src.unchanged
    if known and not updates_new and not events_new:
        stats["unchanged"] = True
        stats["session_key"] = r.session_key
        return stats

    chat = _read_chat(session_dir, r)
    if updates_src is not None and (updates_new or not known):
        _replay_updates(con, r, updates_src, chat, model_fallback, effort)
        for ordinal, obj, line in updates_src.records():
            stats["lines"] += 1
            if obj is None or not isinstance(obj, dict):
                stats["malformed"] += 1
                updates_src.error(ordinal, "json_error", line)
                continue
            try:
                _ingest_update(r, updates_src, obj, ordinal)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                stats["malformed"] += 1
                updates_src.error(ordinal, f"schema_error: {exc}", line)
    if events_src is not None and (events_new or not known):
        for ordinal, obj, line in events_src.records():
            stats["lines"] += 1
            if obj is None or not isinstance(obj, dict):
                stats["malformed"] += 1
                events_src.error(ordinal, "json_error", line)
                continue
            try:
                _ingest_event(r, events_src, obj, ordinal)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                stats["malformed"] += 1
                events_src.error(ordinal, f"schema_error: {exc}", line)

    fields = {"started_at": r.first_ts, "ended_at": r.last_ts, **r.meta,
              **r.identity.fields(con)}
    if r.parent_key:
        fields["parent_session_key"] = r.parent_key
    db.upsert_session(con, r.session_key, HARNESS, r.native_sid,
                      updates_src.source_id if updates_src is not None
                      else (events_src.source_id if events_src is not None
                            else None), **fields)
    if updates_src is not None:
        stats.update(updates_src.finish(session_id=r.native_sid))
    if events_src is not None:
        stats.update(events_src.finish(session_id=r.native_sid))
    stats["session_key"] = r.session_key
    stats["unchanged"] = bool(
        (updates_src is None or updates_src.unchanged)
        and (events_src is None or events_src.unchanged)) and not full
    con.commit()
    return stats


def _read_chat(session_dir: str, r: _Reader) -> dict:
    """Identity evidence and per-prompt synthetic marks from chat_history.

    The file is read-only metadata: prompt texts feed the instruction
    identity, never the ledger, and only the prompt_index marks survive.
    """
    chat = {"synthetic": set(), "effort": None}
    path = os.path.join(session_dir, "chat_history.jsonl")
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return chat
    with fh:
        for raw in fh:
            if not raw.endswith("\n") or not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            text = _content_text(obj.get("content"))
            if kind in ("user", "system") and text:
                r.identity.observe_text(text)
                for body in USER_RULE_RE.findall(text):
                    r.identity.observe_loaded_instructions(body.strip())
            if kind == "user" and obj.get("prompt_index") is not None \
                    and obj.get("synthetic_reason"):
                chat["synthetic"].add(str(obj["prompt_index"]))
            if kind == "assistant" and obj.get("reasoning_effort") \
                    and chat["effort"] is None:
                chat["effort"] = obj["reasoning_effort"]
    return chat


def _complete_lines(path: str):
    """(ordinal, text) for every non-blank complete line from the start.

    Mirrors JsonlSource: a trailing line without a newline is still being
    written, so it is left for the next import. Ordinals count non-blank
    lines from the start of the file, so they are stable across syncs.
    """
    ordinal = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if not raw.endswith("\n"):
                break
            if raw.strip():
                yield ordinal, raw
                ordinal += 1


def _turn_models(events_path: str | None) -> list:
    """(ts, model_id) of every turn_started, oldest first."""
    models = []
    if not events_path or not os.path.isfile(events_path):
        return models
    for _, raw in _complete_lines(events_path):
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "turn_started" \
                and obj.get("model_id"):
            ts = iso_ts(obj.get("ts"))
            if ts is not None:
                models.append((ts, str(obj["model_id"])))
    models.sort()
    return models


def _model_at(ts, turn_models: list, *fallbacks):
    model = None
    for started, name in turn_models:
        if ts is not None and started <= ts:
            model = name
    if model is None:
        for candidate in fallbacks:
            if candidate:
                return candidate
    return model


def _replay_updates(con, r: _Reader, src: JsonlSource, chat: dict,
                    model_fallback, effort) -> None:
    """Rebuild prompts and per-prompt usage from the whole updates.jsonl.

    A growing file can extend an open prompt or finalize it on a later sync,
    but the schema keeps only the text hash and a bounded excerpt, so the
    full text is reconstructed here on every sync that saw new bytes. Every
    insert is under a natural key, so the replay never duplicates rows.
    """
    prompts: dict = {}
    order: list = []
    completions: list = []
    for ordinal, raw in _complete_lines(src.path):
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        params = obj.get("params") if isinstance(obj, dict) else None
        update = params.get("update") if isinstance(params, dict) else None
        if not isinstance(update, dict):
            continue
        kind = update.get("sessionUpdate")
        if kind == "user_message_chunk":
            meta = update.get("_meta") if isinstance(
                update.get("_meta"), dict) else {}
            pidx = meta.get("promptIndex")
            if pidx is None:
                r.stats["malformed"] += 1
                src.error(ordinal, "user chunk without promptIndex",
                          raw)
                continue
            key = str(pidx)
            entry = prompts.get(key)
            if entry is None:
                entry = {"texts": [], "model": None, "first": ordinal,
                         "ts": iso_ts(obj.get("timestamp"))}
                prompts[key] = entry
                order.append(key)
            entry["texts"].append(_content_text(update.get("content")))
            if entry["model"] is None and meta.get("modelId"):
                entry["model"] = str(meta["modelId"])
        elif kind == "turn_completed":
            if not update.get("prompt_id"):
                r.stats["malformed"] += 1
                src.error(ordinal, "turn_completed without prompt_id",
                          raw)
                continue
            completions.append((ordinal, update, iso_ts(obj.get("timestamp"))))
    turn_models = _turn_models(
        os.path.join(os.path.dirname(src.path), "events.jsonl"))
    for position, key in enumerate(order):
        entry = prompts[key]
        text = "".join(entry["texts"])
        prompt_id = (completions[position][1].get("prompt_id")
                     if position < len(completions) else None)
        turn_id = (f"{HARNESS}:{r.native_sid}:{prompt_id}"
                   if prompt_id else None)
        kind = "synthetic" if key in chat["synthetic"] else "genuine"
        native_id = f"{HARNESS}:{r.native_sid}:prompt:{key}"
        existing = con.execute(
            "SELECT text_hash, text_excerpt, turn_id FROM submissions"
            " WHERE native_id=?", (native_id,)).fetchone()
        if existing is None:
            cur = con.execute(
                "INSERT OR IGNORE INTO submissions(native_id, source_id,"
                " session_key, turn_id, ordinal_num, ts, kind, text_hash,"
                " text_excerpt, is_genuine) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (native_id, src.source_id, r.session_key, turn_id,
                 entry["first"], entry["ts"], kind, text_hash(text),
                 text[:EXCERPT_LEN], 1 if kind == "genuine" else 0))
            if cur.rowcount:
                r.stats["submissions_inserted"] += 1
        else:
            # A growing file extends the text or finalizes the prompt: the
            # stored hash follows appended text, never a divergent rewrite.
            if text_hash(text) != existing["text_hash"]:
                if text.startswith(existing["text_excerpt"] or ""):
                    con.execute(
                        "UPDATE submissions SET text_hash=?, text_excerpt=?"
                        " WHERE native_id=?",
                        (text_hash(text), text[:EXCERPT_LEN], native_id))
                else:
                    r.stats["malformed"] += 1
                    src.error(entry["first"],
                              f"conflicting prompt text for {native_id}",
                              text[:200])
            if turn_id and not existing["turn_id"]:
                con.execute("UPDATE submissions SET turn_id=? WHERE native_id=?",
                            (turn_id, native_id))
    for position, (ordinal, update, ts) in enumerate(completions):
        chunk_model = (prompts[order[position]]["model"]
                       if position < len(order) else None)
        _store_response(con, r, src, ordinal, update, ts,
                        _usage_model(update), model_fallback, chunk_model,
                        effort or chat["effort"], turn_models)


def _usage_model(update: dict):
    usage = update.get("usage")
    if isinstance(usage, dict):
        model_usage = usage.get("modelUsage")
        if isinstance(model_usage, dict) and len(model_usage) == 1:
            return next(iter(model_usage))
    return None


def _store_response(con, r: _Reader, src: JsonlSource, ordinal: int,
                    update: dict, ts, usage_model, summary_model, chunk_model,
                    effort, turn_models) -> None:
    prompt_id = update.get("prompt_id")
    response_id = f"{HARNESS}:{r.native_sid}:{prompt_id}"
    usage = update.get("usage") if isinstance(update.get("usage"), dict) \
        else {}
    model = _model_at(ts, turn_models, usage_model, summary_model,
                      chunk_model)
    counters = {key: usage.get(native) if isinstance(usage.get(native), int)
                else None for key, native in
                (("input_tokens", "inputTokens"),
                 ("cached_input_tokens", "cachedReadTokens"),
                 ("cache_write_input_tokens", "cacheCreationTokens"),
                 ("output_tokens", "outputTokens"),
                 ("reasoning_output_tokens", "reasoningTokens"),
                 ("total_tokens", "totalTokens"))}
    cur = con.execute(
        "INSERT OR IGNORE INTO responses(response_id, source_id, harness,"
        " session_key, turn_id, session_id, ordinal_num, ts, model, effort,"
        " input_tokens, cached_input_tokens, cache_write_input_tokens,"
        " output_tokens, reasoning_output_tokens, total_tokens, semantics)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (response_id, src.source_id, HARNESS, r.session_key, response_id,
         r.native_sid, ordinal, ts, model, effort, counters["input_tokens"],
         counters["cached_input_tokens"],
         counters["cache_write_input_tokens"], counters["output_tokens"],
         counters["reasoning_output_tokens"], counters["total_tokens"],
         SEMANTICS))
    if cur.rowcount:
        r.stats["responses_inserted"] += 1
        return
    existing = con.execute(
        "SELECT input_tokens, cached_input_tokens, cache_write_input_tokens,"
        " output_tokens, reasoning_output_tokens, total_tokens FROM responses"
        " WHERE response_id=?", (response_id,)).fetchone()
    if existing is None:
        r.stats["responses_duplicate"] += 1
        return
    for key in counters:
        old, new = existing[key], counters[key]
        if old is not None and new is not None and old != new:
            r.stats["malformed"] += 1
            src.error(ordinal, f"conflicting usage for {prompt_id}",
                      json.dumps(usage, sort_keys=True, default=str)[:200])
            return
    r.stats["responses_duplicate"] += 1


def _ingest_update(r: _Reader, src: JsonlSource, obj: dict,
                   ordinal: int) -> None:
    if obj.get("method") not in METHODS:
        raise ValueError(f"unsupported method: {obj.get('method')!r}")
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    update = params.get("update") if isinstance(params, dict) else {}
    if not isinstance(update, dict) or \
            not isinstance(update.get("sessionUpdate"), str):
        raise ValueError("record without sessionUpdate")
    ts = iso_ts(obj.get("timestamp"))
    r.note_ts(ts)
    kind = update["sessionUpdate"]
    if kind == "user_message_chunk":
        text = _content_text(update.get("content"))
        if text:
            r.identity.observe_text(text)
            for body in USER_RULE_RE.findall(text):
                r.identity.observe_loaded_instructions(body.strip())
    elif kind in ("agent_message_chunk", "agent_thought_chunk"):
        return
    elif kind == "tool_call":
        _tool_call(r, src, update, params, ordinal, ts)
    elif kind == "tool_call_update":
        _tool_call_update(r, src, update, params, ordinal, ts)
    elif kind == "turn_completed":
        # Usage lands in the full replay, which also binds the prompt even
        # when its chunks arrived on an earlier sync.
        return
    elif kind in COMPACTION_KINDS:
        r.stats["compactions"] += 1
        event_id = params.get("_meta", {}).get("eventId") \
            if isinstance(params.get("_meta"), dict) else None
        r.event(src, "compaction", event_id or f"{kind}:{ordinal}", ordinal,
                ts, name=kind,
                detail=_scalars({k: v for k, v in update.items()
                                 if k != "sessionUpdate"}))
    elif kind in EXTENSION_KINDS:
        if kind == "subagent_spawned":
            _subagent_link(r, update)
        event_id = params.get("_meta", {}).get("eventId") \
            if isinstance(params.get("_meta"), dict) else None
        r.event(src, "lifecycle", event_id or f"{kind}:{ordinal}", ordinal,
                ts, name=kind,
                detail=_scalars({k: v for k, v in update.items()
                                 if k != "sessionUpdate"}))
    else:
        raise ValueError(f"unsupported sessionUpdate: {kind!r}")


def _turn_id(r: _Reader, params: dict):
    meta = params.get("_meta") if isinstance(params, dict) else None
    prompt_id = meta.get("promptId") if isinstance(meta, dict) else None
    if prompt_id:
        return f"{HARNESS}:{r.native_sid}:{prompt_id}"
    return None


def _tool_name(update: dict):
    title = update.get("title")
    if title:
        return str(title)
    meta = update.get("_meta") if isinstance(update.get("_meta"), dict) \
        else {}
    tool = meta.get("x.ai/tool") if isinstance(meta, dict) else None
    name = tool.get("name") if isinstance(tool, dict) else None
    return str(name) if name else "unknown"


def _tool_call(r: _Reader, src: JsonlSource, update: dict, params: dict,
               ordinal: int, ts) -> None:
    call_id = update.get("toolCallId")
    if not call_id:
        raise ValueError("tool_call without toolCallId")
    raw = update.get("rawInput")
    name = _tool_name(update)
    meta = update.get("_meta") if isinstance(update.get("_meta"), dict) \
        else {}
    tool = meta.get("x.ai/tool") if isinstance(meta, dict) else {}
    detail = {"tool": tool.get("name") if isinstance(tool, dict) else None,
              "kind": tool.get("kind") if isinstance(tool, dict) else None}
    r.event(src, "tool_call", call_id, ordinal, ts,
            turn_id=_turn_id(r, params), name=name,
            target=_safe_target(raw),
            fingerprint=fingerprint(
                name, json.dumps(raw, sort_keys=True, default=str)[:4000]),
            detail={k: v for k, v in detail.items() if v is not None})


def _tool_call_update(r: _Reader, src: JsonlSource, update: dict,
                      params: dict, ordinal: int, ts) -> None:
    call_id = update.get("toolCallId")
    if not call_id:
        raise ValueError("tool_call_update without toolCallId")
    turn_id = _turn_id(r, params)
    locations = update.get("locations")
    if update.get("kind") == "read" and isinstance(locations, list) \
            and locations:
        paths = [loc.get("path") for loc in locations
                 if isinstance(loc, dict) and loc.get("path")]
        if paths:
            for path in paths:
                r.identity.observe_path(str(path))
            skill = skill_from_path(str(paths[0]))
            # A second location in the same call may name the Skill while the
            # first does not; the Skill read is the identity evidence.
            if skill is None:
                for path in paths[1:]:
                    skill = skill_from_path(str(path))
                    if skill is not None:
                        break
            lines = {str(loc["path"]): loc.get("line") for loc in locations
                     if isinstance(loc, dict) and loc.get("path")
                     and loc.get("line") is not None}
            detail = {"paths": [str(p) for p in paths]}
            if lines:
                detail["lines"] = lines
            if skill:
                detail["skill"] = skill
            r.event(src, "skill_read" if skill else "read", call_id,
                    ordinal, ts, turn_id=turn_id,
                    name=skill or os.path.basename(str(paths[0])),
                    target=str(paths[0]),
                    fingerprint=fingerprint(call_id, paths),
                    detail=detail)
    status = update.get("status")
    if isinstance(status, str) and status in TERMINAL_TOOL_STATUS:
        r.event(src, "tool_result", call_id, ordinal, ts, turn_id=turn_id,
                name=_tool_name(update), status=TERMINAL_TOOL_STATUS[status],
                detail={"status": status})


def _subagent_link(r: _Reader, update: dict) -> None:
    parent = update.get("parent_session_id")
    child = update.get("child_session_id") or update.get("subagent_id")
    if parent and child and str(child) == r.native_sid:
        r.parent_key = f"{HARNESS}:{parent}"
    if parent and child:
        # The child row may be imported before or after this record; the
        # parent link survives either order because session fields only fill
        # unknowns.
        try:
            db.upsert_session(r.con, f"{HARNESS}:{child}", HARNESS,
                              str(child), None,
                              parent_session_key=f"{HARNESS}:{parent}")
        except (sqlite3.DatabaseError, ValueError):
            pass


def _ingest_event(r: _Reader, src: JsonlSource, obj: dict,
                  ordinal: int) -> None:
    if not isinstance(obj.get("type"), str):
        raise ValueError("event without type")
    kind = obj["type"]
    if kind in SKIP_EVENT_TYPES:
        return
    ts = iso_ts(obj.get("ts"))
    r.note_ts(ts)
    if kind == "turn_started":
        r.event(src, "lifecycle", f"turn_started:{ordinal}", ordinal, ts,
                name="turn_started",
                detail={"turn_number": obj.get("turn_number"),
                        "model_id": obj.get("model_id"),
                        "session_relationship":
                            obj.get("session_relationship")})
    elif kind == "turn_ended":
        r.event(src, "lifecycle", f"turn_ended:{ordinal}", ordinal, ts,
                name="turn_ended", status=obj.get("outcome"),
                detail={"outcome": obj.get("outcome")})
    elif kind == "tool_started":
        r.event(src, "lifecycle", f"tool_started:{ordinal}", ordinal, ts,
                name=str(obj.get("tool_name") or "unknown"),
                detail={"tool": obj.get("tool_name")})
    elif kind == "tool_completed":
        _tool_completed(r, src, obj, ordinal, ts)
    elif kind == "permission_requested":
        r.event(src, "permission", f"permission:{ordinal}", ordinal, ts,
                name=str(obj.get("tool_name") or "unknown"),
                detail={"tool": obj.get("tool_name"), "phase": "requested"})
    elif kind == "permission_resolved":
        r.event(src, "permission", f"permission:{ordinal}", ordinal, ts,
                name=str(obj.get("tool_name") or "unknown"),
                status=obj.get("decision"),
                duration_ms=obj.get("wait_ms")
                if isinstance(obj.get("wait_ms"), int) else None,
                detail={"tool": obj.get("tool_name"),
                        "decision": obj.get("decision"),
                        "wait_ms": obj.get("wait_ms")})
    else:
        r.event(src, "lifecycle", f"{kind}:{ordinal}", ordinal, ts,
                name=kind, detail=_scalars(
                    {k: v for k, v in obj.items() if k not in ("ts", "type")})
                or None)


def _tool_completed(r: _Reader, src: JsonlSource, obj: dict, ordinal: int,
                    ts) -> None:
    call_id = obj.get("tool_call_id")
    if not call_id:
        raise ValueError("tool_completed without tool_call_id")
    outcome = obj.get("outcome")
    status = "ok" if outcome in ("success", "completed") else "error"
    duration = obj.get("duration_ms") \
        if isinstance(obj.get("duration_ms"), int) else None
    row = r.con.execute(
        "SELECT id, status, duration_ms, detail_json FROM events"
        " WHERE session_key=? AND family='tool_result' AND native_id=?",
        (r.session_key, str(call_id))).fetchone()
    if row is None:
        r.event(src, "tool_result", call_id, ordinal, ts,
                name=str(obj.get("tool_name") or "unknown"), status=status,
                duration_ms=duration,
                detail={"tool": obj.get("tool_name"), "outcome": outcome})
        return
    try:
        detail = json.loads(row["detail_json"]) if row["detail_json"] else {}
    except ValueError:
        detail = {}
    changed = False
    if row["duration_ms"] is None and duration is not None:
        changed = True
    if row["status"] is None and status:
        changed = True
    for key, value in (("tool", obj.get("tool_name")), ("outcome", outcome)):
        if value is not None and key not in detail:
            detail[key] = value
            changed = True
    if changed:
        r.con.execute(
            "UPDATE events SET duration_ms=COALESCE(duration_ms, ?),"
            " status=COALESCE(status, ?), detail_json=? WHERE id=?",
            (duration, status, json.dumps(detail, sort_keys=True)[:4000],
             row["id"]))
