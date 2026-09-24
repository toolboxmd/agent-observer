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

import hashlib
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

# Benign MCP server lifecycle/config event shapes: recognized and ignored.
# Storing nothing keeps server names, targets, transports, errors, tools
# and other MCP values out of the ledger with no new privacy allowlist.
# Exact top-level key sets only; a known MCP type with any other shape
# stays unknown_record. Observed on this Mac across 1,127 events.jsonl
# files (read-only aggregate of type values and sorted key names).
_MCP_IGNORED_SHAPES: dict[str, set[frozenset]] = {
    "mcp_server_starting": {
        frozenset({"server_name", "target", "timeout_sec", "transport",
                   "ts", "type"}),
    },
    "mcp_config_resolved": {
        frozenset({"disabled", "servers", "ts", "type"}),
    },
    "mcp_server_connected": {
        frozenset({"duration_ms", "server_name", "tool_count", "tools",
                   "transport", "ts", "type"}),
    },
    "mcp_init_completed": {
        frozenset({"auth_required", "duration_ms", "failed", "is_reinit",
                   "succeeded", "total_servers", "total_tools", "ts",
                   "type"}),
        frozenset({"auth_required", "duration_ms", "failed", "failed_servers",
                   "is_reinit", "succeeded", "total_servers", "total_tools",
                   "ts", "type"}),
    },
    "mcp_server_failed": {
        frozenset({"duration_ms", "error_message", "error_type",
                   "server_name", "target", "timeout_sec", "transport",
                   "ts", "type"}),
    },
}

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
    if _SAFE_TOKEN_RE.fullmatch(value) is None:
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


class _SyncCtx:
    """Per-operation shared state: linear parent lookup with no rescans.

    parent_index maps child native session id -> parent session key. For a
    full-tree sync it accumulates from each imported session's own
    subagent_spawned records in the single sync pass (plus persisted ledger
    links). For a source-scoped sync, sync() builds the source's
    containing-tree index once before importing, so a child whose only
    parent evidence lives in a sibling file still classifies correctly. A
    child imported before its parent misses the index but is reclassified
    in place when the parent later imports via _subagent_link.
    import_grok_session receives this index from its caller and never scans
    sibling files itself.
    """

    def __init__(self) -> None:
        self.parent_index: dict[str, str] = {}


def sync(con: sqlite3.Connection, root: str | None = None, full: bool = False,
         source: str | None = None) -> dict:
    """Import every session under root, or one session directory."""
    paths = _resolve_source(source, root) if source else discover(root)
    totals = {"harness": HARNESS, "sources": 0, "unchanged": 0,
              "responses_inserted": 0, "events_inserted": 0,
              "submissions_inserted": 0, "malformed": 0, "failed": []}
    ctx = _SyncCtx()
    if source is not None:
        # Source-scoped operation: build the containing-tree parent index
        # once before importing, regardless of each session's role. A
        # child already proven subagent by summary or events can still be
        # missing its parent link, and the link may live only in a sibling
        # file outside the sync set.
        _build_source_tree_index(con, ctx, paths)
    for path in paths:
        try:
            stats = import_grok_session(con, path, full=full, _sync_ctx=ctx)
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
        if content.get("type") != "text":
            return ""
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
    def __init__(self, con, session_key, native_sid, stats, sync_ctx=None):
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
        self.sync_ctx = sync_ctx

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


_GROK_META_TABLE = "grok_session_meta"


def _ensure_grok_meta_table(con: sqlite3.Connection) -> None:
    """Adapter-owned summary/chat fingerprints for change detection.

    An unchanged second sync must not fully parse updates.jsonl,
    events.jsonl, or chat_history.jsonl. The ledger already persists parent
    links and roles in sessions; this table persists the summary fingerprint
    plus the chat raw fingerprint and its parsed marks (seen/synthetic
    prompt indexes, effort, reliability), so a later sync can reuse the
    marks from the ledger when the raw bytes are unchanged and only
    JSON-parse the chat file when its bytes actually changed.
    """
    try:
        con.execute(
            f"CREATE TABLE IF NOT EXISTS {_GROK_META_TABLE}"
            "(session_key TEXT PRIMARY KEY,"
            " summary_fp TEXT, chat_fp TEXT)")
    except sqlite3.DatabaseError:
        pass
    for column in ("chat_seen_json", "chat_synthetic_json", "chat_effort",
                   "chat_reliable"):
        try:
            cols = {row["name"] for row in con.execute(
                f"PRAGMA table_info({_GROK_META_TABLE})")}
        except sqlite3.DatabaseError:
            return
        if column not in cols:
            try:
                con.execute(
                    f"ALTER TABLE {_GROK_META_TABLE} ADD COLUMN {column} TEXT")
            except sqlite3.DatabaseError:
                pass


def _summary_fingerprint(summary: dict) -> str:
    try:
        canonical = json.dumps(summary, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = repr(sorted(summary.keys()))
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()


def _stored_meta(con: sqlite3.Connection, session_key: str):
    """Stored chat/summary state or Nones when absent/unreadable.

    Returns (summary_fp, chat_fp, seen_set, synthetic_set, chat_effort,
    chat_reliable). Mark collections are None when the row or its mark
    columns are absent, so the caller falls back to parsing the chat file.
    """
    try:
        row = con.execute(
            f"SELECT summary_fp, chat_fp, chat_seen_json,"
            f" chat_synthetic_json, chat_effort, chat_reliable"
            f" FROM {_GROK_META_TABLE}"
            " WHERE session_key=?", (session_key,)).fetchone()
    except sqlite3.DatabaseError:
        return None, None, None, None, None, None
    if row is None:
        return None, None, None, None, None, None
    try:
        summary_fp = row["summary_fp"]
        chat_fp = row["chat_fp"]
    except (KeyError, TypeError, IndexError):
        return None, None, None, None, None, None
    try:
        seen_raw = row["chat_seen_json"]
        synth_raw = row["chat_synthetic_json"]
        chat_effort = row["chat_effort"]
        reliable_raw = row["chat_reliable"]
    except (KeyError, TypeError, IndexError):
        return summary_fp, chat_fp, None, None, None, None
    try:
        seen = set(json.loads(seen_raw)) if seen_raw else set()
        synthetic = set(json.loads(synth_raw)) if synth_raw else set()
    except (TypeError, ValueError):
        return summary_fp, chat_fp, None, None, None, None
    if reliable_raw is None:
        reliable = None
    elif isinstance(reliable_raw, int):
        reliable = bool(reliable_raw)
    elif isinstance(reliable_raw, str):
        reliable = reliable_raw == "1"
    else:
        reliable = None
    return summary_fp, chat_fp, seen, synthetic, chat_effort, reliable


def _store_meta(con: sqlite3.Connection, session_key: str,
                summary_fp: str | None, chat_fp: str | None,
                chat: dict | None = None) -> None:
    """Persist fingerprints plus parsed chat marks for the fast path."""
    _ensure_grok_meta_table(con)
    if chat is None:
        seen_json = synth_json = None
        chat_effort = None
        chat_reliable = None
    else:
        try:
            seen_json = json.dumps(sorted(chat.get("seen") or set()))
            synth_json = json.dumps(sorted(chat.get("synthetic") or set()))
        except (TypeError, ValueError):
            seen_json = synth_json = None
        chat_effort = chat.get("effort")
        reliable = chat.get("reliable")
        chat_reliable = None if reliable is None else (
            "1" if reliable else "0")
    try:
        con.execute(
            f"INSERT OR REPLACE INTO {_GROK_META_TABLE}"
            "(session_key, summary_fp, chat_fp, chat_seen_json,"
            " chat_synthetic_json, chat_effort, chat_reliable)"
            " VALUES(?,?,?,?,?,?,?)",
            (session_key, summary_fp, chat_fp, seen_json, synth_json,
             chat_effort, chat_reliable))
    except sqlite3.DatabaseError:
        pass


def _chat_fingerprint(session_dir: str) -> str:
    """Raw-byte hash of chat_history.jsonl without any JSON parsing.

    A small raw read every sync is the only per-sync chat I/O on the fast
    path; the JSON parse below runs only when these bytes changed. Missing
    files fingerprint as "missing" so a late-arriving chat file still
    counts as changed.
    """
    path = os.path.join(session_dir, "chat_history.jsonl")
    try:
        with open(path, "rb") as bfh:
            raw_bytes = bfh.read()
    except OSError:
        return "missing"
    try:
        return hashlib.sha256(raw_bytes).hexdigest()
    except (TypeError, ValueError):
        return "unhashable"


def _chat_from_stored(chat_fp: str | None, seen, synthetic,
                      chat_effort, chat_reliable) -> dict | None:
    """Rebuild the chat marks dict from persisted ledger state.

    Returns None when any mark is missing so the caller parses the file
    instead. No file I/O and no JSON parsing of chat records happens here.
    """
    if seen is None or synthetic is None or chat_reliable is None:
        return None
    return {"synthetic": set(synthetic), "seen": set(seen),
            "effort": chat_effort, "reliable": bool(chat_reliable),
            "fp": chat_fp}



def _sibling_updates_unchanged(con: sqlite3.Connection,
                               updates_path: str) -> bool:
    """Whether a sibling updates.jsonl is already imported and unchanged.

    A cheap stat plus tail read, never a full parse. Unchanged siblings
    already contributed their spawns to persisted session links on their
    own import, so the tree index can skip reparsing them. The stored
    size, mtime_ns and inode must all agree with the current file (rows
    written before they were recorded fail closed to changed), and the
    size/mtime are rechecked after the tail read so an append racing the
    check is not reported unchanged.
    """
    try:
        row = con.execute(
            "SELECT read_offset, size_bytes, tail_sha256, mtime_ns, ino,"
            " privacy_version FROM sources"
            " WHERE harness=? AND path=?", (HARNESS, updates_path)).fetchone()
    except sqlite3.DatabaseError:
        return False
    if row is None:
        return False
    try:
        stored_version = row["privacy_version"]
        offset = row["read_offset"]
        tail = row["tail_sha256"]
    except (KeyError, TypeError, IndexError):
        return False
    if stored_version != privacy.PRIVACY_VERSION:
        return False
    try:
        keys = row.keys()
        stored_size = row["size_bytes"] if "size_bytes" in keys else None
        stored_mtime = row["mtime_ns"] if "mtime_ns" in keys else None
        stored_ino = row["ino"] if "ino" in keys else None
    except (TypeError, IndexError):
        return False
    if (not isinstance(offset, int) or not tail
            or not isinstance(stored_size, int)
            or stored_mtime is None or stored_ino is None):
        # Pre-metadata rows fail closed until a fresh import records them.
        return False
    try:
        st = os.stat(updates_path)
    except OSError:
        return False
    if st.st_ino != stored_ino or st.st_size != stored_size:
        return False
    try:
        if st.st_mtime_ns != stored_mtime:
            return False
    except AttributeError:
        return False
    if offset != st.st_size:
        return False
    if offset <= 0:
        try:
            st2 = os.stat(updates_path)
        except OSError:
            return False
        return (st2.st_size == st.st_size
                and st2.st_mtime_ns == st.st_mtime_ns
                and st2.st_ino == st.st_ino)
    try:
        with open(updates_path, "rb") as fh:
            start = max(0, offset - 4096)
            fh.seek(start)
            digest = hashlib.sha256(fh.read(offset - start)).hexdigest()
    except OSError:
        return False
    if digest != tail:
        return False
    try:
        st2 = os.stat(updates_path)
    except OSError:
        return False
    return (st2.st_size == st.st_size
            and st2.st_mtime_ns == st.st_mtime_ns
            and st2.st_ino == st.st_ino)


def _build_source_tree_index(con: sqlite3.Connection, ctx,
                             paths: list[str]) -> None:
    """Build the containing-tree parent index once per source-scoped op.

    Called by sync() before any import, never by import_grok_session.
    Scans each sibling updates.jsonl in the sync set's containing groups at
    most once for subagent_spawned records and merges them into
    ctx.parent_index, persisting child links so later syncs can consult the
    ledger instead of reparsing. Siblings already imported and unchanged are
    skipped via a stat/tail check without parsing; their spawns already
    reached persisted session links on their own import. Siblings in the
    sync set itself are skipped here because their own import parse will
    contribute their spawns to the shared index.
    """
    if ctx is None:
        return
    try:
        in_set = {os.path.abspath(p) for p in paths}
    except OSError:
        in_set = set()
    groups: list[str] = []
    seen_groups: set[str] = set()
    for path in paths:
        try:
            group = os.path.dirname(os.path.abspath(path.rstrip(os.sep)))
        except OSError:
            continue
        if group not in seen_groups:
            seen_groups.add(group)
            groups.append(group)
    for group in groups:
        try:
            children = sorted(os.listdir(group))
        except OSError:
            continue
        for child in children:
            sibling = os.path.join(group, child)
            try:
                if os.path.abspath(sibling) in in_set:
                    continue
            except OSError:
                continue
            try:
                if not (os.path.isdir(sibling)
                        and _looks_like_session(sibling)):
                    continue
            except OSError:
                continue
            updates_path = os.path.join(sibling, "updates.jsonl")
            if not os.path.isfile(updates_path):
                continue
            if _sibling_updates_unchanged(con, updates_path):
                continue
            try:
                records = _load_jsonl_records(updates_path)
            except OSError:
                continue
            try:
                spawns = _extract_spawns_from_records(records)
            except (AttributeError, TypeError):
                continue
            for child_sid, pkey in spawns.items():
                try:
                    ctx.parent_index.setdefault(child_sid, pkey)
                except (AttributeError, TypeError):
                    pass
                child_key = f"{HARNESS}:{child_sid}"
                try:
                    db.upsert_session(con, child_key, HARNESS, child_sid, None,
                                      parent_session_key=pkey, role="subagent")
                except (sqlite3.DatabaseError, ValueError):
                    pass
                _force_child_synthetic(con, None, child_key)


def _precompute_child_status(con: sqlite3.Connection, r: _Reader,
                             updates_records: list | None = None,
                             events_records: list | None = None,
                             parent_index: dict | None = None) -> None:
    """Set parent link and subagent role before any replay.

    Order: summary role (already in r.meta), persisted
    sessions.parent_session_key/role, the operation parent index plus this
    session's own spawn records, and events turn_started
    session_relationship='subagent' from cached records. A proven child is
    non-genuine with an empty excerpt via privacy.py rule 1. A spawn-proven
    parent link also marks the subagent role, so a child whose only evidence
    is a sibling's dispatch still carries the role. Per-session code never
    scans sibling files: the caller supplies the index.
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
    if parent_index is not None and r.parent_key is None:
        indexed = parent_index.get(r.native_sid)
        if indexed is not None:
            r.parent_key = indexed
    if r.parent_key is None and updates_records is not None:
        found = _find_parent_in_records(updates_records, r.native_sid)
        if found is not None:
            r.parent_key = found
    if r.meta.get("role") != "subagent" and events_records is not None:
        if _has_subagent_in_records(events_records):
            r.meta["role"] = "subagent"
    if r.parent_key is not None and r.meta.get("role") != "subagent":
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


def _validated_usage_model(update: dict) -> str | None:
    """modelUsage name only when usage counters validate.

    Malformed present usage never yields metadata: the completion is
    quarantined elsewhere, and its modelUsage must not become a response
    model or downgrade a recorded one. Absent/null/empty usage yields None.
    """
    try:
        _validated_usage_counters(update)
    except _AdapterError:
        return None
    usage = update.get("usage")
    if not isinstance(usage, dict):
        return None
    model_usage = usage.get("modelUsage")
    if isinstance(model_usage, dict) and len(model_usage) == 1:
        name = next(iter(model_usage))
        if isinstance(name, str) and _safe_token(name) is not None:
            return name
    return None


def _counters_compatible(first: dict | None, new: dict | None) -> bool:
    """Whether two validated counter dicts can merge without conflict."""
    if not first or not new:
        return True
    for key in first:
        old, cur = first.get(key), new.get(key)
        if old is not None and cur is not None and old != cur:
            return False
    return True


def _deduplicate_completions(completions_ordered) -> dict:
    """Shared first-wins selection with late valid fill for bindings.

    Keeps the first ordinal/ts/obj/method per prompt id for stable prompt
    bindings, but merges late valid usage (counters and modelUsage) when the
    first lacked valid counters, was malformed, or lacked a validated model
    while the later brings a compatible one. A malformed or conflicting
    later completion never replaces the selected usage; conflicting
    counters stay for the response path to quarantine. Later valid
    modelUsage wins over fallback evidence. Returns dict pid ->
    (ordinal, update, ts, obj, method, raw).
    """
    selected: dict = {}
    for ordinal, update, ts, obj, method, raw in completions_ordered:
        raw_pid = update.get("prompt_id") or update.get("promptId")
        pid = raw_pid if isinstance(raw_pid, str) \
            and _safe_token(raw_pid) is not None else None
        if pid is None:
            continue
        if pid not in selected:
            selected[pid] = (ordinal, update, ts, obj, method, raw)
            continue
        _, first_update, _, _, _, _ = selected[pid]
        try:
            first_counters = _validated_usage_counters(first_update)
            first_ok = True
            first_has = _has_any_counter(first_counters)
        except _AdapterError:
            first_ok = False
            first_has = False
            first_counters = None
        try:
            new_counters = _validated_usage_counters(update)
            new_ok = True
            new_has = _has_any_counter(new_counters)
        except _AdapterError:
            new_ok = False
            new_has = False
            new_counters = None
        if not new_ok:
            continue
        first_model = _validated_usage_model(first_update) \
            if first_ok else None
        new_model = _validated_usage_model(update)
        if not first_ok or not first_has:
            if new_has or new_model is not None:
                merged = dict(first_update)
                merged["usage"] = update.get("usage")
                selected[pid] = (selected[pid][0], merged,
                                 selected[pid][2], selected[pid][3],
                                 selected[pid][4], selected[pid][5])
        elif first_model is None and new_model is not None:
            if _counters_compatible(first_counters, new_counters):
                merged = dict(first_update)
                merged["usage"] = update.get("usage")
                selected[pid] = (selected[pid][0], merged,
                                 selected[pid][2], selected[pid][3],
                                 selected[pid][4], selected[pid][5])
        elif new_model is not None and new_model != first_model:
            if _counters_compatible(first_counters, new_counters):
                merged = dict(first_update)
                merged["usage"] = update.get("usage")
                selected[pid] = (selected[pid][0], merged,
                                 selected[pid][2], selected[pid][3],
                                 selected[pid][4], selected[pid][5])
    return selected


def _model_ranks(model, turn_m, usage_models, summary_m, chunk_m) -> int:
    """Evidence rank for a model value: turn 3, usage 2, summary 1, chunk 0.

    usage_models is a set of validated usage models for the prompt. Unknown
    or missing values rank -1 so a current best can replace stale evidence,
    while a weaker fallback never downgrades a stronger recorded value.
    """
    if model is None:
        return -1
    if turn_m is not None and model == turn_m:
        return 3
    if usage_models and model in usage_models:
        return 2
    if summary_m is not None and model == summary_m:
        return 1
    if chunk_m is not None and model == chunk_m:
        return 0
    return -1


_PROVENANCE_TABLE = "grok_response_provenance"


def _ensure_provenance_table(con: sqlite3.Connection) -> None:
    """Adapter-owned evidence provenance: stored model/effort ranks.

    Without stored ranks, a rewritten file that drops the old usage model
    makes the old value rank -1 under current evidence, letting the summary
    fallback win. Stored ranks let stronger validated turn/usage evidence
    beat later fallback evidence across syncs.
    """
    try:
        con.execute(
            f"CREATE TABLE IF NOT EXISTS {_PROVENANCE_TABLE}"
            "(response_id TEXT PRIMARY KEY,"
            " model_rank INTEGER, effort_rank INTEGER)")
    except sqlite3.DatabaseError:
        pass


def _read_provenance(con: sqlite3.Connection, response_id: str):
    """(model_rank, effort_rank) or (None, None) when absent/unreadable."""
    try:
        row = con.execute(
            f"SELECT model_rank, effort_rank FROM {_PROVENANCE_TABLE}"
            " WHERE response_id=?", (response_id,)).fetchone()
    except sqlite3.DatabaseError:
        return None, None
    if row is None:
        return None, None
    try:
        return row["model_rank"], row["effort_rank"]
    except (KeyError, TypeError, IndexError):
        return None, None


def _write_provenance(con: sqlite3.Connection, response_id: str,
                      model_rank=None, effort_rank=None) -> None:
    """Insert or converge stored ranks without touching ledger counters."""
    _ensure_provenance_table(con)
    try:
        cur = con.execute(
            f"INSERT OR IGNORE INTO {_PROVENANCE_TABLE}"
            "(response_id, model_rank, effort_rank) VALUES(?,?,?)",
            (response_id, model_rank, effort_rank))
    except sqlite3.DatabaseError:
        return
    if cur.rowcount:
        return
    sets: list[str] = []
    args: list = []
    if model_rank is not None:
        sets.append("model_rank=?")
        args.append(model_rank)
    if effort_rank is not None:
        sets.append("effort_rank=?")
        args.append(effort_rank)
    if not sets:
        return
    args.append(response_id)
    try:
        con.execute(
            f"UPDATE {_PROVENANCE_TABLE} SET {', '.join(sets)}"
            " WHERE response_id=?", args)
    except sqlite3.DatabaseError:
        pass


def _has_unproven_model_usage(update: dict) -> bool:
    """Whether a valid-counters update carries present-but-invalid modelUsage.

    A modelUsage key that is present with a non-empty, non-None value that
    fails validation (wrong shape, unsafe name, multi-key, non-dict) is
    unproven evidence: the current model falls back to summary, but that
    fallback must not downgrade a previously recorded valid usage model.
    Missing keys and empty-dict/None values are clean absent evidence (the
    existing empty-modelUsage contract) and return False.
    """
    try:
        _validated_usage_counters(update)
    except _AdapterError:
        return False
    usage = update.get("usage")
    if not isinstance(usage, dict):
        return False
    if "modelUsage" not in usage:
        return False
    mu = usage.get("modelUsage")
    if mu is None or (isinstance(mu, dict) and len(mu) == 0):
        return False
    return _validated_usage_model(update) is None


def _reconcile_response_metadata(con: sqlite3.Connection, r: _Reader,
                                 session_dir: str, model_fallback,
                                 summary_effort, chat_effort=None,
                                 updates_records: list | None = None,
                                 events_records: list | None = None,
                                 precomputed: tuple | None = None) -> int:
    """Update stale or NULL model/effort on existing responses in place.

    Recomputes validated model and effort with the existing precedence
    (turn_started stream, usage modelUsage, summary fallback, chunk model;
    summary effort else chat effort) from current summary/events files.
    Reuses the replay duplicate-selection logic so a later valid modelUsage
    wins over fallback evidence, validates usage before accepting its
    metadata, and never lets weaker evidence downgrade a valid stronger
    model or effort already recorded. Stored evidence provenance
    (grok_response_provenance) decides stronger-wins across syncs; a
    malformed present usage, counters conflicting with the stored row, or
    present-but-invalid modelUsage preserve the recorded model (effort
    still applies). Only a new valid value differing from the stored one
    updates the row; a missing new value never clears a known one. Never
    inserts or duplicates rows; counters and usage_conflict behavior are
    untouched. Returns rows changed.

    Both record sets must be supplied by the caller from its single cached
    parse; a missing set means the file was deliberately not loaded (known
    and unchanged with no metadata change) and there is nothing to
    reconcile, so this returns 0 without opening any file.
    """
    if precomputed is not None:
        prompts, order, completions_ordered, turn_models = precomputed
    elif updates_records is not None and events_records is not None:
        prompts, order, completions_ordered = _collect_prompts_from_records(
            updates_records, record_error=None)
        turn_models = _turn_models_from_records(events_records)
    else:
        return 0
    pid_to_model: dict = {}
    for key in order:
        pid = prompts[key].get("prompt_id")
        if pid and pid not in pid_to_model and prompts[key].get("model"):
            pid_to_model[pid] = prompts[key]["model"]
    safe_turns = [(t, m) for t, m in turn_models
                  if isinstance(m, str) and _safe_token(m) is not None]
    # Reuse the replay duplicate-selection logic: stable first binding with
    # late valid usage/model merged, so later valid modelUsage wins and an
    # unchanged re-sync retains it instead of reverting to the fallback.
    selected = _deduplicate_completions(completions_ordered)
    # All validated usage models per pid for strength comparison: an
    # existing model matching any of them is strong evidence.
    all_usage_models: dict = {}
    for _ordinal, update, _ts, _obj, _method, _raw in completions_ordered:
        raw_pid = update.get("prompt_id") or update.get("promptId")
        pid = raw_pid if isinstance(raw_pid, str) \
            and _safe_token(raw_pid) is not None else None
        if pid is None:
            continue
        umodel = _validated_usage_model(update)
        if umodel is not None:
            all_usage_models.setdefault(pid, set()).add(umodel)
    valid_summary_effort = _valid_model(summary_effort)
    valid_chat_effort = _valid_model(chat_effort)
    valid_effort = valid_summary_effort or valid_chat_effort
    new_effort_rank = -1
    if valid_effort is not None:
        new_effort_rank = 1 if valid_effort == valid_summary_effort else 0
    changed = 0
    for pid, (_ordinal, update, ts, _obj, _method, _raw) in selected.items():
        response_id = f"{HARNESS}:{r.native_sid}:{pid}"
        try:
            existing = con.execute(
                "SELECT model, effort, input_tokens, cached_input_tokens,"
                " cache_write_input_tokens, output_tokens,"
                " reasoning_output_tokens, total_tokens"
                " FROM responses WHERE response_id=?",
                (response_id,)).fetchone()
        except sqlite3.DatabaseError:
            continue
        if existing is None:
            continue
        # Fail closed on current usage: a malformed present usage never
        # yields metadata, and counters conflicting with the stored row
        # never downgrade a recorded model to the fallback. Both preserve
        # the existing model; effort (summary/chat evidence) still applies.
        try:
            cur_counters = _validated_usage_counters(update)
            cur_malformed = False
        except _AdapterError:
            cur_malformed = True
            cur_counters = None
        chunk_model = _valid_model(pid_to_model.get(pid))
        usage_model = _validated_usage_model(update)
        summary_model = _valid_model(model_fallback)
        new_model = _model_at(ts, safe_turns, usage_model, summary_model,
                              chunk_model)
        # Applicable turn at this completion for rank comparison.
        turn_m = None
        for started, name in safe_turns:
            if ts is not None and started <= ts:
                turn_m = name
        usage_set = all_usage_models.get(pid, set())
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
            preserve_model = cur_malformed or _has_unproven_model_usage(update)
            if not preserve_model and cur_counters is not None:
                try:
                    stored_counters = {
                        key: existing[key]
                        for key, _ in _GROK_USAGE_MAP}
                except (KeyError, TypeError, IndexError):
                    stored_counters = None
                if stored_counters is not None and not _counters_compatible(
                        stored_counters, cur_counters):
                    preserve_model = True
            if not preserve_model:
                new_rank = _model_ranks(new_model, turn_m, usage_set,
                                        summary_model, chunk_model)
                stored_model_rank, stored_effort_rank = _read_provenance(
                    con, response_id)
                if stored_model_rank is not None:
                    # Stored provenance wins over recomputation: a recorded
                    # usage/turn model (2/3) beats a later fallback (1/0).
                    # Equal ranks allow a changed summary fallback to update
                    # a prior summary-only model.
                    if old_model is None or new_rank >= stored_model_rank:
                        sets.append("model=?")
                        args.append(new_model)
                else:
                    if old_model is None:
                        sets.append("model=?")
                        args.append(new_model)
                    elif new_rank >= 2:
                        # No provenance: fail safe by keeping the existing
                        # value unless the rewrite proves a turn (3) or
                        # usage (2) model. Summary/chunk fallback (1/0)
                        # never overwrites a provenance-less value.
                        sets.append("model=?")
                        args.append(new_model)
                    # Else preserve the existing model: a provenance-less
                    # row keeps its value against fallback evidence.
        if valid_effort is not None and old_effort != valid_effort:
            if old_effort is None:
                sets.append("effort=?")
                args.append(valid_effort)
            else:
                _, _stored_erank = _read_provenance(con, response_id)
                if _stored_erank is not None:
                    if new_effort_rank >= _stored_erank:
                        sets.append("effort=?")
                        args.append(valid_effort)
                else:
                    # No provenance: preserve the existing effort. Summary
                    # or chat fallback never overwrites a provenance-less
                    # value; only a missing value is filled above.
                    pass
        if sets:
            args.append(response_id)
            try:
                con.execute(
                    f"UPDATE responses SET {', '.join(sets)}"
                    " WHERE response_id=?", args)
            except sqlite3.DatabaseError:
                continue
            _m_rank_to_store = None
            _e_rank_to_store = None
            if "model=?" in sets:
                _m_rank_to_store = _model_ranks(
                    new_model, turn_m, usage_set, summary_model, chunk_model)
            if "effort=?" in sets:
                _e_rank_to_store = new_effort_rank
            _write_provenance(con, response_id, _m_rank_to_store,
                              _e_rank_to_store)
            changed += 1
    return changed


def _advance_source_to_cached(src, path: str, records: list) -> None:
    """Advance a JsonlSource's offsets to the cached full read without I/O.

    Consumes the single parse for finish() bookkeeping: when the file ends
    with a newline the end is the file size, otherwise the byte offset just
    after the last complete line (found via small tail reads). A trailing
    partial line stays for the next import. Blank lines advance the offset
    but never the ordinal, matching JsonlSource.records().
    """
    if src is None:
        return
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size == 0:
        src.end_offset = 0
        return
    try:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            ends_newline = fh.read(1) == b"\n"
    except OSError:
        return
    if ends_newline:
        src.end_offset = size
        if records:
            try:
                src.last_ordinal = max(o for o, _, _ in records)
            except ValueError:
                pass
        return
    try:
        with open(path, "rb") as fh:
            chunk_size = 65536
            pos = size
            found = None
            while pos > 0:
                read_size = min(chunk_size, pos)
                pos -= read_size
                fh.seek(pos)
                chunk = fh.read(read_size)
                idx = chunk.rfind(b"\n")
                if idx != -1:
                    found = pos + idx + 1
                    break
            if found is not None:
                src.end_offset = found
                if records:
                    src.last_ordinal = max(o for o, _, _ in records)
    except OSError:
        pass


def _delta_records(records: list, src) -> list:
    """Cached records after the source's start offset (no re-read).

    Ordinals count non-blank complete lines from the start in both the
    cache and JsonlSource, so filtering by the stored ordinal_max replays
    exactly the lines records() would yield. A privacy-stale or full
    re-read has start_offset 0 and yields the whole cache.
    """
    if src is None:
        return []
    if not getattr(src, "incremental", False):
        return list(records)
    try:
        row = src.row
        start_ord = (row["ordinal_max"] + 1) \
            if row is not None and row["ordinal_max"] is not None else 0
    except (KeyError, TypeError, IndexError):
        start_ord = 0
    return [rec for rec in records if rec[0] >= start_ord]


def import_grok_session(con: sqlite3.Connection, session_dir: str,
                        full: bool = False, parent_index=None,
                        _sync_ctx=None) -> dict:
    """Import one Grok session directory. Idempotent; growing logs resume.

    Change detection runs before any updates.jsonl/events.jsonl parse: the
    JsonlSource size/mtime/inode plus tail check decides unchanged without
    reading the whole file, and an unchanged known session with unchanged
    summary/chat metadata and no new parent link returns without parsing
    either file. Both JsonlSource decisions are refreshed after
    construction and rechecked immediately before the fully unchanged
    return, so a file that grows in between still parses on this sync.
    The caller supplies the operation parent index (sync() builds it once
    per operation); per-session code never scans sibling files.
    """
    stats = {"lines": 0, "responses_inserted": 0, "responses_duplicate": 0,
             "submissions_inserted": 0, "events_inserted": 0,
             "events_duplicate": 0, "compactions": 0, "malformed": 0}
    updates_path = os.path.join(session_dir, "updates.jsonl")
    events_path = os.path.join(session_dir, "events.jsonl")
    summary = _read_json(os.path.join(session_dir, "summary.json")) or {}
    info = summary.get("info") if isinstance(summary.get("info"), dict) else {}
    # Fail closed on unsafe identifiers: the native session id comes only
    # from a validated summary.info.id or a validated directory basename.
    # Anything else never reaches session keys, response ids, submission
    # ids or event keys.
    basename = os.path.basename(session_dir.rstrip(os.sep))
    safe_basename = _valid_native_id(basename)
    raw_info_id = info.get("id")
    valid_info_id = _valid_native_id(raw_info_id) \
        if isinstance(raw_info_id, str) and raw_info_id else None
    native_sid = valid_info_id or safe_basename
    if native_sid is None:
        # No safe native id: quarantine under the fixed missing_id
        # category with no record values. The source_path locates the
        # directory; error and excerpt never carry the raw name.
        category = privacy.error_category("missing_id")
        existing = con.execute(
            "SELECT 1 FROM import_errors WHERE harness=? AND source_path=?"
            " AND ordinal_num=? AND error=?",
            (HARNESS, session_dir, 0, category)).fetchone()
        if existing is None:
            con.execute(
                "INSERT INTO import_errors(harness, source_path, ordinal_num,"
                " error, line_excerpt, created_at) VALUES(?,?,?,?,?,?)",
                (HARNESS, session_dir, 0, category,
                 privacy.line_excerpt(""), db.now()))
            stats["malformed"] = stats.get("malformed", 0) + 1
            con.commit()
        return stats
    # Resolve the shared operation index. sync() passes _sync_ctx with the
    # operation parent index (source-scoped tree index prebuilt there);
    # direct callers may pass a parent_index dict built once for their
    # operation, or nothing for a lone session relying on its own records
    # plus persisted ledger links. Per-session code never scans siblings.
    ctx = _sync_ctx
    if ctx is not None:
        try:
            effective_index = ctx.parent_index
        except AttributeError:
            effective_index = {}
            try:
                ctx.parent_index = effective_index
            except (AttributeError, TypeError):
                pass
    elif isinstance(parent_index, dict):
        effective_index = parent_index
        ctx = _SyncCtx()
        ctx.parent_index = effective_index
    else:
        effective_index = {}
        ctx = _SyncCtx()
        ctx.parent_index = effective_index
    r = _Reader(con, f"{HARNESS}:{native_sid}", native_sid, stats,
                sync_ctx=ctx)
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

    # Change state before any full parse. JsonlSource does a stat plus a
    # small tail read, never a full parse.
    updates_src = (JsonlSource(con, HARNESS, updates_path, full=full)
                   if os.path.isfile(updates_path) else None)
    events_src = (JsonlSource(con, HARNESS, events_path, full=full)
                  if os.path.isfile(events_path) else None)
    r.updates_src = updates_src
    r.events_src = events_src
    known_row = con.execute("SELECT 1 FROM sessions WHERE session_key=?",
                            (r.session_key,)).fetchone()
    known = known_row is not None
    # Refresh the initial JsonlSource decisions before judging: a file
    # appended between the two constructions must not be judged by a stale
    # flag. Recheck is stat/tail only, no SQL and no parse.
    if updates_src is not None and updates_src.unchanged:
        updates_src.recheck_unchanged()
    if events_src is not None and events_src.unchanged:
        events_src.recheck_unchanged()
    updates_new = full or updates_src is None or not updates_src.unchanged
    events_new = full or events_src is None or not events_src.unchanged
    privacy_stale = bool(
        (updates_src is not None and updates_src.privacy_stale)
        or (events_src is not None and events_src.privacy_stale))
    try:
        stored_parent_row = con.execute(
            "SELECT parent_session_key, role FROM sessions WHERE session_key=?",
            (r.session_key,)).fetchone()
    except sqlite3.DatabaseError:
        stored_parent_row = None
    try:
        stored_parent = stored_parent_row["parent_session_key"] \
            if stored_parent_row is not None else None
    except (KeyError, TypeError, IndexError):
        stored_parent = None
    try:
        stored_role = stored_parent_row["role"] \
            if stored_parent_row is not None else None
    except (KeyError, TypeError, IndexError):
        stored_role = None
    summary_fp = _summary_fingerprint(summary)
    chat_fp_current = _chat_fingerprint(session_dir)
    _ensure_grok_meta_table(con)
    (stored_summary_fp, stored_chat_fp, stored_seen, stored_synthetic,
     stored_chat_effort, stored_chat_reliable) = _stored_meta(con,
                                                              r.session_key)
    stored_marks = _chat_from_stored(stored_chat_fp, stored_seen,
                                     stored_synthetic, stored_chat_effort,
                                     stored_chat_reliable)

    # Preliminary child status from summary, persisted links and the shared
    # operation index only (no record parse and no sibling scan). The index
    # already holds the containing-tree spawns for source-scoped operations
    # (built once in sync()), so this finds a sibling-spawned parent even
    # when the child role is already subagent via summary or events.
    _precompute_child_status(con, r, None, None, effective_index)
    new_parent_via_index = (
        r.parent_key is not None and r.parent_key != stored_parent)
    new_role_via_evidence = (
        r.meta.get("role") == "subagent" and stored_role != "subagent")
    meta_changed = (stored_summary_fp != summary_fp
                    or stored_chat_fp != chat_fp_current)

    chat = None

    def _get_chat() -> dict:
        """Parsed chat marks, reusing persisted marks when bytes match.

        JSON-parses chat_history.jsonl only when its raw bytes changed (or
        no usable marks are stored). The fast path never calls this, so an
        unchanged second sync performs no chat JSON parsing at all.
        """
        nonlocal chat
        if chat is not None:
            return chat
        if not meta_changed and stored_marks is not None:
            chat = _chat_from_stored(stored_chat_fp, stored_seen,
                                     stored_synthetic, stored_chat_effort,
                                     stored_chat_reliable)
            if chat is not None:
                return chat
        chat = _read_chat(session_dir, r)
        return chat

    if known and not updates_new and not events_new and not privacy_stale \
            and not full:
        if not meta_changed and stored_marks is not None \
                and not new_parent_via_index and not new_role_via_evidence:
            # Fully unchanged: no parse of updates/events and no JSON parse
            # of chat. Per-sync I/O is summary.json, one raw chat read for
            # the fingerprint, and stat/tail checks. Persisted links, roles
            # and chat marks already cover classification, and the stored
            # session/meta rows already match, so no session, meta or
            # source writes happen here. The late parent/role branch below
            # keeps its convergence writes.
            # Final safety recheck immediately before returning: an append
            # (or same-size rewrite) racing the earlier checks falls
            # through to parsing below instead of being skipped.
            if updates_src is not None and updates_src.unchanged:
                updates_src.recheck_unchanged()
            if events_src is not None and events_src.unchanged:
                events_src.recheck_unchanged()
            updates_new = (full or updates_src is None
                           or not updates_src.unchanged)
            events_new = (full or events_src is None
                          or not events_src.unchanged)
            if not updates_new and not events_new:
                stats["unchanged"] = True
                stats["session_key"] = r.session_key
                return stats
        elif not meta_changed and stored_marks is not None \
                and (new_parent_via_index or new_role_via_evidence):
            # Late parent or role with no JSONL or chat-bytes growth:
            # converge without parsing updates/events and without JSON
            # parsing chat. The prebuilt operation index (or persisted link)
            # already supplied the parent; force existing submissions to
            # synthetic/empty in place and record the session link/role.
            # Recheck first: a raced JSONL growth must parse below instead
            # of converging without it.
            if updates_src is not None and updates_src.unchanged:
                updates_src.recheck_unchanged()
            if events_src is not None and events_src.unchanged:
                events_src.recheck_unchanged()
            updates_new = (full or updates_src is None
                           or not updates_src.unchanged)
            events_new = (full or events_src is None
                          or not events_src.unchanged)
            if not updates_new and not events_new:
                chat = stored_marks
                _force_child_synthetic(con, stats, r.session_key)
                late_fields = {"started_at": r.first_ts, "ended_at": r.last_ts,
                               **r.meta, **r.identity.fields(con)}
                if r.parent_key:
                    late_fields["parent_session_key"] = r.parent_key
                db.upsert_session(
                    con, r.session_key, HARNESS, r.native_sid,
                    updates_src.source_id if updates_src is not None
                    else (events_src.source_id if events_src is not None else None),
                    **late_fields)
                # Reclassify without records is covered by the force above
                # (no parse, no duplicates).
                _store_meta(con, r.session_key, summary_fp, chat_fp_current,
                            chat)
                con.commit()
                stats["unchanged"] = False
                stats["session_key"] = r.session_key
                return stats
            # A raced JSONL growth invalidated the no-parse shortcut: fall
            # through so the new records parse and reconcile below.
        # Else metadata changed (or first sync after the mark migration):
        # fall through so late chat/summary evidence reclassifies and
        # reconciles in place. Chat is resolved lazily below: reused
        # without parsing when its bytes match, parsed when they changed.
        chat = _get_chat()

    # Changed, new, or metadata-changed session: parse only what the sync
    # strictly requires, reusing one cached parse per loaded file.
    # - updates is required for new/full/stale/grown updates, or when
    #   summary/chat bytes changed (reclassify needs prompts, reconcile
    #   needs completions).
    # - events is additionally required when updates will replay (turn
    #   models decide response models), for new/full/stale/grown events,
    #   or when summary/chat bytes changed (reconcile needs turn models).
    # An unchanged updates file is therefore NOT reparsed merely because
    # events grew: that case loads events only, unless the events parse
    # itself proves a new subagent role, in which case updates is loaded
    # in a second stage strictly for the reclassification.
    if chat is None:
        chat = _get_chat()
    updates_will_replay = updates_src is not None and (updates_new or not known)
    load_updates = ((not known) or full or privacy_stale or updates_new
                    or meta_changed)
    load_events = ((not known) or full or privacy_stale or events_new
                   or meta_changed or updates_will_replay)
    updates_records = _load_jsonl_records(updates_path) \
        if (load_updates and os.path.isfile(updates_path)) else None
    events_records = _load_jsonl_records(events_path) \
        if (load_events and os.path.isfile(events_path)) else None
    if updates_records is None and events_records is not None and known \
            and not full and not privacy_stale and not meta_changed:
        # Events-only growth: the events parse may itself prove a new
        # subagent relationship. Only then is the unchanged updates file
        # loaded, strictly for reclassifying existing submissions.
        if stored_role != "subagent" and r.meta.get("role") != "subagent" \
                and _has_subagent_in_records(events_records):
            updates_records = _load_jsonl_records(updates_path) \
                if os.path.isfile(updates_path) else []
        elif _turn_models_from_records(
                _delta_records(events_records, events_src)):
            # Events-only model change: a new valid turn_started in the
            # changed delta can move existing responses off stale models.
            # Load the unchanged updates file strictly for response
            # reconciliation. Unrelated event growth (turn_ended only)
            # yields no turn model and keeps updates unparsed.
            updates_records = _load_jsonl_records(updates_path) \
                if os.path.isfile(updates_path) else []
    if updates_records is not None:
        try:
            _spawns = _extract_spawns_from_records(updates_records)
        except (AttributeError, TypeError):
            _spawns = {}
    else:
        # Updates file not loaded (unchanged, known, metadata-unchanged):
        # its spawns already reached persisted links on an earlier import.
        _spawns = {}
    for _child, _pkey in _spawns.items():
        try:
            effective_index.setdefault(_child, _pkey)
        except (AttributeError, TypeError):
            pass
        # Persist immediately from the same cached parse (no extra scan
        # or reparse): a newly appearing child that sorts before this
        # parent must converge even when this parent is unchanged and
        # would otherwise take a fast path without reaching _subagent_link.
        # Upsert only fills unknowns and the synthetic force only touches
        # non-synthetic rows, so repeats stay idempotent.
        _child_key = f"{HARNESS}:{_child}"
        try:
            db.upsert_session(con, _child_key, HARNESS, _child, None,
                              parent_session_key=_pkey, role="subagent")
        except (sqlite3.DatabaseError, ValueError):
            pass
        if _child_key != r.session_key:
            _force_child_synthetic(con, stats, _child_key)

    # Child status with the full evidence: summary, persisted, shared index,
    # this session's own dispatches and the cached turn_started relationship.
    _precompute_child_status(con, r, updates_records, events_records,
                             effective_index)

    # A parent link discovered while ingesting this sync's own updates means
    # the replay above computed submissions as a main session; reclassify to
    # correct those rows to non-genuine in place.
    parent_before = r.parent_key
    if updates_src is not None and (updates_new or not known) \
            and updates_records is not None:
        def _replay_error(ordinal: int, category: str, line: str = "") -> None:
            _record_error_once(updates_src, stats, ordinal, category, line)

        _prompts, _order, _completions = _collect_prompts_from_records(
            updates_records, record_error=_replay_error)
        _turn_models_cached = _turn_models_from_records(
            events_records if events_records is not None else [])
        _replay_updates(
            con, r, updates_src, chat, model_fallback, effort,
            updates_records, events_records,
            (_prompts, _order, _completions, _turn_models_cached))
        _advance_source_to_cached(updates_src, updates_path, updates_records)
        for ordinal, obj, line in _delta_records(
                updates_records, updates_src):
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
    if events_src is not None and (events_new or not known) \
            and events_records is not None:
        _advance_source_to_cached(events_src, events_path, events_records)
        for ordinal, obj, line in _delta_records(
                events_records, events_src):
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
    # import_errors) and never duplicates rows. Requires the updates records:
    # without them (events-only growth with no new role evidence) there is
    # nothing to reclassify, so the unchanged updates file stays unparsed.
    if updates_records is not None and (
            known or (r.parent_key is not None
                      and r.parent_key != parent_before)):
        _reclassify_existing(con, r, session_dir, chat, updates_records)
    replayed_updates = updates_src is not None and (updates_new or not known)
    if not replayed_updates and updates_records is not None \
            and events_records is not None:
        # Only events (or only summary/chat) changed: turn_started models,
        # summary model/effort or chat effort may be new. Update existing
        # response rows in place without duplicates or counter changes.
        # Both record sets are loaded here by construction (metadata change
        # loads both), so this never triggers a fresh parse itself.
        reconciled = _reconcile_response_metadata(
            con, r, session_dir, model_fallback,
            effort, chat.get("effort"), updates_records, events_records)
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
    _store_meta(con, r.session_key, summary_fp, chat_fp_current, chat)
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
    A type=user record proves human authorship only when its content yields
    valid text; missing or invalid content never adds the prompt to seen.
    The returned dict carries fp, a hash of the raw bytes distinguishing
    a changed chat file from an unchanged one without parsing updates or
    events.
    """
    chat = {"synthetic": set(), "seen": set(), "effort": None,
            "reliable": True, "fp": "missing"}
    path = os.path.join(session_dir, "chat_history.jsonl")
    try:
        with open(path, "rb") as bfh:
            raw_bytes = bfh.read()
    except OSError:
        chat["reliable"] = False
        return chat
    try:
        chat["fp"] = hashlib.sha256(raw_bytes).hexdigest()
    except (TypeError, ValueError):
        chat["fp"] = "unhashable"
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
                # Fail closed on unproven authorship: only valid chat
                # content counts as human-authorship proof. Missing or
                # invalid shapes yield no text and never mark the prompt
                # seen (synthetic marks need the same proof).
                if not text:
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


def _load_jsonl_records(path: str | None) -> list:
    """All complete-line records from path in a single open+parse.

    Returns [(ordinal, obj, raw)] where obj is None for malformed JSON.
    Ordinals count non-blank complete lines from the start, matching
    JsonlSource and _complete_lines; a trailing partial line is left for
    the next import. Callers reuse the list for parent indexing, prompt
    collection, replay, ingestion and reclassification instead of
    reopening and reparsing the file per path.
    """
    if not path or not os.path.isfile(path):
        return []
    try:
        lines = list(_complete_lines(path))
    except OSError:
        return []
    out: list = []
    for ordinal, raw in lines:
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
        out.append((ordinal, obj, raw))
    return out


def _extract_spawns_from_records(records: list) -> dict:
    """Child native sid -> parent session key from spawn records only."""
    out: dict = {}
    for _, obj, _ in records:
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
        out.setdefault(child, f"{HARNESS}:{parent}")
    return out


def _find_parent_in_records(records: list, native_sid: str) -> str | None:
    """Parent key from this file's own spawn records naming native_sid."""
    if not records or not native_sid:
        return None
    return _extract_spawns_from_records(records).get(native_sid)


def _has_subagent_in_records(records: list) -> bool:
    """Whether cached events prove a subagent via session_relationship."""
    for _, obj, _ in records:
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "turn_started":
            continue
        if obj.get("session_relationship") == "subagent":
            return True
    return False


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


def _turn_models_from_records(records: list) -> list:
    """Turn models from cached events records without reopening the file."""
    models = []
    for _, obj, _ in records:
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "turn_started" \
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


def _collect_prompts_from_records(records: list,
                                  record_error=None) -> tuple[dict, list, list]:
    """Same as _collect_prompts but over cached (ordinal, obj, raw) records.

    No file I/O or re-parsing: malformed (None) and non-dict records are
    skipped silently here exactly like the path version; the incremental
    ingest loop quarantines them once from the same cached list.
    """
    prompts: dict = {}
    order: list = []
    completions_ordered: list = []
    for ordinal, obj, raw in records:
        if obj is None or not isinstance(obj, dict):
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
                         chat: dict,
                         updates_records: list | None = None) -> int:
    """Update stored submissions when chat metadata arrives later.

    Requires the single cached parse of updates.jsonl (no duplicate
    import_errors) and applies evidence-aware kind/excerpt/turn updates in
    place without duplicates. Returns the number of rows changed. A missing
    record set means the updates file was deliberately not loaded (known
    and unchanged with no new role evidence) and there is nothing to
    reclassify, so this returns 0 without opening any file.
    """
    # The reader's updates source carries the ledger source id; building a
    # second JsonlSource here would repeat its privacy-stale import_errors
    # replacement and wipe the errors this sync just recorded.
    if updates_records is None:
        return 0
    prompts, order, completions_ordered = _collect_prompts_from_records(
        updates_records, record_error=None)
    completions_by_id = _deduplicate_completions(completions_ordered)
    before = con.total_changes
    for key in order:
        _upsert_submission(con, r, r.updates_src, chat, key, prompts[key],
                           completions_by_id)
    # Finish without advancing offsets when we only reclassified: do not
    # call finish() here because sources offsets belong to the incremental
    # loops. Just report whether anything changed.
    return con.total_changes - before


def _replay_updates(con, r: _Reader, src: JsonlSource, chat: dict,
                    model_fallback, effort,
                    updates_records: list | None = None,
                    events_records: list | None = None,
                    precomputed: tuple | None = None) -> None:
    """Rebuild prompts and per-prompt usage from the whole updates.jsonl.

    A growing file can extend an open prompt or finalize it on a later sync,
    but the schema keeps only the text hash and a bounded excerpt, so the
    full text is reconstructed here on every sync that saw new bytes. Every
    insert is under a natural key, so the replay never duplicates rows.
    Completions bind to prompts by native prompt id (deduplicated); position
    never shifts bindings. Unmatched prompts stay unbound; unmatched
    completions still store one response row by their own key.

    When cached records (or precomputed prompts/completions/turn models)
    are provided the single parse is reused instead of reopening the files.
    """

    def record_error(ordinal: int, category: str, line: str = "") -> None:
        # Categories arrive closed (missing_id, schema_error); the ingest
        # path maps anything else to the fallback and keeps only sorted
        # top-level key names from the raw line.
        _record_error_once(src, r.stats, ordinal, category, line)

    if precomputed is not None:
        prompts, order, completions_ordered, turn_models = precomputed
    elif updates_records is not None:
        prompts, order, completions_ordered = _collect_prompts_from_records(
            updates_records, record_error=record_error)
        if events_records is not None:
            turn_models = _turn_models_from_records(events_records)
        else:
            turn_models = _turn_models(
                os.path.join(os.path.dirname(src.path), "events.jsonl"))
    else:
        prompts, order, completions_ordered = _collect_prompts(
            src.path, record_error=record_error)
        turn_models = _turn_models(
            os.path.join(os.path.dirname(src.path), "events.jsonl"))
    completions_by_id = _deduplicate_completions(completions_ordered)
    # Prompt-ID to chunk model for response model fallback.
    pid_to_model: dict = {}
    for key in order:
        pid = prompts[key].get("prompt_id")
        if pid and pid not in pid_to_model and prompts[key].get("model"):
            pid_to_model[pid] = prompts[key]["model"]
    # All validated usage models per pid for strength comparison in the
    # response path: an existing model matching any of them is strong.
    all_usage_models: dict = {}
    for _o, _u, _t, _ob, _m, _rw in completions_ordered:
        _rp = _u.get("prompt_id") or _u.get("promptId")
        _pid = _rp if isinstance(_rp, str) \
            and _safe_token(_rp) is not None else None
        if _pid is None:
            continue
        _um = _validated_usage_model(_u)
        if _um is not None:
            all_usage_models.setdefault(_pid, set()).add(_um)
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
                        _validated_usage_model(update), model_fallback,
                        chunk_model, effort, chat.get("effort"),
                        turn_models, raw,
                        all_usage_models.get(pid, set()))


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
                    summary_effort, chat_effort, turn_models,
                    raw_line: str = "", all_usage_models=None) -> None:
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
        # records for other (or the same) prompt ids still import. Usage
        # is validated before its modelUsage metadata is accepted.
        _record_error_once(src, r.stats, ordinal, exc.category, raw_line)
        return
    usage_model = _valid_model(usage_model)
    summary_model = _valid_model(summary_model)
    chunk_model = _valid_model(chunk_model)
    valid_summary_effort = _valid_model(summary_effort)
    valid_chat_effort = _valid_model(chat_effort)
    valid_effort = valid_summary_effort or valid_chat_effort
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
        # Persist evidence provenance for later stronger-wins comparison.
        _turn_m = None
        for _started, _name in safe_turns:
            if ts is not None and _started <= ts:
                _turn_m = _name
        if all_usage_models is None:
            _uset = {usage_model} if usage_model is not None else set()
        else:
            _uset = set(all_usage_models)
            if usage_model is not None:
                _uset.add(usage_model)
        _write_provenance(
            con, response_id,
            _model_ranks(model, _turn_m, _uset, summary_model, chunk_model),
            (1 if valid_effort == valid_summary_effort else 0)
            if valid_effort is not None else -1)
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
    # validated values, without duplicates. Stronger evidence wins; weaker
    # never downgrades a recorded value; missing never clears a known one.
    # Only a new valid differing value of equal or stronger rank writes.
    turn_m = None
    for started, name in safe_turns:
        if ts is not None and started <= ts:
            turn_m = name
    if all_usage_models is None:
        usage_set = {usage_model} if usage_model is not None else set()
    else:
        usage_set = set(all_usage_models)
        if usage_model is not None:
            usage_set.add(usage_model)
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
    stored_model_rank, stored_effort_rank = _read_provenance(con, response_id)
    new_model_rank = _model_ranks(model, turn_m, usage_set,
                                  summary_model, chunk_model)
    new_erank = (1 if valid_effort == valid_summary_effort else 0) \
        if valid_effort is not None else -1
    if model is not None and old_model != model:
        if stored_model_rank is not None:
            if old_model is None or new_model_rank >= stored_model_rank:
                meta_sets.append("model=?")
                meta_args.append(model)
        elif _has_unproven_model_usage(update):
            pass
        elif old_model is None:
            meta_sets.append("model=?")
            meta_args.append(model)
        elif new_model_rank >= 2:
            # No provenance: fail safe by keeping the existing value
            # unless the rewrite proves a turn (3) or usage (2) model.
            # Summary/chunk fallback never overwrites.
            meta_sets.append("model=?")
            meta_args.append(model)
        # Else preserve the existing model against fallback evidence.
    if valid_effort is not None and old_effort != valid_effort:
        if old_effort is None:
            meta_sets.append("effort=?")
            meta_args.append(valid_effort)
        elif stored_effort_rank is not None:
            if new_erank >= stored_effort_rank:
                meta_sets.append("effort=?")
                meta_args.append(valid_effort)
        else:
            # No provenance: preserve the existing effort. Fallback never
            # overwrites a provenance-less value.
            pass
    if meta_sets:
        meta_args.append(response_id)
        con.execute(
            f"UPDATE responses SET {', '.join(meta_sets)} WHERE response_id=?",
            meta_args)
        # Converge stored ranks only for fields that actually changed.
        _model_rank_to_store = None
        _effort_rank_to_store = None
        if "model=?" in meta_sets:
            _model_rank_to_store = new_model_rank
        if "effort=?" in meta_sets:
            _effort_rank_to_store = new_erank
        _write_provenance(con, response_id, _model_rank_to_store,
                          _effort_rank_to_store)
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
    sync_ctx = getattr(r, "sync_ctx", None)
    if sync_ctx is not None:
        try:
            sync_ctx.parent_index.setdefault(child, f"{HARNESS}:{parent}")
        except (AttributeError, TypeError):
            pass
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
                              parent_session_key=f"{HARNESS}:{parent}",
                              role="subagent")
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
    elif kind in _MCP_IGNORED_SHAPES:
        # Benign MCP lifecycle/config records: ignored entirely when the
        # top-level key set matches exactly one known shape. No event, no
        # error, and no MCP value (server names, targets, transports,
        # errors, tools) reaches any ledger column. Any other shape,
        # including an extra or missing key, stays unknown_record.
        try:
            shape = frozenset(obj.keys())
        except AttributeError:
            raise _AdapterError("unknown_record")
        if shape not in _MCP_IGNORED_SHAPES[kind]:
            raise _AdapterError("unknown_record")
        return
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
