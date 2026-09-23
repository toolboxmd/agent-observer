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

from .. import db, privacy
from ..identity import SessionIdentity, skill_from_path
from ..ingest import JsonlSource, fingerprint, insert_event, iso_ts, text_hash

HARNESS = "grok"
SEMANTICS = "grok:input_includes_cached,output_includes_reasoning"
DEFAULT_ROOT = os.path.expanduser("~/.grok/sessions")

CAPABILITIES = [
    ("model_usage", True, "one responses row per turn_completed prompt_id; input includes cached reads, output includes reasoning"),
    ("tool_calls", True, "tool_call records with validated native tool name, safe target and argument fingerprint; raw input never stored"),
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
SECRET_SK_RE = re.compile(r"sk-[A-Za-z0-9\-_]{8,}")
SECRET_TOKEN_RE = re.compile(r"SECRET[A-Za-z0-9\-_]*")
# Event targets come only from validated path or command fields. Pattern
# and URL values are never targets (rule 6: targets follow the same type
# rules as detail, so only paths and commands persist).
TARGET_KEYS = ("target_file", "path", "file_path", "file", "command")

# Fail-closed ledger string gate for native identifiers (tool, call, event,
# prompt and session ids, model names, skill names): short tokens without
# free-text markers. Anything else is dropped, never stringified.
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-/:]{1,200}$")
# Location objects carrying these keys bear message/output-style free text
# and must not reach any ledger column, even when a valid path is present
# alongside them.
_FREE_TEXT_LOCATION_KEYS = frozenset({
    "message", "output", "content", "text", "error", "error_text",
    "arguments", "args", "result", "data",
})

# Closed value sets for enum-shaped event status columns. Fail closed: only
# these exact strings persist in their column. Anything else, even
# token-shaped, is dropped (a lexical check alone cannot tell free text
# from an enum). Event detail itself is filtered by agent_observer/privacy.py
# rule 6 through the ingest path, so the adapter builds no detail allowlist.
_OUTCOME_VALUES = frozenset({"success", "completed", "failed", "error"})
_DECISION_VALUES = frozenset({"allow", "deny"})


def _safe_token(value) -> str | None:
    """Validated native-identifier string, or None when it must not persist."""
    if not isinstance(value, str) or not value:
        return None
    if not _SAFE_TOKEN_RE.match(value):
        return None
    return value


def _valid_enum(value, allowed: frozenset) -> str | None:
    """Closed-set enum string, or None for anything not in the set."""
    if isinstance(value, str) and value in allowed:
        return value
    return None


def _record_error_once(src, stats, ordinal: int, category: str,
                        line: str = "") -> bool:
    """Insert one import_errors row, deduplicated by source/ordinal/category.

    The category and line pass through the ingest path, which applies
    agent_observer/privacy.py rules 4 and 5: the error column holds exactly
    one closed category (anything else maps to the fallback) and the excerpt
    holds only sorted top-level key names. The deduplication lookup uses the
    same mapped category so a replay never adds a duplicate row. A genuinely
    new ordinal or a new fixed category still gets its own row. Returns True
    when a new row was inserted.
    """
    safe = privacy.error_category(category)
    existing = src.con.execute(
        "SELECT 1 FROM import_errors WHERE harness=? AND source_path=?"
        " AND ordinal_num=? AND error=?",
        (src.harness, src.path, ordinal, safe)).fetchone()
    if existing is not None:
        return False
    src.error(ordinal, category, line)
    if stats is not None:
        stats["malformed"] = stats.get("malformed", 0) + 1
    return True


def _valid_native_id(value) -> str | None:
    """Native call/event/prompt/session identifier, or None when invalid."""
    return _safe_token(value)


def _valid_prompt_index(value):
    """Prompt index as a stable key, or None when it must not persist."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    return _safe_token(value)


class _AdapterError(ValueError):
    """Fixed-category adapter failure; never carries record values."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


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
            # Fail closed: only plain string identifiers persist; dicts,
            # lists and other shapes are never stringified into ledger keys.
            if isinstance(value, str) and value:
                if _safe_token(value) is not None:
                    return value
    return None


def _classify_prompt(prompt_idx: str, chat: dict,
                     is_main_session: bool = True) -> tuple[str, int]:
    """Evidence-aware kind/is_genuine; provisional when chat is unreliable.

    A child or subagent session is never genuine: its prompts are dispatched
    by the parent agent, not typed by a human, so they classify as synthetic
    with an empty excerpt even when chat history marks them as user prompts.
    """
    if not is_main_session:
        return "synthetic", 0
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
        # Fail closed: only plain strings persist; dicts/lists are never
        # stringified into the ledger.
        if not isinstance(value, str) or not value:
            continue
        text = value
        text = SECRET_SK_RE.sub("[redacted]", text)
        text = SECRET_TOKEN_RE.sub("[redacted]", text)
        return text[:500]
    return None


# Whitelisted lifecycle metadata reached event detail_json through an
# adapter-private allowlist. That allowlist is gone: every event detail now
# passes through agent_observer/privacy.py rule 6 in the ingest path, which
# keeps only its own per-family allowlist. The adapter keeps extracting and
# validating native identifiers, paths, commands, statuses and event
# families for ledger columns; free-text detail never persists.


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
        # Every target and detail mapping passes through
        # agent_observer/privacy.py rule 6 in insert_event. On a privacy
        # version re-import the existing row's target and detail are
        # corrected in place instead of kept stale.
        insert_event(self.con, self.stats, source_id=src.source_id,
                     session_key=self.session_key, family=family,
                     native_id=native_id, ordinal=ordinal, ts=ts,
                     update=src.privacy_stale, **kw)

    @property
    def is_main_session(self) -> bool:
        """False for subagent and child sessions, from every available signal.

        summary.json session_kind, a persisted sessions.parent_session_key,
        a parent subagent_spawned dispatch (even when the child was imported
        first) and events.jsonl turn_started with
        session_relationship='subagent' all prove a child. Any signal forces
        submissions non-genuine with an empty excerpt via privacy.py rule 1.
        """
        return self.meta.get("role") != "subagent" \
            and self.parent_key is None


_GROK_USAGE_MAP = (
    ("input_tokens", "inputTokens"),
    ("cached_input_tokens", "cachedReadTokens"),
    ("cache_write_input_tokens", "cacheCreationTokens"),
    ("output_tokens", "outputTokens"),
    ("reasoning_output_tokens", "reasoningTokens"),
    ("total_tokens", "totalTokens"),
)


def _validated_usage_counters(update: dict) -> dict:
    """Validated counter dict for a turn_completed update, or raise.

    Codex-style absent versus malformed distinction, Grok-specific keys:
    missing usage, null usage and an empty usage object are absent usage
    and yield all-NULL counters without an error. A present malformed shape
    (non-dict) or a malformed present counter (wrong type, including
    booleans) raises _AdapterError('malformed_usage') before any SQL, so
    the caller quarantines the completion and creates no partial row.
    Unknown keys such as modelUsage or modelCalls are ignored; a None
    counter value stays NULL.
    """
    if "usage" not in update:
        return {key: None for key, _ in _GROK_USAGE_MAP}
    raw = update.get("usage")
    if raw is None:
        return {key: None for key, _ in _GROK_USAGE_MAP}
    if not isinstance(raw, dict):
        raise _AdapterError("malformed_usage")
    if not raw:
        return {key: None for key, _ in _GROK_USAGE_MAP}
    for _, native in _GROK_USAGE_MAP:
        if native in raw:
            value = raw[native]
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise _AdapterError("malformed_usage")
    return {key: (raw.get(native) if isinstance(raw.get(native), int)
                  and not isinstance(raw.get(native), bool) else None)
            for key, native in _GROK_USAGE_MAP}


def _has_any_counter(counters: dict | None) -> bool:
    return bool(counters) and any(v is not None for v in counters.values())


def _scan_updates_file_for_parent(updates_path: str,
                                  native_sid: str) -> str | None:
    """Parent session key from subagent_spawned records naming native_sid."""
    if not updates_path or not os.path.isfile(updates_path):
        return None
    try:
        lines = list(_complete_lines(updates_path))
    except OSError:
        return None
    for _, raw in lines:
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
        if update.get("sessionUpdate") != "subagent_spawned":
            continue
        parent = update.get("parent_session_id")
        child = update.get("child_session_id") or update.get("subagent_id")
        if not isinstance(parent, str) or _safe_token(parent) is None:
            continue
        if not isinstance(child, str) or _safe_token(child) is None:
            continue
        if child == native_sid:
            return f"{HARNESS}:{parent}"
    return None


def _has_subagent_relationship(session_dir: str) -> bool:
    """Whether events.jsonl proves this session is a subagent.

    Any complete turn_started line with session_relationship exactly
    'subagent' marks the session as a child, regardless of summary or
    parent-link evidence. Other relationship values prove nothing.
    """
    events_path = os.path.join(session_dir, "events.jsonl")
    if not os.path.isfile(events_path):
        return False
    try:
        lines = list(_complete_lines(events_path))
    except OSError:
        return False
    for _, raw in lines:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "turn_started":
            continue
        if obj.get("session_relationship") == "subagent":
            return True
    return False


def _find_parent_via_dispatches(session_dir: str,
                                native_sid: str) -> str | None:
    """Parent key from own or any sibling session's spawn records on disk.

    Covers the import-order case: the child may be imported before its
    parent session, but the parent's updates.jsonl already exists on disk.
    Only subagent_spawned shapes are read; prompt text is never touched.
    """
    own = _scan_updates_file_for_parent(
        os.path.join(session_dir, "updates.jsonl"), native_sid)
    if own is not None:
        return own
    group_dir = os.path.dirname(os.path.abspath(session_dir.rstrip(os.sep)))
    root = os.path.dirname(group_dir.rstrip(os.sep))
    candidates: list[str] = []
    try:
        if root and os.path.isdir(root):
            for sess_dir in discover(root):
                if os.path.abspath(sess_dir) == os.path.abspath(session_dir):
                    continue
                candidates.append(sess_dir)
        elif os.path.isdir(group_dir):
            for child in sorted(os.listdir(group_dir)):
                path = os.path.join(group_dir, child)
                if os.path.abspath(path) == os.path.abspath(session_dir):
                    continue
                if os.path.isdir(path) and _looks_like_session(path):
                    candidates.append(path)
    except OSError:
        return None
    for sess_dir in candidates:
        found = _scan_updates_file_for_parent(
            os.path.join(sess_dir, "updates.jsonl"), native_sid)
        if found is not None:
            return found
    return None


def _precompute_child_status(con: sqlite3.Connection, r: _Reader,
                             session_dir: str) -> None:
    """Set parent link and subagent role before any replay.

    Order: summary role (already in r.meta), persisted
    sessions.parent_session_key/role, parent subagent_spawned dispatches on
    disk (own file first, then siblings for import-order), and
    events.jsonl turn_started session_relationship='subagent'. A proven
    child is non-genuine with an empty excerpt via privacy.py rule 1.
    """
    try:
        row = con.execute(
            "SELECT parent_session_key, role FROM sessions WHERE session_key=?",
            (r.session_key,)).fetchone()
    except sqlite3.DatabaseError:
        row = None
    if row is not None:
        try:
            persisted_parent = row["parent_session_key"]
        except (KeyError, TypeError, IndexError):
            persisted_parent = None
        try:
            persisted_role = row["role"]
        except (KeyError, TypeError, IndexError):
            persisted_role = None
        if persisted_parent and r.parent_key is None:
            r.parent_key = persisted_parent
        if persisted_role == "subagent" and r.meta.get("role") != "subagent":
            r.meta["role"] = "subagent"
    if r.parent_key is None and r.meta.get("role") != "subagent":
        found = _find_parent_via_dispatches(session_dir, r.native_sid)
        if found is not None:
            r.parent_key = found
    elif r.parent_key is None:
        # Already proven via summary/persisted role, but a parent link may
        # still exist on disk; record it for the session row.
        found = _find_parent_via_dispatches(session_dir, r.native_sid)
        if found is not None:
            r.parent_key = found
    if r.meta.get("role") != "subagent":
        if _has_subagent_relationship(session_dir):
            r.meta["role"] = "subagent"


def _force_child_synthetic(con: sqlite3.Connection, stats: dict | None,
                           child_key: str) -> int:
    """Force existing submissions of a proven child to synthetic/empty.

    Uses the shared privacy rule (empty excerpt for non-genuine) and never
    touches free text: only kind, is_genuine and text_excerpt change, in
    place, without duplicates. Returns rows changed.
    """
    empty = privacy.submission_excerpt("x", is_genuine=False,
                                       is_main_session=False)
    assert empty == ""
    try:
        cur = con.execute(
            "UPDATE submissions SET kind='synthetic', is_genuine=0,"
            " text_excerpt='' WHERE session_key=? AND (kind!='synthetic'"
            " OR is_genuine!=0 OR text_excerpt!='')",
            (child_key,))
    except sqlite3.DatabaseError:
        return 0
    changed = cur.rowcount or 0
    if changed and stats is not None:
        stats["submissions_updated"] = \
            stats.get("submissions_updated", 0) + changed
    return changed


def _reconcile_response_metadata(con: sqlite3.Connection, r: _Reader,
                                 session_dir: str, model_fallback,
                                 effort_combined) -> int:
    """Update stale or NULL model/effort on existing responses in place.

    Recomputes validated model and effort with the existing precedence
    (turn_started stream, usage modelUsage, summary fallback, chunk model;
    summary effort else chat effort) from current summary/events files.
    Only a new valid value differing from the stored one updates the row;
    a missing new value never clears a known one. Never inserts or
    duplicates rows; counters and usage_conflict behavior are untouched.
    Returns rows changed.
    """
    updates_path = os.path.join(session_dir, "updates.jsonl")
    if not os.path.isfile(updates_path):
        return 0
    prompts, order, completions_ordered = _collect_prompts(
        updates_path, record_error=None)
    pid_to_model: dict = {}
    for key in order:
        pid = prompts[key].get("prompt_id")
        if pid and pid not in pid_to_model and prompts[key].get("model"):
            pid_to_model[pid] = prompts[key]["model"]
    turn_models = _turn_models(os.path.join(session_dir, "events.jsonl"))
    safe_turns = [(t, m) for t, m in turn_models
                  if isinstance(m, str) and _safe_token(m) is not None]
    first_per_pid: dict = {}
    for _ordinal, update, ts, _obj, _method, _raw in completions_ordered:
        raw_pid = update.get("prompt_id") or update.get("promptId")
        pid = raw_pid if isinstance(raw_pid, str) \
            and _safe_token(raw_pid) is not None else None
        if pid is None:
            continue
        if pid not in first_per_pid:
            first_per_pid[pid] = (update, ts)
    valid_effort = _valid_model(effort_combined)
    changed = 0
    for pid, (update, ts) in first_per_pid.items():
        response_id = f"{HARNESS}:{r.native_sid}:{pid}"
        try:
            existing = con.execute(
                "SELECT model, effort FROM responses WHERE response_id=?",
                (response_id,)).fetchone()
        except sqlite3.DatabaseError:
            continue
        if existing is None:
            continue
        chunk_model = _valid_model(pid_to_model.get(pid))
        usage_model = _valid_model(_usage_model(update))
        summary_model = _valid_model(model_fallback)
        new_model = _model_at(ts, safe_turns, usage_model, summary_model,
                              chunk_model)
        sets: list[str] = []
        args: list = []
        try:
            old_model = existing["model"]
        except (KeyError, TypeError, IndexError):
            old_model = None
        try:
            old_effort = existing["effort"]
        except (KeyError, TypeError, IndexError):
            old_effort = None
        if new_model is not None and old_model != new_model:
            sets.append("model=?")
            args.append(new_model)
        if valid_effort is not None and old_effort != valid_effort:
            sets.append("effort=?")
            args.append(valid_effort)
        if sets:
            args.append(response_id)
            try:
                con.execute(
                    f"UPDATE responses SET {', '.join(sets)}"
                    " WHERE response_id=?", args)
            except sqlite3.DatabaseError:
                continue
            changed += 1
    return changed


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
    if isinstance(info.get("id"), str) and info.get("id"):
        if _safe_token(info["id"]) is not None:
            r.native_sid = info["id"]
            r.session_key = f"{HARNESS}:{r.native_sid}"
    group_name = os.path.basename(os.path.dirname(session_dir.rstrip(os.sep)))
    fallback_dir = urllib.parse.unquote(group_name)
    project_dir = (summary.get("git_root_dir") if isinstance(
        summary.get("git_root_dir"), str) else None) or (
            info.get("cwd") if isinstance(info.get("cwd"), str) else None) \
        or fallback_dir or None
    if isinstance(project_dir, str):
        project_dir = project_dir.rstrip("/") or None
    if isinstance(summary.get("head_branch"), str) and summary.get(
            "head_branch"):
        r.meta["git_branch"] = summary["head_branch"][:200]
    if project_dir:
        r.meta["project_dir"] = str(project_dir)
    if summary.get("session_kind") == "subagent":
        r.meta["role"] = "subagent"
    model_fallback = summary.get("current_model_id") if isinstance(
        summary.get("current_model_id"), str) and _safe_token(
            summary.get("current_model_id")) is not None else None
    effort = summary.get("reasoning_effort") if isinstance(
        summary.get("reasoning_effort"), str) and _safe_token(
            summary.get("reasoning_effort")) is not None else None
    for key in ("created_at", "last_active_at", "updated_at"):
        ts = iso_ts(summary.get(key))
        if ts is not None:
            r.note_ts(ts)

    # Child status is available before any replay: summary role is already
    # in r.meta; persisted links, parent dispatches (import-order) and the
    # turn_started subagent relationship complete it here.
    _precompute_child_status(con, r, session_dir)

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
        reconciled = _reconcile_response_metadata(
            con, r, session_dir, model_fallback,
            effort or chat.get("effort"))
        # Persist late identity even when no JSONL bytes changed.
        late_fields = {"started_at": r.first_ts, "ended_at": r.last_ts,
                       **r.meta, **r.identity.fields(con)}
        if r.parent_key:
            late_fields["parent_session_key"] = r.parent_key
        db.upsert_session(
            con, r.session_key, HARNESS, r.native_sid,
            updates_src.source_id if updates_src is not None
            else (events_src.source_id if events_src is not None else None),
            **late_fields)
        con.commit()
        stats["unchanged"] = not bool(reclassified or reconciled)
        if reconciled:
            stats["responses_updated"] = \
                stats.get("responses_updated", 0) + reconciled
        stats["session_key"] = r.session_key
        return stats

    # A parent link discovered while ingesting this sync's own updates means
    # the replay above computed submissions as a main session; reclassify to
    # correct those rows to non-genuine in place.
    parent_before = r.parent_key
    if updates_src is not None and (updates_new or not known):
        _replay_updates(con, r, updates_src, chat, model_fallback, effort)
        for ordinal, obj, line in updates_src.records():
            stats["lines"] += 1
            if obj is None or not isinstance(obj, dict):
                _record_error_once(
                    updates_src, stats, ordinal, "malformed_json", line)
                continue
            try:
                _ingest_update(r, updates_src, obj, ordinal)
            except _AdapterError as exc:
                _record_error_once(
                    updates_src, stats, ordinal, exc.category, line)
            except (KeyError, TypeError, ValueError, AttributeError):
                _record_error_once(
                    updates_src, stats, ordinal, "schema_error", line)
    if events_src is not None and (events_new or not known):
        for ordinal, obj, line in events_src.records():
            stats["lines"] += 1
            if obj is None or not isinstance(obj, dict):
                _record_error_once(
                    events_src, stats, ordinal, "malformed_json", line)
                continue
            try:
                _ingest_event(r, events_src, obj, ordinal)
            except _AdapterError as exc:
                _record_error_once(
                    events_src, stats, ordinal, exc.category, line)
            except (KeyError, TypeError, ValueError, AttributeError):
                _record_error_once(
                    events_src, stats, ordinal, "schema_error", line)
    # Late chat metadata reclassifies provisional submissions even when only
    # events.jsonl grew and updates.jsonl did not. Runs silently (no duplicate
    # import_errors) and never duplicates rows. Child status was already
    # precomputed before replay, so a parent link in this sync's own bytes
    # wrote synthetic rows directly; this corrects rows from earlier syncs
    # when late summary, persisted-link, dispatch or relationship evidence
    # arrives.
    if known or (r.parent_key is not None
                 and r.parent_key != parent_before):
        _reclassify_existing(con, r, session_dir, chat)
    replayed_updates = updates_src is not None and (updates_new or not known)
    if not replayed_updates:
        # Only events (or only summary/chat) changed: turn_started models,
        # summary model/effort or chat effort may be new. Update existing
        # response rows in place without duplicates or counter changes.
        reconciled = _reconcile_response_metadata(
            con, r, session_dir, model_fallback,
            effort or chat.get("effort"))
        if reconciled:
            stats["responses_updated"] = \
                stats.get("responses_updated", 0) + reconciled

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
                idx = _valid_prompt_index(obj.get("prompt_index"))
                if idx is None:
                    chat["reliable"] = False
                    continue
                chat["seen"].add(idx)
                if obj.get("synthetic_reason"):
                    chat["synthetic"].add(idx)
            if kind == "assistant" and obj.get("reasoning_effort") \
                    and chat["effort"] is None:
                effort = obj.get("reasoning_effort")
                if isinstance(effort, str) and _safe_token(effort) is not None:
                    chat["effort"] = effort
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
                and isinstance(obj.get("model_id"), str) \
                and _safe_token(obj.get("model_id")) is not None:
            ts = iso_ts(obj.get("ts"))
            if ts is not None:
                models.append((ts, obj["model_id"]))
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
    quarantines them once with the raw line. Missing promptIndex/prompt_id
    are reported through record_error when provided (replay path) and
    skipped silently otherwise (reclassification path avoids duplicates).
    Categories are the closed privacy.py set (missing_id, schema_error).
    Returns (prompts, order, completions_ordered) where completions_ordered
    holds (ordinal, update, ts, outer_obj, method, raw_line) and prompts
    values hold texts, model, first, ts and prompt_id.
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
            key = _valid_prompt_index(pidx)
            if key is None:
                if record_error is not None:
                    record_error(ordinal, "missing_id", raw)
                continue
            pid = _extract_prompt_id(obj, update)
            entry = prompts.get(key)
            if entry is None:
                entry = {"texts": [], "model": None, "first": ordinal,
                         "ts": iso_ts(obj.get("timestamp")), "prompt_id": pid}
                prompts[key] = entry
                order.append(key)
            else:
                if entry.get("prompt_id") is None and pid is not None:
                    entry["prompt_id"] = pid
                elif pid is not None and entry.get("prompt_id") is not None \
                        and pid != entry["prompt_id"]:
                    if record_error is not None:
                        record_error(ordinal, "schema_error", raw)
            entry["texts"].append(_content_text(update.get("content")))
            if entry["model"] is None and isinstance(
                    meta.get("modelId"), str) and _safe_token(
                        meta.get("modelId")) is not None:
                entry["model"] = meta["modelId"]
        elif kind == "turn_completed":
            raw_pid = update.get("prompt_id") or update.get("promptId")
            pid = raw_pid if isinstance(
                raw_pid, str) and _safe_token(raw_pid) is not None else None
            if not pid:
                if record_error is not None:
                    record_error(ordinal, "missing_id", raw)
                continue
            completions_ordered.append(
                (ordinal, update, iso_ts(obj.get("timestamp")), obj,
                 obj.get("method"), raw))
        else:
            continue
    return prompts, order, completions_ordered


def _upsert_submission(con, r: _Reader, src, chat: dict, key: str,
                       entry: dict, completions_by_id: dict) -> None:
    """Insert or converge one submission row under its native prompt key.

    The excerpt comes from agent_observer/privacy.py rule 1: empty unless a
    genuine main-session human submission, otherwise the human text up to
    the first tag-like marker with whitespace collapsed at 300 chars. Child
    and subagent sessions are never genuine. Full native text still feeds
    the usage hash; only the privacy excerpt reaches the ledger.

    Every relevant re-sync recomputes text_hash, text_excerpt, kind and
    is_genuine and updates the existing row in place when any of them
    differs: appended text, late chat evidence, a privacy version re-import
    and evidence that became unknown all converge instead of preserving
    stale values. Turn bindings only fill unknowns.
    """
    full_text = "".join(entry["texts"])
    is_main = r.is_main_session
    kind, is_genuine = _classify_prompt(key, chat, is_main)
    excerpt = privacy.submission_excerpt(
        full_text, is_genuine=bool(is_genuine), is_main_session=is_main)
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
    if digest != existing["text_hash"] \
            or (existing["text_excerpt"] or "") != excerpt \
            or existing["kind"] != kind \
            or (existing["is_genuine"] or 0) != is_genuine:
        con.execute(
            "UPDATE submissions SET text_hash=?, text_excerpt=?, kind=?,"
            " is_genuine=? WHERE native_id=?",
            (digest, excerpt, kind, is_genuine, native_id))
        r.stats["submissions_updated"] = \
            r.stats.get("submissions_updated", 0) + 1
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
    # The reader's updates source carries the ledger source id; building a
    # second JsonlSource here would repeat its privacy-stale import_errors
    # replacement and wipe the errors this sync just recorded.
    prompts, order, completions_ordered = _collect_prompts(
        updates_path, record_error=None)
    completions_by_id = {}
    for ordinal, update, ts, obj, method, raw in completions_ordered:
        raw_pid = update.get("prompt_id") or update.get("promptId")
        pid = raw_pid if isinstance(
            raw_pid, str) and _safe_token(raw_pid) is not None else None
        if pid is None:
            continue
        if pid not in completions_by_id:
            completions_by_id[pid] = (ordinal, update, ts, obj, method)
    before = con.total_changes
    for key in order:
        _upsert_submission(con, r, r.updates_src, chat, key, prompts[key],
                           completions_by_id)
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

    def record_error(ordinal: int, category: str, line: str = "") -> None:
        # Categories arrive closed (missing_id, schema_error); the ingest
        # path maps anything else to the fallback and keeps only sorted
        # top-level key names from the raw line.
        _record_error_once(src, r.stats, ordinal, category, line)

    prompts, order, completions_ordered = _collect_prompts(
        src.path, record_error=record_error)
    completions_by_id: dict = {}
    for ordinal, update, ts, obj, method, raw in completions_ordered:
        raw_pid = update.get("prompt_id") or update.get("promptId")
        pid = raw_pid if isinstance(
            raw_pid, str) and _safe_token(raw_pid) is not None else None
        if pid is None:
            continue
        if pid not in completions_by_id:
            completions_by_id[pid] = (ordinal, update, ts, obj, method)
        else:
            # Duplicate turn_completed: keep first binding but merge late
            # usage when the first lacked valid counters or was malformed,
            # so a repeated completion can fill NULL counters without
            # shifting any prompt binding. A malformed first never blocks a
            # later valid record for the same prompt id.
            _, first_update, _, _, _ = completions_by_id[pid]
            try:
                first_counters = _validated_usage_counters(first_update)
                first_has = _has_any_counter(first_counters)
                first_ok = True
            except _AdapterError:
                first_has = False
                first_ok = False
            try:
                new_counters = _validated_usage_counters(update)
                new_has = _has_any_counter(new_counters)
                new_ok = True
            except _AdapterError:
                new_has = False
                new_ok = False
            if (not first_ok or not first_has) and new_ok and new_has:
                merged = dict(first_update)
                merged["usage"] = update.get("usage")
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
                           completions_by_id)
    for ordinal, update, ts, obj, method, raw in completions_ordered:
        raw_pid = update.get("prompt_id") or update.get("promptId")
        pid = raw_pid if isinstance(
            raw_pid, str) and _safe_token(raw_pid) is not None else None
        if pid is None:
            continue
        chunk_model = pid_to_model.get(pid)
        _store_response(con, r, src, ordinal, update, ts,
                        _usage_model(update), model_fallback, chunk_model,
                        effort or chat.get("effort"), turn_models, raw)


def _usage_model(update: dict):
    usage = update.get("usage")
    if isinstance(usage, dict):
        model_usage = usage.get("modelUsage")
        if isinstance(model_usage, dict) and len(model_usage) == 1:
            name = next(iter(model_usage))
            if isinstance(name, str) and _safe_token(name) is not None:
                return name
    return None


def _valid_model(value):
    if isinstance(value, str) and value and _safe_token(value) is not None:
        return value
    return None


def _store_response(con, r: _Reader, src: JsonlSource, ordinal: int,
                    update: dict, ts, usage_model, summary_model, chunk_model,
                    effort, turn_models, raw_line: str = "") -> None:
    raw_pid = update.get("prompt_id") or update.get("promptId")
    if not isinstance(raw_pid, str) or _safe_token(raw_pid) is None:
        return
    prompt_id = raw_pid
    response_id = f"{HARNESS}:{r.native_sid}:{prompt_id}"
    try:
        counters = _validated_usage_counters(update)
    except _AdapterError as exc:
        # Present malformed usage: quarantine under the fixed category with
        # only the raw line's top-level key names, never record values or
        # exception text. No partial response row is created; later valid
        # records for other (or the same) prompt ids still import.
        _record_error_once(src, r.stats, ordinal, exc.category, raw_line)
        return
    usage_model = _valid_model(usage_model)
    summary_model = _valid_model(summary_model)
    chunk_model = _valid_model(chunk_model)
    valid_effort = _valid_model(effort)
    # Turn-model entries are pre-validated; drop anything unexpected.
    safe_turns = [(t, m) for t, m in turn_models
                  if isinstance(m, str) and _safe_token(m) is not None]
    model = _model_at(ts, safe_turns, usage_model, summary_model,
                      chunk_model)
    cur = con.execute(
        "INSERT OR IGNORE INTO responses(response_id, source_id, harness,"
        " session_key, turn_id, session_id, ordinal_num, ts, model, effort,"
        " input_tokens, cached_input_tokens, cache_write_input_tokens,"
        " output_tokens, reasoning_output_tokens, total_tokens, semantics)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (response_id, src.source_id, HARNESS, r.session_key, response_id,
         r.native_sid, ordinal, ts, model, valid_effort, counters["input_tokens"],
         counters["cached_input_tokens"],
         counters["cache_write_input_tokens"], counters["output_tokens"],
         counters["reasoning_output_tokens"], counters["total_tokens"],
         SEMANTICS))
    if cur.rowcount:
        r.stats["responses_inserted"] += 1
        return
    existing = con.execute(
        "SELECT input_tokens, cached_input_tokens, cache_write_input_tokens,"
        " output_tokens, reasoning_output_tokens, total_tokens, model, effort"
        " FROM responses WHERE response_id=?", (response_id,)).fetchone()
    if existing is None:
        r.stats["responses_duplicate"] += 1
        return
    for key in counters:
        old, new = existing[key], counters[key]
        if old is not None and new is not None and old != new:
            # A rewritten completion under the same prompt id: quarantine
            # under the closed usage_conflict category with only the raw
            # line's top-level key names, never counters or ids.
            _record_error_once(src, r.stats, ordinal, "usage_conflict",
                               raw_line)
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
    # Late model or effort evidence updates the existing row in place with
    # validated values, without duplicates. A missing new value never
    # clears a known one; only a new valid differing value writes.
    meta_sets: list[str] = []
    meta_args: list = []
    try:
        old_model = existing["model"]
    except (KeyError, TypeError, IndexError):
        old_model = None
    try:
        old_effort = existing["effort"]
    except (KeyError, TypeError, IndexError):
        old_effort = None
    if model is not None and old_model != model:
        meta_sets.append("model=?")
        meta_args.append(model)
    if valid_effort is not None and old_effort != valid_effort:
        meta_sets.append("effort=?")
        meta_args.append(valid_effort)
    if meta_sets:
        meta_args.append(response_id)
        con.execute(
            f"UPDATE responses SET {', '.join(meta_sets)} WHERE response_id=?",
            meta_args)
    r.stats["responses_duplicate"] += 1


def _ingest_update(r: _Reader, src: JsonlSource, obj: dict,
                   ordinal: int) -> None:
    if obj.get("method") not in METHODS:
        raise _AdapterError("unknown_record")
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    update = params.get("update") if isinstance(params, dict) else {}
    if not isinstance(update, dict) or \
            not isinstance(update.get("sessionUpdate"), str):
        raise _AdapterError("unknown_record")
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
        event_id = _valid_native_id(
            params.get("_meta", {}).get("eventId")
            if isinstance(params.get("_meta"), dict) else None)
        r.event(src, "compaction", event_id or f"{kind}:{ordinal}", ordinal,
                ts, name=kind)
    elif kind in EXTENSION_KINDS:
        if kind == "subagent_spawned":
            _subagent_link(r, update)
        event_id = _valid_native_id(
            params.get("_meta", {}).get("eventId")
            if isinstance(params.get("_meta"), dict) else None)
        r.event(src, "lifecycle", event_id or f"{kind}:{ordinal}", ordinal,
                ts, name=kind)
    else:
        raise _AdapterError("unknown_record")


def _turn_id(r: _Reader, params: dict):
    meta = params.get("_meta") if isinstance(params, dict) else None
    prompt_id = meta.get("promptId") if isinstance(meta, dict) else None
    if isinstance(prompt_id, str) and _safe_token(prompt_id) is not None:
        return f"{HARNESS}:{r.native_sid}:{prompt_id}"
    return None


def _tool_name(update: dict):
    # Fail closed: native free-text titles are never stored and never
    # stringified. Only the validated native tool name (x.ai/tool) persists
    # as the event name; when it is absent or malformed the name is the safe
    # "unknown" value without preserving the title.
    meta = update.get("_meta") if isinstance(update.get("_meta"), dict) \
        else {}
    tool = meta.get("x.ai/tool") if isinstance(meta, dict) else None
    name = tool.get("name") if isinstance(tool, dict) else None
    if isinstance(name, str) and name:
        safe = _safe_token(name)
        if safe is not None:
            return safe
    return "unknown"


def _tool_call(r: _Reader, src: JsonlSource, update: dict, params: dict,
               ordinal: int, ts) -> None:
    raw_call = update.get("toolCallId")
    if not isinstance(raw_call, str) or _safe_token(raw_call) is None:
        raise _AdapterError("schema_error")
    call_id = raw_call
    raw = update.get("rawInput")
    name = _tool_name(update)
    target = _safe_target(raw)
    # Tool_call keeps no detail under privacy.py rule 6; the validated name,
    # path-or-command target and argument fingerprint are the ledger columns.
    r.event(src, "tool_call", call_id, ordinal, ts,
            turn_id=_turn_id(r, params), name=name, target=target,
            fingerprint=fingerprint(call_id, name, target or ""))


def _tool_call_update(r: _Reader, src: JsonlSource, update: dict,
                      params: dict, ordinal: int, ts) -> None:
    raw_call = update.get("toolCallId")
    if not isinstance(raw_call, str) or _safe_token(raw_call) is None:
        raise _AdapterError("schema_error")
    call_id = raw_call
    turn_id = _turn_id(r, params)
    locations = update.get("locations")
    if update.get("kind") == "read" and isinstance(locations, list) \
            and locations:
        # Fail closed: every location must be a plain {"path": str} shape
        # with an optional integer line. A location bearing message/output
        # style free-text keys, a non-string path, or a non-integer line is
        # dropped entirely and never stringified into any ledger column.
        paths: list[str] = []
        for loc in locations:
            if not isinstance(loc, dict):
                continue
            if any(k in loc for k in _FREE_TEXT_LOCATION_KEYS):
                continue
            path = loc.get("path")
            if not isinstance(path, str) or not path:
                continue
            paths.append(path)
        if paths:
            for path in paths:
                r.identity.observe_path(path)
            skill = skill_from_path(paths[0])
            # A second location in the same call may name the Skill while the
            # first does not; the Skill read is the identity evidence.
            if skill is None:
                for path in paths[1:]:
                    skill = skill_from_path(path)
                    if skill is not None:
                        break
            if skill is not None and _safe_token(skill) is None:
                skill = None
            # Read events keep no detail under privacy.py rule 6; the first
            # validated path is the target. A skill_read keeps only the
            # validated skill name as detail.
            r.event(src, "skill_read" if skill else "read", call_id,
                    ordinal, ts, turn_id=turn_id,
                    name=skill or os.path.basename(paths[0]),
                    target=paths[0],
                    fingerprint=fingerprint(call_id, paths),
                    detail={"skill": skill} if skill else None)
    status = update.get("status")
    if isinstance(status, str) and status in TERMINAL_TOOL_STATUS:
        # Tool results keep no detail under privacy.py rule 6; the mapped
        # terminal status is the ledger column.
        r.event(src, "tool_result", call_id, ordinal, ts, turn_id=turn_id,
                name=_tool_name(update), status=TERMINAL_TOOL_STATUS[status])


def _subagent_link(r: _Reader, update: dict) -> None:
    parent = update.get("parent_session_id")
    child = update.get("child_session_id") or update.get("subagent_id")
    if not isinstance(parent, str) or _safe_token(parent) is None:
        return
    if not isinstance(child, str) or _safe_token(child) is None:
        return
    if child == r.native_sid:
        r.parent_key = f"{HARNESS}:{parent}"
    if parent and child:
        # The child row may be imported before or after this record; the
        # parent link survives either order because session fields only fill
        # unknowns. When the parent arrives after the child, immediately
        # reclassify the child's existing submissions to synthetic/empty in
        # place (privacy.py rule 1), so import order never leaves genuine
        # child rows behind.
        try:
            db.upsert_session(r.con, f"{HARNESS}:{child}", HARNESS,
                              child, None,
                              parent_session_key=f"{HARNESS}:{parent}")
        except (sqlite3.DatabaseError, ValueError):
            pass
        child_key = f"{HARNESS}:{child}"
        if child_key != r.session_key:
            _force_child_synthetic(r.con, r.stats, child_key)


def _ingest_event(r: _Reader, src: JsonlSource, obj: dict,
                  ordinal: int) -> None:
    if not isinstance(obj.get("type"), str):
        raise _AdapterError("schema_error")
    kind = obj["type"]
    if kind in SKIP_EVENT_TYPES:
        return
    ts = iso_ts(obj.get("ts"))
    r.note_ts(ts)
    if kind == "turn_started":
        # Lifecycle, permission and compaction events keep no detail under
        # privacy.py rule 6. Validated names, statuses and durations are the
        # ledger columns; anything else is dropped, never stringified.
        r.event(src, "lifecycle", f"turn_started:{ordinal}", ordinal, ts,
                name="turn_started")
    elif kind == "turn_ended":
        outcome = _valid_enum(obj.get("outcome"), _OUTCOME_VALUES)
        r.event(src, "lifecycle", f"turn_ended:{ordinal}", ordinal, ts,
                name="turn_ended", status=outcome)
    elif kind == "tool_started":
        tool_s = _safe_token(obj.get("tool_name")) or "unknown"
        r.event(src, "lifecycle", f"tool_started:{ordinal}", ordinal, ts,
                name=tool_s)
    elif kind == "tool_completed":
        _tool_completed(r, src, obj, ordinal, ts)
    elif kind == "permission_requested":
        tool_s = _safe_token(obj.get("tool_name")) or "unknown"
        r.event(src, "permission", f"permission:{ordinal}", ordinal, ts,
                name=tool_s)
    elif kind == "permission_resolved":
        tool_s = _safe_token(obj.get("tool_name")) or "unknown"
        decision_s = _valid_enum(obj.get("decision"), _DECISION_VALUES)
        wait = obj.get("wait_ms")
        if isinstance(wait, bool) or not isinstance(wait, int):
            wait = None
        r.event(src, "permission", f"permission:{ordinal}", ordinal, ts,
                name=tool_s, status=decision_s, duration_ms=wait)
    else:
        raise _AdapterError("unknown_record")


def _tool_completed(r: _Reader, src: JsonlSource, obj: dict, ordinal: int,
                    ts) -> None:
    raw_call = obj.get("tool_call_id")
    if not isinstance(raw_call, str) or _safe_token(raw_call) is None:
        raise _AdapterError("schema_error")
    call_id = raw_call
    outcome = obj.get("outcome")
    status = "ok" if outcome in ("success", "completed") else "error"
    duration = obj.get("duration_ms")
    if isinstance(duration, bool) or not isinstance(duration, int):
        duration = None
    row = r.con.execute(
        "SELECT id, status, duration_ms, detail_json FROM events"
        " WHERE session_key=? AND family='tool_result' AND native_id=?",
        (r.session_key, call_id)).fetchone()
    if row is None:
        tool_s = _safe_token(obj.get("tool_name")) or "unknown"
        r.event(src, "tool_result", call_id, ordinal, ts,
                name=tool_s, status=status, duration_ms=duration)
        return
    try:
        loaded = json.loads(row["detail_json"]) if row["detail_json"] else {}
    except ValueError:
        loaded = {}
    # Scrub any legacy values through privacy.py rule 6: only allowlisted
    # tool_result keys with correctly typed values survive, so unknown keys
    # and free text are dropped, never stringified.
    detail = privacy.filter_detail("tool_result", loaded)
    payload = json.dumps(detail, sort_keys=True) if detail else None
    changed = payload != row["detail_json"]
    if row["duration_ms"] is None and duration is not None:
        changed = True
    if row["status"] is None and status:
        changed = True
    if changed:
        r.con.execute(
            "UPDATE events SET duration_ms=COALESCE(duration_ms, ?),"
            " status=COALESCE(status, ?), detail_json=? WHERE id=?",
            (duration, status, payload,
             row["id"]))
