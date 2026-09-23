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
DIRECTION_BLOCK_RE = re.compile(
    r"<<<AGENTSMD_PROJECT_DIRECTION_V1>>>.*?<<<END_AGENTSMD_PROJECT_DIRECTION_V1>>>",
    re.S)
USER_QUERY_TAG_RE = re.compile(r"</?user_query\s*>", re.I)
GENERIC_BLOCK_RE = re.compile(r"<<<.*?>>>", re.S)
SECRET_SK_RE = re.compile(r"sk-[A-Za-z0-9\-_]{8,}")
SECRET_TOKEN_RE = re.compile(r"SECRET[A-Za-z0-9\-_]*")
TARGET_KEYS = ("target_file", "path", "file_path", "file", "command",
               "url", "pattern")
EXCERPT_LEN = 300
ERROR_EXCERPT_LEN = 200


def _sanitize_human_text(text: str) -> str:
    """Human's own text without injected instruction or preference content.

    Removes the full AgentsMD direction block, <user_rule> bodies, the
    <user_query> wrapper tags (keeping the inner human text) and any other
    <<<...>>> injected block, then redacts secret-looking tokens. Full native
    text still feeds SessionIdentity; only this sanitized form reaches the
    ledger excerpt.
    """
    if not text:
        return ""
    cleaned = DIRECTION_BLOCK_RE.sub("", text)
    cleaned = USER_RULE_RE.sub("", cleaned)
    cleaned = USER_QUERY_TAG_RE.sub("", cleaned)
    cleaned = GENERIC_BLOCK_RE.sub("", cleaned)
    cleaned = SECRET_SK_RE.sub("[redacted]", cleaned)
    cleaned = SECRET_TOKEN_RE.sub("[redacted]", cleaned)
    return cleaned.strip()


def _build_excerpt(full_text: str, is_genuine: bool) -> str:
    """At most 300 chars of genuine human text; empty for non-genuine."""
    if not is_genuine:
        return ""
    return _sanitize_human_text(full_text)[:EXCERPT_LEN]


def _shape_parts(obj) -> tuple[str, str, str]:
    """Sorted top-level keys, method and update type without any values."""
    if not isinstance(obj, dict):
        return "", "?", "?"
    try:
        keys = sorted(str(k) for k in obj.keys())
    except Exception:
        keys = []
    method = obj.get("method")
    method_s = method if isinstance(method, str) else "?"
    update_s = "?"
    params = obj.get("params")
    if isinstance(params, dict):
        upd = params.get("update")
        if isinstance(upd, dict):
            su = upd.get("sessionUpdate")
            if isinstance(su, str):
                update_s = su
    return ",".join(keys), method_s, update_s


def _safe_error_excerpt(category: str, obj) -> str:
    """Error category plus bounded record shape, never raw text."""
    keys_s, method_s, update_s = _shape_parts(obj)
    base = f"{category} keys=[{keys_s}] method={method_s} update={update_s}"
    return base[:ERROR_EXCERPT_LEN]


def _safe_unparsable(category: str) -> str:
    return f"{category} unparsable method=? update=?"[:ERROR_EXCERPT_LEN]


def _safe_conflict_excerpt(category: str, method: str, update: str,
                            extra: str = "") -> str:
    base = f"{category} method={method} update={update}"
    if extra:
        base += f" {extra}"
    return base[:ERROR_EXCERPT_LEN]


def _extract_prompt_id(obj: dict, update: dict) -> str | None:
    """Native prompt id from update, inner _meta or outer _meta."""
    candidates: list[dict] = []
    if isinstance(update, dict):
        candidates.append(update)
        inner = update.get("_meta")
        if isinstance(inner, dict):
            candidates.append(inner)
    params = obj.get("params") if isinstance(obj, dict) else None
    if isinstance(params, dict):
        pmeta = params.get("_meta")
        if isinstance(pmeta, dict):
            candidates.append(pmeta)
    if isinstance(obj, dict):
        outer = obj.get("_meta")
        if isinstance(outer, dict):
            candidates.append(outer)
    for d in candidates:
        for key in ("prompt_id", "promptId"):
            value = d.get(key)
            if value:
                return str(value)
    return None


def _classify_prompt(prompt_idx: str, chat: dict) -> tuple[str, int]:
    """Evidence-aware kind/is_genuine; provisional when chat is unreliable."""
    if not chat.get("reliable"):
        return "unknown", 0
    if prompt_idx in chat.get("synthetic", set()):
        return "synthetic", 0
    if prompt_idx in chat.get("seen", set()):
        return "genuine", 1
    return "unknown", 0


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
    chat = _read_chat(session_dir, r)
    if known and not updates_new and not events_new:
        reclassified = _reclassify_existing(con, r, session_dir, chat)
        if reclassified:
            con.commit()
        stats["unchanged"] = not bool(reclassified)
        stats["session_key"] = r.session_key
        return stats

    if updates_src is not None and (updates_new or not known):
        _replay_updates(con, r, updates_src, chat, model_fallback, effort)
        for ordinal, obj, line in updates_src.records():
            stats["lines"] += 1
            if obj is None or not isinstance(obj, dict):
                stats["malformed"] += 1
                updates_src.error(
                    ordinal, "json_error",
                    _safe_unparsable("json_error"))
                continue
            try:
                _ingest_update(r, updates_src, obj, ordinal)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                stats["malformed"] += 1
                updates_src.error(
                    ordinal, f"schema_error: {exc}",
                    _safe_error_excerpt("schema_error", obj))
    if events_src is not None and (events_new or not known):
        for ordinal, obj, line in events_src.records():
            stats["lines"] += 1
            if obj is None or not isinstance(obj, dict):
                stats["malformed"] += 1
                events_src.error(
                    ordinal, "json_error",
                    _safe_unparsable("json_error"))
                continue
            try:
                _ingest_event(r, events_src, obj, ordinal)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                stats["malformed"] += 1
                events_src.error(
                    ordinal, f"schema_error: {exc}",
                    _safe_error_excerpt("schema_error", obj))

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
    Missing, malformed or unterminated files leave reliable=False so prompts
    stay provisional (unknown, empty excerpt) until complete metadata arrives.
    """
    chat = {"synthetic": set(), "seen": set(), "effort": None,
            "reliable": True}
    path = os.path.join(session_dir, "chat_history.jsonl")
    try:
        with open(path, "rb") as bfh:
            raw_bytes = bfh.read()
    except OSError:
        chat["reliable"] = False
        return chat
    if raw_bytes and not raw_bytes.endswith(b"\n"):
        chat["reliable"] = False
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        chat["reliable"] = False
        return chat
    with fh:
        for raw in fh:
            if not raw.strip():
                continue
            if not raw.endswith("\n"):
                chat["reliable"] = False
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                chat["reliable"] = False
                continue
            if not isinstance(obj, dict):
                chat["reliable"] = False
                continue
            kind = obj.get("type")
            text = _content_text(obj.get("content"))
            if kind in ("user", "system") and text:
                r.identity.observe_text(text)
                for body in USER_RULE_RE.findall(text):
                    r.identity.observe_loaded_instructions(body.strip())
            if kind == "user" and obj.get("prompt_index") is not None:
                idx = str(obj["prompt_index"])
                chat["seen"].add(idx)
                if obj.get("synthetic_reason"):
                    chat["synthetic"].add(idx)
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


def _collect_prompts(path: str, record_error=None) -> tuple[dict, list, list]:
    """Prompts by promptIndex and completions in file order with ID links.

    Validates method before building any prompt/completion entry so an
    unsupported method never creates ledger state. Invalid JSON and
    non-dict lines are skipped silently here; the incremental ingest loop
    quarantines them once with a safe shape. Missing promptIndex/prompt_id
    are reported through record_error when provided (replay path) and
    skipped silently otherwise (reclassification path avoids duplicates).
    Returns (prompts, order, completions_ordered) where completions_ordered
    holds (ordinal, update, ts, outer_obj, method) and prompts values hold
    texts, model, first, ts, prompt_id and first_obj for safe shapes.
    """
    prompts: dict = {}
    order: list = []
    completions_ordered: list = []
    for ordinal, raw in _complete_lines(path):
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("method") not in METHODS:
            continue
        params = obj.get("params") if isinstance(obj.get("params"), dict) \
            else {}
        update = params.get("update") if isinstance(params, dict) else {}
        if not isinstance(update, dict):
            continue
        kind = update.get("sessionUpdate")
        if kind == "user_message_chunk":
            meta = update.get("_meta") if isinstance(
                update.get("_meta"), dict) else {}
            pidx = meta.get("promptIndex")
            if pidx is None:
                if record_error is not None:
                    record_error(
                        ordinal, "user chunk without promptIndex", obj)
                continue
            key = str(pidx)
            pid = _extract_prompt_id(obj, update)
            entry = prompts.get(key)
            if entry is None:
                entry = {"texts": [], "model": None, "first": ordinal,
                         "ts": iso_ts(obj.get("timestamp")), "prompt_id": pid,
                         "first_obj": obj}
                prompts[key] = entry
                order.append(key)
            else:
                if entry.get("prompt_id") is None and pid is not None:
                    entry["prompt_id"] = pid
                elif pid is not None and entry.get("prompt_id") is not None \
                        and pid != entry["prompt_id"]:
                    if record_error is not None:
                        record_error(
                            ordinal, "conflicting prompt id", obj)
                if entry.get("first_obj") is None:
                    entry["first_obj"] = obj
            entry["texts"].append(_content_text(update.get("content")))
            if entry["model"] is None and meta.get("modelId"):
                entry["model"] = str(meta["modelId"])
        elif kind == "turn_completed":
            pid = update.get("prompt_id") or update.get("promptId")
            if not pid:
                if record_error is not None:
                    record_error(
                        ordinal, "turn_completed without prompt_id", obj)
                continue
            completions_ordered.append(
                (ordinal, update, iso_ts(obj.get("timestamp")), obj,
                 obj.get("method")))
        else:
            continue
    return prompts, order, completions_ordered


def _upsert_submission(con, r: _Reader, src, chat: dict, key: str,
                       entry: dict, completions_by_id: dict,
                       record_error) -> None:
    full_text = "".join(entry["texts"])
    kind, is_genuine = _classify_prompt(key, chat)
    excerpt = _build_excerpt(full_text, bool(is_genuine))
    pid = entry.get("prompt_id")
    turn_id = (f"{HARNESS}:{r.native_sid}:{pid}"
               if pid and pid in completions_by_id else None)
    native_id = f"{HARNESS}:{r.native_sid}:prompt:{key}"
    existing = con.execute(
        "SELECT text_hash, text_excerpt, turn_id, kind, is_genuine"
        " FROM submissions WHERE native_id=?", (native_id,)).fetchone()
    digest = text_hash(full_text)
    if existing is None:
        cur = con.execute(
            "INSERT OR IGNORE INTO submissions(native_id, source_id,"
            " session_key, turn_id, ordinal_num, ts, kind, text_hash,"
            " text_excerpt, is_genuine) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (native_id, src.source_id if src is not None else None,
             r.session_key, turn_id, entry["first"], entry["ts"], kind,
             digest, excerpt, is_genuine))
        if cur.rowcount:
            r.stats["submissions_inserted"] += 1
        return
    # Existing row: follow appended text, allow reclassification, fill turn.
    if digest != existing["text_hash"]:
        new_sanitized = excerpt
        old_excerpt = existing["text_excerpt"] or ""
        # Growth means the sanitized form extends the stored one (empty
        # provisional excerpts are a prefix of any reclassified excerpt).
        if new_sanitized.startswith(old_excerpt) or old_excerpt == "":
            # For synthetic/provisional both excerpts are empty; still
            # follow the hash so later comparisons see the latest text.
            con.execute(
                "UPDATE submissions SET text_hash=?, text_excerpt=?"
                " WHERE native_id=?", (digest, new_sanitized, native_id))
        else:
            r.stats["malformed"] += 1
            if record_error is not None:
                record_error(
                    entry["first"], "conflicting prompt",
                    entry.get("first_obj"))
            # Do not apply divergent text; keep original hash/excerpt.
    # Reclassification: reliable chat evidence updates kind/excerpt even
    # when the full text hash is unchanged. Never downgrade a definitive
    # genuine/synthetic row merely because later chat is unreliable or
    # silent for that index.
    if chat.get("reliable"):
        if kind in ("genuine", "synthetic") and \
                (existing["kind"] != kind or
                 (existing["is_genuine"] or 0) != is_genuine or
                 (existing["text_excerpt"] or "") != excerpt):
            # Allow provisional->definitive and definitive->definitive
            # flips; keep provisional when desired is unknown.
            con.execute(
                "UPDATE submissions SET kind=?, is_genuine=?, text_excerpt=?"
                " WHERE native_id=?", (kind, is_genuine, excerpt, native_id))
    else:
        # Unreliable chat: only ensure provisional rows stay empty-excerpt.
        if existing["kind"] in (None, "", "unknown") and \
                (existing["text_excerpt"] or "") != "":
            con.execute(
                "UPDATE submissions SET text_excerpt=? WHERE native_id=?",
                ("", native_id))
    if turn_id and not existing["turn_id"]:
        con.execute("UPDATE submissions SET turn_id=? WHERE native_id=?",
                    (turn_id, native_id))


def _reclassify_existing(con, r: _Reader, session_dir: str,
                         chat: dict) -> int:
    """Update stored submissions when chat metadata arrives later.

    Runs even when updates.jsonl/events.jsonl are unchanged. Re-reads the
    updates file silently (no duplicate import_errors) and applies
    evidence-aware kind/excerpt/turn updates in place without duplicates.
    Returns the number of rows changed.
    """
    updates_path = os.path.join(session_dir, "updates.jsonl")
    if not os.path.isfile(updates_path):
        return 0
    # Source id for any late insert (normally rows already exist).
    src_holder = None
    try:
        src_holder = JsonlSource(con, HARNESS, updates_path)
    except (OSError, sqlite3.DatabaseError):
        src_holder = None
    prompts, order, completions_ordered = _collect_prompts(
        updates_path, record_error=None)
    completions_by_id = {}
    for ordinal, update, ts, obj, method in completions_ordered:
        pid = str(update.get("prompt_id") or update.get("promptId"))
        if pid not in completions_by_id:
            completions_by_id[pid] = (ordinal, update, ts, obj, method)
    before = con.total_changes
    for key in order:
        _upsert_submission(con, r, src_holder, chat, key, prompts[key],
                           completions_by_id, record_error=None)
    # Finish without advancing offsets when we only reclassified: do not
    # call finish() here because sources offsets belong to the incremental
    # loops. Just report whether anything changed.
    return con.total_changes - before


def _replay_updates(con, r: _Reader, src: JsonlSource, chat: dict,
                    model_fallback, effort) -> None:
    """Rebuild prompts and per-prompt usage from the whole updates.jsonl.

    A growing file can extend an open prompt or finalize it on a later sync,
    but the schema keeps only the text hash and a bounded excerpt, so the
    full text is reconstructed here on every sync that saw new bytes. Every
    insert is under a natural key, so the replay never duplicates rows.
    Completions bind to prompts by native prompt id (deduplicated); position
    never shifts bindings. Unmatched prompts stay unbound; unmatched
    completions still store one response row by their own key.
    """

    def record_error(ordinal: int, category: str, obj) -> None:
        r.stats["malformed"] += 1
        if category == "user chunk without promptIndex":
            src.error(ordinal, category,
                      _safe_error_excerpt("missing_prompt_index", obj))
        elif category == "turn_completed without prompt_id":
            src.error(ordinal, category,
                      _safe_error_excerpt("missing_prompt_id", obj))
        elif category == "conflicting prompt id":
            src.error(ordinal, "conflicting prompt id",
                      _safe_error_excerpt("conflicting_prompt", obj))
        elif category == "conflicting prompt":
            src.error(ordinal, category,
                      _safe_conflict_excerpt(
                          "conflicting_prompt", "session/update",
                          "user_message_chunk"))
        else:
            src.error(ordinal, category,
                      _safe_error_excerpt("schema_error", obj))

    prompts, order, completions_ordered = _collect_prompts(
        src.path, record_error=record_error)
    completions_by_id: dict = {}
    for ordinal, update, ts, obj, method in completions_ordered:
        pid = str(update.get("prompt_id") or update.get("promptId"))
        if pid not in completions_by_id:
            completions_by_id[pid] = (ordinal, update, ts, obj, method)
        else:
            # Duplicate turn_completed: keep first binding but merge late
            # usage when the first lacked it, so a repeated completion can
            # fill NULL counters without shifting any prompt binding.
            _, first_update, _, _, _ = completions_by_id[pid]
            first_usage = first_update.get("usage")
            new_usage = update.get("usage")
            if (not isinstance(first_usage, dict) or not first_usage) \
                    and isinstance(new_usage, dict) and new_usage:
                merged = dict(first_update)
                merged["usage"] = new_usage
                completions_by_id[pid] = (
                    completions_by_id[pid][0], merged,
                    completions_by_id[pid][2], completions_by_id[pid][3],
                    completions_by_id[pid][4])
    # Prompt-ID to chunk model for response model fallback.
    pid_to_model: dict = {}
    for key in order:
        pid = prompts[key].get("prompt_id")
        if pid and pid not in pid_to_model and prompts[key].get("model"):
            pid_to_model[pid] = prompts[key]["model"]
    turn_models = _turn_models(
        os.path.join(os.path.dirname(src.path), "events.jsonl"))
    for key in order:
        _upsert_submission(con, r, src, chat, key, prompts[key],
                           completions_by_id, record_error)
    for ordinal, update, ts, obj, method in completions_ordered:
        pid = str(update.get("prompt_id") or update.get("promptId"))
        chunk_model = pid_to_model.get(pid)
        _store_response(con, r, src, ordinal, update, ts,
                        _usage_model(update), model_fallback, chunk_model,
                        effort or chat.get("effort"), turn_models,
                        outer_obj=obj)


def _usage_model(update: dict):
    usage = update.get("usage")
    if isinstance(usage, dict):
        model_usage = usage.get("modelUsage")
        if isinstance(model_usage, dict) and len(model_usage) == 1:
            return next(iter(model_usage))
    return None


def _store_response(con, r: _Reader, src: JsonlSource, ordinal: int,
                    update: dict, ts, usage_model, summary_model, chunk_model,
                    effort, turn_models, outer_obj=None) -> None:
    prompt_id = update.get("prompt_id") or update.get("promptId")
    if not prompt_id:
        return
    prompt_id = str(prompt_id)
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
            shape = _safe_error_excerpt("conflicting_usage", outer_obj) \
                if isinstance(outer_obj, dict) else _safe_conflict_excerpt(
                    "conflicting_usage", "session/update", "turn_completed",
                    f"prompt_id={prompt_id}")
            # Ensure prompt_id travels without any counter values.
            if "prompt_id=" not in shape:
                shape = (shape + f" prompt_id={prompt_id}")[:ERROR_EXCERPT_LEN]
            src.error(ordinal, f"conflicting usage for {prompt_id}", shape)
            return
    fill = {key: counters[key] for key in counters
            if existing[key] is None and counters[key] is not None}
    if fill:
        con.execute(
            "UPDATE responses SET input_tokens=COALESCE(input_tokens, ?),"
            " cached_input_tokens=COALESCE(cached_input_tokens, ?),"
            " cache_write_input_tokens=COALESCE(cache_write_input_tokens, ?),"
            " output_tokens=COALESCE(output_tokens, ?),"
            " reasoning_output_tokens=COALESCE(reasoning_output_tokens, ?),"
            " total_tokens=COALESCE(total_tokens, ?) WHERE response_id=?",
            (fill.get("input_tokens"), fill.get("cached_input_tokens"),
             fill.get("cache_write_input_tokens"), fill.get("output_tokens"),
             fill.get("reasoning_output_tokens"), fill.get("total_tokens"),
             response_id))
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
