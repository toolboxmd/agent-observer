"""Claude Code native adapter: session transcripts into the ledger.

Reads `~/.claude/projects/<project>/<session>.jsonl` and subagent transcripts
under `<session>/subagents/agent-<id>.jsonl`. A subagent is its own session
linked to its parent; its API calls are distinct messages, so parent and
subagent usage never overlap.

Counter rule: Claude writes one record per streamed content block and repeats
the same usage on each, so a response is counted once per message id. Input
excludes cache reads and cache writes; output includes thinking, so
total_tokens = input + cache_creation + cache_read + output.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict, deque

from .. import db, intervention_evidence, native_response, placement, privacy
from ..identity import SessionIdentity, skill_from_path
from ..ingest import (JsonlSource, MissingNativeId, fingerprint, insert_event,
                      iso_ts, text_hash)
from .discovery import record_paths

HARNESS = "claude"
SEMANTICS = "claude:input_excludes_cache,output_includes_thinking"
DEFAULT_ROOT = os.path.expanduser("~/.claude/projects")
SCAFFOLD_PREFIXES = ("<command-name>", "<local-command", "<system-reminder>",
                     "Caveat:", "<task-notification>", "<bash-",
                     "<hook", "<stop-hook")
INTERRUPT_PREFIX = "[Request interrupted"
SKILL_BASE_PREFIX = "Base directory for this skill:"
READ_TOOLS = {"Read": "file_path", "NotebookRead": "notebook_path"}
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path"}

CAPABILITIES = [
    ("model_usage", True, "assistant message usage once per message id; thinking inside output; cache read and creation separate from input"),
    ("tool_calls", True, "tool_use blocks with name, input fingerprint and target path or command"),
    ("tool_results", True, "tool_result blocks joined on tool_use_id; is_error and permission denials kept"),
    ("read_evidence", True, "Read tool results with resolved path and line range"),
    ("skill_file_reads", True, "reads under an installed Skill directory"),
    ("skill_invocation", True, "Skill tool calls name the skill"),
    ("compaction", True, "system compact_boundary records with trigger and pre-compaction tokens"),
    ("lifecycle_task", True, "turn_duration records; interrupts as user messages of kind interrupt"),
    ("human_input", True, "typed prompts, mid-turn queued prompts, slash commands and interrupts told apart"),
    ("instruction_identity", True, "AgentsMD direction block from the SessionStart hook; versioned plugin paths read"),
    ("subagents", True, "subagent transcripts as child sessions of their parent"),
]

# Benign native metadata records: recognized as known and ignored. Their
# contents are never stored; only the session/timing fields read before
# dispatch (identifiers, never free text) are kept. Anything not listed
# here still raises _UnsupportedSchema, so arbitrary future record types
# stay quarantined instead of being silently dropped.
#
# last-prompt stays here deliberately: it is known duplicate state that
# repeats the latest human prompt already carried by user records, which
# are the canonical prompt evidence. It stores nothing.
#
# cost-state is NOT ignored: it carries per-model token counters whose
# reconciliation with assistant usage is an open follow-up, so it stays
# quarantined as unsupported_schema (nothing stored) instead of being
# silently dropped.
IGNORED_METADATA_TYPES = frozenset({
    "agent-name",
    "api-request",
    "api-request-blob",
    "api-request-shape",
    "atis-latch",
    "bridge-session",
    "custom-title",
    "file-history-delta",
    "file-history-snapshot",
    "last-prompt",
    "mode",
    "permission-mode",
    "pr-link",
})


class _MalformedUsage(ValueError):
    """A usage bucket that is not NULL or a real integer counter."""


class _UsageConflict(ValueError):
    """An exact-ID repeat whose counters differ from the stored final row."""


class _UnsupportedSchema(ValueError):
    """A record type the adapter does not support."""


_CLAUDE_COUNTER_KEYS = ("input_tokens", "cache_creation_input_tokens",
                        "cache_read_input_tokens", "output_tokens")


def _valid_counter(value) -> bool:
    """A known counter is a nonnegative SQLite INTEGER."""
    if value is None:
        return True
    return type(value) is int and 0 <= value <= (1 << 63) - 1


_FINALITY_TABLE = "claude_response_finality"
_NATIVE_KINDS_TABLE = "claude_native_kinds"
_IMPORT_VERSION = 17  # Recheck every priced field and the source of each legacy trace event.
_FINAL_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
                 "reasoning_output_tokens", "total_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens", "model")


def _ensure_native_kinds(con) -> None:
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {_NATIVE_KINDS_TABLE}("
        " native_id TEXT PRIMARY KEY REFERENCES user_messages(native_id) ON DELETE CASCADE,"
        " kind TEXT NOT NULL CHECK(kind IN"
        " ('genuine','synthetic','unknown','interrupt','command','scaffolding')),"
        " origin_allowed INTEGER NOT NULL CHECK(origin_allowed IN (0,1)))")
    con.execute("CREATE INDEX IF NOT EXISTS idx_claude_message_alias"
                " ON user_messages(alias_id) WHERE alias_id IS NOT NULL")


def native_kind(con, native_id: str) -> str | None:
    """Transcript classification before any mutable Chromeria origin evidence."""
    try:
        row = con.execute(f"SELECT kind FROM {_NATIVE_KINDS_TABLE} WHERE native_id=?",
                          (native_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None  # A ledger that has not imported this native fact yet.
        raise
    return row[0] if row else None


def prompt_origin_allowed(con, native_id: str) -> bool:
    """Native sidechain/scaffolding restrictions also govern mirror backfill."""
    try:
        row = con.execute(f"SELECT origin_allowed FROM {_NATIVE_KINDS_TABLE} WHERE native_id=?",
                          (native_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return False
        raise
    return bool(row and row[0])


def restore_native_excerpts(con, native_ids: list[str]) -> None:
    """Recover permitted excerpts from affected native sources, never a text cache."""
    by_path = {}
    for native_id in native_ids:
        row = con.execute(
            "SELECT u.native_id,u.alias_id,s.path,s.session_id,s.thread_id,se.entrypoint"
            " FROM user_messages u JOIN sources s ON s.id=u.source_id"
            " LEFT JOIN sessions se ON se.session_key=u.session_key"
            " WHERE u.native_id=? AND u.kind='genuine' AND u.text_excerpt=''",
            (native_id,)).fetchone()
        if row:
            by_path.setdefault(row["path"], {})[prompt_native_id(row)] = row
    for path, wanted in by_path.items():
        try:
            with open(path, "rb") as fh:
                ordinal = 0
                for raw in fh:
                    if not raw.endswith(b"\n"):
                        break
                    if not raw.strip():
                        continue
                    position = ordinal
                    ordinal += 1
                    try:
                        obj = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(obj, dict) or obj.get("type") != "user":
                        continue
                    native_id = f"{HARNESS}:{obj.get('uuid') or f'ordinal:{position}'}"
                    row = wanted.get(native_id)
                    if not row or (obj.get("sessionId") or row["session_id"]) != row["session_id"]:
                        continue
                    message = obj.get("message")
                    try:
                        text = _text(message.get("content")) if isinstance(message, dict) else ""
                    except TypeError:
                        continue  # The same malformed text is quarantined by native import.
                    native_kind = _native_user_kind(obj, text, agent_id=row["thread_id"],
                                                    entrypoint=row["entrypoint"])
                    kind = _user_kind(con, row["session_id"], row["thread_id"], obj, native_kind)
                    excerpt = privacy.submission_excerpt(
                        text, is_genuine=kind == "genuine",
                        is_main_session=row["thread_id"] is None and row["session_id"] is not None)
                    con.execute("UPDATE user_messages SET text_excerpt=? WHERE native_id=?",
                                (excerpt, row["native_id"]))
        except OSError:
            # Classification can be restored from metadata even if native
            # text is unavailable. No other source or text store is consulted.
            continue


def _ensure_finality_table(con) -> None:
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {_FINALITY_TABLE}"
        "(response_id TEXT PRIMARY KEY, finalized_at REAL)")


def _is_finalized(con, rid: str) -> bool:
    try:
        row = con.execute(
            f"SELECT 1 FROM {_FINALITY_TABLE} WHERE response_id=?",
            (rid,)).fetchone()
    except sqlite3.DatabaseError:
        return False
    return row is not None


def _mark_finalized(con, rid: str) -> None:
    import time as _time
    con.execute(
        f"INSERT OR IGNORE INTO {_FINALITY_TABLE}"
        "(response_id, finalized_at)"
        " VALUES(?, ?)", (rid, _time.time()))


def discover(root: str | None = None, *, strict: bool = False) -> list[str]:
    root = root or DEFAULT_ROOT
    main = record_paths(root, "*", "*.jsonl", strict=strict)
    subs = record_paths(root, "*", "*", "subagents", "agent-*.jsonl", strict=strict)
    return sorted(main + subs)


def sync(con: sqlite3.Connection, root: str | None = None, full: bool = False,
         source: str | None = None) -> dict:
    paths = [source] if source else discover(root)
    totals = {"harness": HARNESS, "sources": 0, "unchanged": 0,
              "responses_inserted": 0, "events_inserted": 0,
              "user_messages_inserted": 0, "malformed": 0, "failed": []}
    for path in paths:
        try:
            stats = import_claude_file(con, path, full=full)
        except (OSError, sqlite3.DatabaseError, UnicodeDecodeError) as exc:
            totals["failed"].append({"path": path, "error": str(exc)})
            continue
        totals["sources"] += 1
        totals["unchanged"] += 1 if stats.get("unchanged") else 0
        for key in ("responses_inserted", "events_inserted",
                    "user_messages_inserted", "malformed"):
            totals[key] += stats.get(key, 0)
    return totals


def _strings(obj):
    """Every string value inside a nested record."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _strings(value)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content
                       if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _target(name: str, tool_input: dict):
    """A string target only: paths, commands and validated skill names.

    Non-string native values never stringify into the ledger; the caller
    then stores no target. Search patterns and URLs are free text and are
    not targets. A Skill tool input carries only the skill identity: the
    skill or command value is validated as an identifier before any
    path or command handling, so a free-text Skill title in either
    field fails closed to no target on every event (tool_call,
    skill_invoke and tool_result alike).
    """
    if not isinstance(tool_input, dict):
        return None
    if name == "Skill":
        raw_skill = tool_input.get("skill") or tool_input.get("command")
        return privacy.filter_target(raw_skill, family="skill_invoke")
    for key in ("file_path", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    command = tool_input.get("command")
    if isinstance(command, str) and command:
        return command
    skill = tool_input.get("skill")
    if isinstance(skill, str) and skill:
        return privacy.filter_target(skill, family="skill_invoke")
    return None


class _Reader:
    def __init__(self, con, src: JsonlSource, stats: dict):
        self.con = con
        self.src = src
        self.stats = stats
        self.first_new_ordinal = src.first_new_ordinal()
        self.checkout_events = []
        self.native_session = src.row["session_id"]
        self.agent_id = src.row["thread_id"]
        self.identity = SessionIdentity()
        self.meta: dict = {}
        self.first_ts = None
        self.last_ts = None
        self.turn = None
        self.pending: dict = {}
        self.message_events = []
        # Message ids whose native final block (stop_reason) has been seen,
        # either earlier in this import or in the already-imported file
        # prefix. A row is updated while its message streams; once final,
        # every later same-ID repeat is validation only.
        self.finalized: set = set()
        self.native_user_ids: set[str] = set()

    @property
    def session_key(self) -> str:
        if not self.native_session:
            return f"{HARNESS}:file:{os.path.basename(self.src.path)}"
        if self.agent_id:
            return f"{HARNESS}:{self.native_session}:agent:{self.agent_id}"
        return f"{HARNESS}:{self.native_session}"

    def event(self, family, native_id, ordinal, ts, **kw):
        insert_event(self.con, self.stats, source_id=self.src.source_id,
                     session_key=self.session_key, family=family,
                     native_id=native_id, ordinal=ordinal, ts=ts,
                     turn_id=self.turn, update=self.src.privacy_stale,
                     checkout_events=self.checkout_events
                     if self.first_new_ordinal is not None
                     and ordinal >= self.first_new_ordinal else None, **kw)
        self.con.execute("INSERT OR IGNORE INTO claude_event_observations(source_id,event_id,turn_id) "
                         "SELECT ?,id,? FROM events WHERE session_key=? AND family=? AND native_id=?",
                         (self.src.source_id, self.turn or "", self.session_key, family, native_id))
        if family in {"tool_call", "assistant_message"}:
            self.message_events.append((family, native_id))


def import_claude_file(con: sqlite3.Connection, path: str,
                       full: bool = False) -> dict:
    intervention_evidence.ensure_table(con)
    stats = {"lines": 0, "responses_inserted": 0, "responses_duplicate": 0,
             "responses_updated": 0,
             "user_messages_inserted": 0, "events_inserted": 0,
             "events_duplicate": 0, "compactions": 0, "malformed": 0}
    src = JsonlSource(con, HARNESS, path, full=full)
    if (src.row["claude_usage_version"] != _IMPORT_VERSION
            or src.row["claude_usage_offset"] != src.row["read_offset"]):
        src.start_offset = src.end_offset = 0
        src.unchanged = False
    if src.unchanged and src.recheck_unchanged():
        # Unchanged fast path: same size, mtime, inode and tail bytes,
        # same privacy version, not full. The recheck re-stats immediately
        # before returning so an append (or same-size rewrite) racing the
        # first check falls through to the import path below with the
        # corrected offset instead of skipping new bytes.
        # Skip the global finality scan, the turn-resume query, JSON
        # parsing and session writes. The stored fingerprint is the sha256
        # contract; stale/full imports never take this path because
        # JsonlSource leaves unchanged False for them.
        try:
            stored_sha = src.row["sha256"]
            stored_ordinal = src.row["ordinal_max"]
            native_session = src.row["session_id"]
            agent_id = src.row["thread_id"]
        except (KeyError, TypeError, IndexError):
            stored_sha = ""
            stored_ordinal = -1
            native_session = None
            agent_id = None
        if not native_session:
            session_key = f"{HARNESS}:file:{os.path.basename(path)}"
        elif agent_id:
            session_key = f"{HARNESS}:{native_session}:agent:{agent_id}"
        else:
            session_key = f"{HARNESS}:{native_session}"
        stats.update({
            "source_id": src.source_id,
            "sha256": stored_sha,
            "ordinal_max": stored_ordinal,
            "incremental": src.incremental,
            "unchanged": True,
            "session_key": session_key,
        })
        return stats
    if src.incremental and db._has_table(con, "claude_unreadable_sources") and con.execute(
            "SELECT 1 FROM claude_unreadable_sources WHERE source_id=? LIMIT 1", (src.source_id,)).fetchone():
        # Re-evaluate the previously unreadable bytes and their old witnesses
        # together, including when the writer merely finishes its last line.
        src.start_offset = src.end_offset = 0
    r = _Reader(con, src, stats)
    _ensure_finality_table(con)
    _ensure_native_kinds(con)
    _seed_queue_identity(r)
    _prepare_ancestry(r)
    native_response.prepare_events(con, src.source_id, replace=not src.incremental)
    # Durable per-response finality is authoritative for accepted final
    # rows. It covers copied sources, full reimports and incremental
    # appends. The file prefix is never authority on its own, so a malformed
    # final prefix can never finalize a response before any row exists.
    try:
        for row in con.execute(
                f"SELECT response_id FROM {_FINALITY_TABLE}"):
            rid = row["response_id"] if "response_id" in row.keys() else row[0]
            if isinstance(rid, str) and rid.startswith(f"{HARNESS}:"):
                r.finalized.add(rid[len(HARNESS) + 1:])
    except sqlite3.DatabaseError:
        pass
    if src.incremental and src.row["session_id"]:
        # Resume the turn the previous import ended in.
        row = con.execute(
            "SELECT turn_id FROM user_messages WHERE session_key=? AND kind='genuine'"
            " ORDER BY ts DESC LIMIT 1", (r.session_key,)).fetchone()
        r.turn = row["turn_id"] if row else None
    unreadable = False
    for ordinal, obj, line in src.records():
        stats["lines"] += 1
        if obj is None or not isinstance(obj, dict):
            unreadable = True
            stats["malformed"] += 1
            src.error(ordinal,
                      "malformed_json" if obj is None else "unknown_record",
                      line)
            continue
        if obj.get("type") == "assistant":
            message = obj.get("message")
            if (not isinstance(message, dict) or (message.get("model") != "<synthetic>" and (
                    not native_response.identifier(message.get("id")) or message.get("role", "assistant") != "assistant"))):
                unreadable = True
        try:
            r.message_events.clear()
            r.final_location = None
            _ingest(r, obj, ordinal)
            _record_ancestry(r, obj)
            if obj.get("type") == "queue-operation":
                con.execute("DELETE FROM import_errors WHERE harness=? AND source_path=?"
                            " AND ordinal_num=? AND error='unsupported_schema'",
                            (HARNESS, path, ordinal))
        except _UsageConflict:
            _record_ancestry(r, obj, rejected="usage_conflict")
            stats["malformed"] += 1
            src.error(ordinal, "usage_conflict", line)
        except _MalformedUsage:
            _record_ancestry(r, obj, rejected="malformed_usage")
            stats["malformed"] += 1
            src.error(ordinal, "malformed_usage", line)
        except MissingNativeId:
            stats["malformed"] += 1
            src.error(ordinal, "missing_id", line)
        except _UnsupportedSchema:
            stats["malformed"] += 1
            src.error(ordinal, "unsupported_schema", line)
        except (KeyError, TypeError, ValueError, AttributeError):
            if obj.get("type") == "assistant":
                _record_ancestry(r, obj, rejected="schema_error")
            stats["malformed"] += 1
            src.error(ordinal, "schema_error", line)
    unreadable = unreadable or src.incomplete_record or src.end_offset < src.size
    if unreadable:
        con.execute("INSERT OR IGNORE INTO claude_unreadable_sources VALUES(?,?)", (r.session_key, src.source_id))
        if not src.incremental:
            con.execute("INSERT OR IGNORE INTO claude_final_observations SELECT * FROM temp.claude_prior_finals")
            con.execute("""INSERT OR IGNORE INTO claude_rejected_measurements
                SELECT p.session_key,p.source_id,p.uuid,p.parent,p.response_id,'unreadable_record'
                FROM claude_prior_edges p WHERE NOT EXISTS (
                    SELECT 1 FROM claude_response_ancestry a WHERE a.source_id=p.source_id
                    AND a.session_key=p.session_key AND a.uuid=p.uuid AND a.parent=p.parent
                    AND a.response_id=p.response_id)""")
    if not src.incremental:
        if not unreadable:
            # A complete snapshot may remove a message altogether. Any
            # surviving copy of that message, accepted or quarantined, keeps
            # this source's obligation to supply its previously declared final.
            retired = """ WHERE source_id=?
                AND (session_key,response_id) NOT IN (
                    SELECT session_key,response_id FROM claude_response_ancestry WHERE source_id=?
                    UNION SELECT session_key,response_id FROM claude_rejected_measurements WHERE source_id=?
                )"""
            ids = (src.source_id,) * 3
            con.execute("INSERT OR IGNORE INTO claude_withdrawn_finals SELECT session_key,response_id "
                        "FROM claude_expected_finals" + retired, ids)
            con.execute("DELETE FROM claude_expected_finals" + retired, ids)
        # A replacement can revise a field by measuring it again. Omitting
        # the field cannot settle a disagreement, even across partial finals.
        con.execute("""INSERT OR IGNORE INTO claude_retained_final_fields
            SELECT p.session_key,p.response_id,p.source_id,j.key,j.value
            FROM claude_prior_finals p JOIN claude_expected_finals e USING(session_key,response_id,source_id),
            json_each(p.payload) j WHERE j.value IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM claude_final_observations o WHERE o.session_key=p.session_key
                AND o.response_id=p.response_id AND o.source_id=p.source_id
                AND json_extract(o.payload,'$.'||j.key) IS NOT NULL)""")
        con.execute("DROP TABLE temp.claude_prior_edges")
        con.execute("DROP TABLE temp.claude_prior_finals")
    # Retained values supply negative evidence only. A readable removal of
    # the response, or a new measurement for this field, retires that value.
    con.execute("""DELETE FROM claude_retained_final_fields AS p WHERE p.source_id=? AND (
        NOT EXISTS (SELECT 1 FROM claude_expected_finals e WHERE e.session_key=p.session_key
        AND e.response_id=p.response_id AND e.source_id=p.source_id) OR EXISTS (
        SELECT 1 FROM claude_final_observations o WHERE o.session_key=p.session_key
        AND o.response_id=p.response_id AND o.source_id=p.source_id
        AND json_extract(o.payload,'$.'||p.field) IS NOT NULL))""", (src.source_id,))
    _reproject_withdrawn_finals(r)
    con.executemany("INSERT OR IGNORE INTO claude_ancestry_sources VALUES(?,?)",
                    ((key, src.source_id) for key in r.linked_sessions))
    if r.native_session:
        fields = {"started_at": r.first_ts, "ended_at": r.last_ts, **r.meta,
                  **r.identity.fields(con)}
        if r.agent_id:
            fields["parent_session_key"] = f"{HARNESS}:{r.native_session}"
            fields["role"] = "subagent"
        db.upsert_session(con, r.session_key, HARNESS,
                          r.session_key.split(":", 1)[1], src.source_id,
                          **fields)
    stats.update(src.finish(session_id=r.native_session, thread_id=r.agent_id))
    con.execute("UPDATE sources SET claude_usage_version=?,claude_usage_offset=? WHERE id=?",
                (_IMPORT_VERSION, src.end_offset, src.source_id))
    stats["session_key"] = r.session_key
    con.commit()
    placement.observe_new_checkouts(con, r.checkout_events)
    return stats


def _ingest(r: _Reader, obj: dict, ordinal: int) -> None:
    if obj.get("sessionId") and not r.native_session:
        r.native_session = obj["sessionId"]
    if obj.get("isSidechain") and obj.get("agentId") and not r.agent_id:
        r.agent_id = obj["agentId"]
    ts = iso_ts(obj.get("timestamp"))
    if ts is not None:
        r.first_ts = ts if r.first_ts is None else min(r.first_ts, ts)
        r.last_ts = ts if r.last_ts is None else max(r.last_ts, ts)
    for key, field in (("cwd", "project_dir"), ("gitBranch", "git_branch"),
                       ("version", "client_version"), ("entrypoint", "entrypoint")):
        if obj.get(key) and field not in r.meta:
            r.meta[field] = obj[key]
    kind = obj.get("type")
    if kind == "assistant":
        _assistant(r, obj, ordinal, ts)
    elif kind == "user":
        _user(r, obj, ordinal, ts)
    elif kind == "attachment":
        _attachment(r, obj, ordinal, ts)
    elif kind == "queue-operation":
        _queue_operation(r, obj, ordinal, ts)
    elif kind == "system":
        _system(r, obj, ordinal, ts)
    elif kind == "ai-title":
        # Rule 7: native free-text titles are discarded, never stored.
        return
    elif kind in IGNORED_METADATA_TYPES:
        # Known benign metadata: recognized, never stored, never an error.
        return
    else:
        raise _UnsupportedSchema(f"unsupported record type: {kind!r}")


def _cache_consistent(total, five, hour):
    if total is None:
        return five is None or hour is None or _valid_counter(five + hour)
    return ((five is None or five <= total) and (hour is None or hour <= total)
            and (five is None or hour is None or five + hour == total))


def _usage_values(usage) -> dict:
    """Native usage buckets with unknown preserved as NULL.

    Only NULL or real integer counters are accepted; booleans, strings,
    lists and other shapes are malformed and quarantined before any SQL.
    Only recognized numeric counters are validated; string metadata such as
    service_tier is ignored. A usage object with no recognized numeric
    counter is malformed and never creates a response row or finality.
    total_tokens is the harness's own total and is only known when every
    bucket is known; a partial sum from some buckets is never constructed."""
    if not isinstance(usage, dict):
        raise _MalformedUsage("malformed usage")
    details_raw = usage.get("output_tokens_details")
    if details_raw is None:
        details: dict = {}
    elif not isinstance(details_raw, dict):
        raise _MalformedUsage("malformed usage details")
    else:
        details = details_raw
    for key in _CLAUDE_COUNTER_KEYS:
        if not _valid_counter(usage.get(key)):
            raise _MalformedUsage("malformed usage counter")
    thinking = details.get("thinking_tokens")
    if not _valid_counter(thinking):
        raise _MalformedUsage("malformed usage counter")
    # service_tier and any other unrecognized metadata are ignored: they
    # never validate or invalidate a usage block.
    if all(usage.get(k) is None for k in _CLAUDE_COUNTER_KEYS) and thinking is None:
        raise _MalformedUsage("malformed usage")
    ttl = usage.get("cache_creation")
    if ttl is not None and not isinstance(ttl, dict):
        raise _MalformedUsage("malformed cache creation details")
    ttl = ttl or {}
    five = ttl.get("ephemeral_5m_input_tokens")
    hour = ttl.get("ephemeral_1h_input_tokens")
    if not _valid_counter(five) or not _valid_counter(hour):
        raise _MalformedUsage("malformed cache creation counter")
    total_write = usage.get("cache_creation_input_tokens")
    if not _cache_consistent(total_write, five, hour):
        raise _MalformedUsage("inconsistent cache creation counters")
    values = [usage.get(k) for k in _CLAUDE_COUNTER_KEYS]
    total = sum(values) if all(type(v) is int for v in values) else None
    if not _valid_counter(total):
        raise _MalformedUsage("invalid total token count")
    return {
        "input_tokens": usage.get("input_tokens"),
        "cached_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_write_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_write_5m_tokens": five,
        "cache_write_1h_tokens": hour,
        "output_tokens": usage.get("output_tokens"),
        "reasoning_output_tokens": details.get("thinking_tokens"),
        "total_tokens": total,
    }


def _stored_counters(r: _Reader, rid: str) -> dict:
    row = r.con.execute(
        "SELECT input_tokens, cached_input_tokens, cache_write_input_tokens,"
        " output_tokens, reasoning_output_tokens, total_tokens,"
        " cache_write_5m_tokens, cache_write_1h_tokens"
        " FROM responses WHERE response_id=?", (rid,)).fetchone()
    return dict(row) if row is not None else {}


def _agreeing_fields(rows, fields):
    agreed = {}
    witnesses = defaultdict(set)
    for row in rows:
        for field, value in json.loads(row["payload"]).items():
            if value is None or field not in fields:
                continue
            if field in agreed and agreed[field] != value:
                return None, witnesses
            agreed[field] = value
            witnesses[field].add(row["source_id"])
    return agreed, witnesses


def _corroborated_final(r, rid, seen):
    """A changed counter needs matching measurements from its original source and a copy."""
    rows = r.con.execute(
        "SELECT o.source_id,o.payload,r.source_id AS original,r.model FROM claude_final_observations o "
        "JOIN responses r USING(session_key,response_id) WHERE o.session_key=? AND o.response_id=?",
        (r.session_key, rid)).fetchall()
    sources = {row["source_id"] for row in rows}
    if len(sources) < 2 or rows[0]["original"] not in sources:
        return None
    agreed, witnesses = _agreeing_fields(rows, {*seen, "model"})
    # A single native final must actually have measured the full total.
    # Its constituent counters, when revised, must also be corroborated.
    if not agreed or "total_tokens" not in agreed:
        return None
    for field, old in dict(seen, model=rows[0]["model"]).items():
        if field != "total_tokens" and old is not None and field in agreed and old != agreed[field]:
            if len(witnesses[field]) < 2 or rows[0]["original"] not in witnesses[field]:
                return None
    return agreed


def _reproject_withdrawn_finals(r):
    """A readable removal retires every counter and locator supplied only by that source."""
    con = r.con
    for key, rid in con.execute("SELECT session_key,response_id FROM claude_withdrawn_finals "
                                "WHERE session_key=?", (r.session_key,)).fetchall():
        copies = con.execute("SELECT o.source_id,o.payload,u.source_id AS unreadable FROM claude_final_observations o "
                             "LEFT JOIN claude_unreadable_sources u USING(session_key,source_id) "
                             "WHERE o.session_key=? AND o.response_id=?", (key, rid)).fetchall()
        blocks = con.execute("SELECT b.* FROM claude_final_blocks b JOIN sources s ON s.id=b.source_id "
                             "WHERE b.session_key=? AND b.response_id=? ORDER BY s.path,b.ordinal_num",
                             (key, rid)).fetchall()
        if (not copies or any(c["unreadable"] for c in copies)
                or {c["source_id"] for c in copies} != {b["source_id"] for b in blocks}):
            continue
        fields = (*_stored_counters(r, rid), "model")
        agreed, witnessed = _agreeing_fields(copies, fields)
        if not agreed or not _cache_consistent(agreed.get("cache_write_input_tokens"),
                                               agreed.get("cache_write_5m_tokens"), agreed.get("cache_write_1h_tokens")):
            continue
        if all(b["link_status"] == "absent" for b in blocks) and len({b["turn_id"] for b in blocks}) > 1:
            continue
        # Prefer a native block from a copy that measured the complete total.
        block = next((b for b in blocks if b["source_id"] in witnessed["total_tokens"]), blocks[0])
        con.execute("UPDATE responses SET " + ",".join(f"{field}=?" for field in fields) +
                    ",source_id=?,turn_id=?,ordinal_num=?,ts=? WHERE session_key=? AND response_id=?",
                    (*(agreed.get(field) for field in fields), block["source_id"], block["turn_id"],
                     block["ordinal_num"], block["ts"], key, rid))
        # Only a block with validated final usage can regain its recorded
        # ancestry. Other rejected blocks and unusable parents stay disputed.
        for b in blocks:
            _save_edge(con, key, b["uuid"], b["parent"], "", rid, b["source_id"], b["link_status"])
        con.execute("DELETE FROM claude_withdrawn_finals WHERE session_key=? AND response_id=?", (key, rid))


def _unsupported_projections(con, scope, args):
    """Older projections may contain individual fields supplied only by a removed source."""
    gaps = []
    retained = db._has_table(con, "claude_retained_final_fields")
    for field in _FINAL_FIELDS:
        gap = (f"r.{field} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM claude_final_observations o "
               "WHERE o.session_key=r.session_key AND o.response_id=r.response_id "
               f"AND json_extract(o.payload,'$.{field}') IS NOT NULL)")
        if retained:
            gap += (" AND NOT EXISTS (SELECT 1 FROM claude_retained_final_fields f WHERE f.session_key=r.session_key "
                    f"AND f.response_id=r.response_id AND f.field='{field}' AND f.value=r.{field})")
        gaps.append("(" + gap + ")")
    gaps.append("NOT EXISTS (SELECT 1 FROM claude_final_observations o WHERE o.session_key=r.session_key "
                "AND o.response_id=r.response_id AND o.source_id=r.source_id)")
    return con.execute("SELECT r.session_key,r.response_id FROM responses r WHERE r.harness='claude' AND " + scope +
                       " AND EXISTS (SELECT 1 FROM claude_final_observations o WHERE o.session_key=r.session_key "
                       "AND o.response_id=r.response_id) AND (" + " OR ".join(gaps) + ")", args)


def conflicting_final_measurements(con, session_key):
    """A task can bill a final row only when its source measurements agree."""
    if not db._has_table(con, "claude_final_observations"):
        return set()
    mismatch = ["json_extract(o.payload,'$.model') IS NOT r.model"]
    for name in _FINAL_FIELDS:
        if name == "model":
            continue
        value = f"json_extract(o.payload,'$.{name}')"
        mismatch.append(f"({value} IS NOT NULL AND {value} IS NOT r.{name})")
    conflicts = {rid for rid, in con.execute(
        "SELECT DISTINCT o.response_id FROM claude_final_observations o JOIN responses r USING(session_key,response_id) "
        "WHERE o.session_key=? AND (" + " OR ".join(mismatch) + ")", (session_key,))}
    if db._has_table(con, "claude_retained_final_fields"):
        column = "CASE p.field " + " ".join(f"WHEN '{name}' THEN r.{name}" for name in _FINAL_FIELDS) + " END"
        conflicts.update(rid for rid, in con.execute(
            "SELECT DISTINCT p.response_id FROM claude_retained_final_fields p "
            "JOIN responses r USING(session_key,response_id) WHERE p.session_key=? AND p.value IS NOT " + column,
            (session_key,)))
    conflicts.update(rid for _, rid in _unsupported_projections(
        con, "r.session_key=? AND r.source_id IN (SELECT id FROM sources WHERE claude_usage_version BETWEEN 11 AND 16)",
        (session_key,)))
    if db._has_table(con, "claude_withdrawn_finals"):
        conflicts.update(rid for rid, in con.execute(
            "SELECT response_id FROM claude_withdrawn_finals WHERE session_key=?", (session_key,)))
    # Read-only ledgers may already contain an incoherent pre-version-15 row.
    conflicts.update(rid for rid, in con.execute(
        "SELECT response_id FROM responses WHERE session_key=? AND (cache_write_5m_tokens>cache_write_input_tokens "
        "OR cache_write_1h_tokens>cache_write_input_tokens "
        "OR cache_write_1h_tokens<>cache_write_input_tokens-cache_write_5m_tokens)", (session_key,)))
    if db._has_table(con, "claude_expected_finals"):
        complete = ""
        if "requires_complete" in {c[1] for c in con.execute("PRAGMA table_info(claude_expected_finals)")}:
            complete = " AND (e.requires_complete=0 OR json_extract(f.payload,'$.total_tokens') IS NOT NULL)"
        conflicts.update(rid for rid, in con.execute(
            "SELECT e.response_id FROM claude_expected_finals e WHERE e.session_key=? AND NOT EXISTS ("
            "SELECT 1 FROM claude_final_observations f WHERE f.session_key=e.session_key "
            "AND f.source_id=e.source_id AND f.response_id=e.response_id" + complete + ")", (session_key,)))
    conflicts.update(rid for rid, in con.execute(
        "SELECT DISTINCT q.response_id FROM claude_rejected_measurements q WHERE q.session_key=? "
        "AND q.reason IN ('missing_final_usage','malformed_final_usage') AND NOT EXISTS ("
        "SELECT 1 FROM claude_final_observations f WHERE f.session_key=q.session_key "
        "AND f.source_id=q.source_id AND f.response_id=q.response_id)", (session_key,)))
    return conflicts


def responses_without_final_usage(con, session_key):
    if not db._has_table(con, _FINALITY_TABLE):
        return set()
    missing = ""
    if db._has_table(con, "claude_final_observations"):
        missing = (" OR NOT EXISTS (SELECT 1 FROM claude_final_observations o WHERE o.session_key=r.session_key "
                   "AND o.response_id=r.response_id AND json_extract(o.payload,'$.total_tokens') IS NOT NULL)")
    return {rid for rid, in con.execute(
        f"SELECT r.response_id FROM responses r JOIN sources s ON s.id=r.source_id "
        f"WHERE r.session_key=? AND r.is_overlap=0 AND s.claude_usage_version>=13 "
        f"AND (r.total_tokens IS NULL OR NOT EXISTS (SELECT 1 FROM {_FINALITY_TABLE} f "
        f"WHERE f.response_id=r.response_id)" + missing + ")", (session_key,))}


def _ingest_usage_row(r: _Reader, obj: dict, ordinal: int, ts, mid: str,
                      model, values: dict, final: bool) -> None:
    """One streamed usage block for a message id.

    The first block inserts the row; later blocks update it while the message
    is still streaming and non-final. Finality persists per response in a
    durable adapter table keyed by response_id, so a stale copied source or
    a full reimport can never overwrite a finalized row: an exact later
    duplicate is a no-op, compatible finals can supply missing fields, and
    disagreements between measured counters are quarantined. A
    malformed final block never reaches here, so it never finalizes."""
    rid = f"{HARNESS}:{mid}"
    if final:
        r.final_location = (ordinal, ts)
        payload = json.dumps(dict(values, model=model), sort_keys=True)
        r.con.execute("INSERT OR IGNORE INTO claude_final_observations VALUES(?,?,?,?)",
                      (r.session_key, rid, r.src.source_id, payload))
        if values["total_tokens"] is not None:
            r.con.execute("""INSERT INTO claude_expected_finals(source_id,session_key,response_id,requires_complete)
                VALUES(?,?,?,1) ON CONFLICT DO UPDATE SET requires_complete=1""",
                          (r.src.source_id, r.session_key, rid))
    if mid in r.finalized or _is_finalized(r.con, rid):
        r.finalized.add(mid)
        seen = _stored_counters(r, rid)
        ttl_keys = ("cache_write_5m_tokens", "cache_write_1h_tokens")
        if not final:
            if any(seen.get(k) != v for k, v in values.items() if k not in ttl_keys):
                raise _UsageConflict("usage conflict")
            r.stats["responses_duplicate"] += 1
            return
        corrected = _corroborated_final(r, rid, seen)
        supplied = corrected if corrected is not None else {k: v for k, v in values.items() if v is not None}
        combined = {**seen, **supplied}
        if not _cache_consistent(combined.get("cache_write_input_tokens"), *(combined.get(k) for k in ttl_keys)):
            raise _UsageConflict("usage conflict")
        changes = {k: v for k, v in supplied.items() if seen.get(k) != v}
        revisions = {k for k in changes if seen.get(k) is not None}
        # Version 14 could already have combined incompatible cache fields.
        # A coherent complete measurement can repair those fields on replay.
        repair_cache = (values["total_tokens"] is not None and
                        not _cache_consistent(seen.get("cache_write_input_tokens"), *(seen.get(k) for k in ttl_keys))
                        and revisions <= {*ttl_keys, "cache_write_input_tokens", "total_tokens"})
        if revisions and corrected is None and not repair_cache:
            raise _UsageConflict("usage conflict")
        # Only the native parser can supply a total. Never sum fields
        # assembled across partial measurements.
        if changes:
            r.con.execute("UPDATE responses SET " + ",".join(f"{k}=?" for k in changes)
                          + " WHERE session_key=? AND response_id=?", (*changes.values(), r.session_key, rid))
        r.stats["responses_duplicate"] += 1
        return
    cur = r.con.execute(
        "INSERT OR IGNORE INTO responses(response_id, source_id, harness,"
        " session_key, turn_id, session_id, ordinal_num, ts, model, effort,"
        " input_tokens, cached_input_tokens, cache_write_input_tokens,"
        " output_tokens, reasoning_output_tokens, total_tokens, semantics,"
        " cache_write_5m_tokens,cache_write_1h_tokens)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, r.src.source_id, HARNESS, r.session_key, r.turn,
         r.native_session, ordinal, ts, model, obj.get("effort"),
         values["input_tokens"], values["cached_input_tokens"],
         values["cache_write_input_tokens"], values["output_tokens"],
         values["reasoning_output_tokens"], values["total_tokens"],
         SEMANTICS, values["cache_write_5m_tokens"], values["cache_write_1h_tokens"]))
    if cur.rowcount:
        r.stats["responses_inserted"] += 1
        if final:
            _mark_finalized(r.con, rid)
            r.finalized.add(mid)
        return
    if _stored_counters(r, rid) == values:
        r.stats["responses_duplicate"] += 1
        if final:
            _mark_finalized(r.con, rid)
            r.finalized.add(mid)
        return
    # The message is still streaming and non-final (or its final block just
    # arrived): the latest native block is the authority, across imports as
    # well. Final rows never reach this update path.
    r.con.execute(
        "UPDATE responses SET ordinal_num=?, ts=?,"
        " model=COALESCE(?, model), effort=COALESCE(?, effort),"
        " input_tokens=?, cached_input_tokens=?,"
        " cache_write_input_tokens=?, output_tokens=?,"
        " reasoning_output_tokens=?, total_tokens=?,"
        " cache_write_5m_tokens=?, cache_write_1h_tokens=?"
        " WHERE response_id=?",
        (ordinal, ts, model, obj.get("effort"),
         values["input_tokens"], values["cached_input_tokens"],
         values["cache_write_input_tokens"], values["output_tokens"],
         values["reasoning_output_tokens"], values["total_tokens"],
         values["cache_write_5m_tokens"], values["cache_write_1h_tokens"], rid))
    r.stats["responses_updated"] += 1
    if final:
        _mark_finalized(r.con, rid)
        r.finalized.add(mid)


def _final_stop(message):
    reason = message.get("stop_reason")
    return isinstance(reason, str) and bool(reason.strip())


def _assistant(r: _Reader, obj: dict, ordinal: int, ts) -> None:
    message = obj.get("message")
    if message is None:
        message = {}
    if message and not isinstance(message, dict):
        raise ValueError("unsupported message shape")
    if not isinstance(message, dict):
        raise ValueError("unsupported message shape")
    mid = message.get("id")
    model = message.get("model")
    for index, block in enumerate(message.get("content") or []):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use" and block.get("id"):
            raw_name = block.get("name")
            name = raw_name if isinstance(raw_name, str) and raw_name \
                else "unknown"
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            target = _target(name, tool_input)
            r.pending[block["id"]] = (name, tool_input)
            skill = None
            if name == "Skill":
                # Rule 6 via agent_observer/privacy.py: only the validated
                # native skill identifier survives in name and target. A
                # free-text Skill title fails closed to no name and no
                # target, never persisting as either.
                raw_skill = tool_input.get("skill") or tool_input.get("command")
                skill = privacy.filter_target(raw_skill, family="skill_invoke")
                target = skill
            r.event("tool_call", block["id"], ordinal, ts, name=name, target=target,
                    fingerprint=fingerprint(name, json.dumps(tool_input, sort_keys=True)[:4000]))
            if name == "Skill":
                r.event("skill_invoke", block["id"], ordinal, ts, name=skill,
                        target=skill)
            if target and name in EDIT_TOOLS:
                r.event("file_change", block["id"], ordinal, ts, name=name,
                        target=target, fingerprint=fingerprint(target))
        elif btype == "text" and block.get("text"):
            text = block["text"]
            excerpt = privacy.assistant_excerpt(text)
            r.event("assistant_message", f"{obj.get('uuid') or mid}:{index}", ordinal, ts,
                    name="assistant_message", size_bytes=len(text),
                    fingerprint=text_hash(text),
                    detail={"excerpt": excerpt} if excerpt else None)

    if mid and "usage" in message and model != "<synthetic>":
        usage_raw = message.get("usage")
        if usage_raw is None or (isinstance(usage_raw, dict) and not usage_raw):
            pass
        else:
            # Validated before any SQL; malformed buckets quarantine here and
            # never finalize, so a later valid block still inserts normally.
            values = _usage_values(usage_raw)
            _ingest_usage_row(r, obj, ordinal, ts, mid, model, values,
                              _final_stop(message))


def _submission(r: _Reader, native_id, ordinal, ts, kind: str, text: str,
                native_kind: str | None = None, *, origin_allowed: bool = False) -> None:
    # Privacy rule 1 via agent_observer/privacy.py: only a genuine
    # main-session human submission keeps an excerpt. Interrupt, synthetic,
    # command and scaffolding kinds keep an empty excerpt, as does any text
    # from a child sub-agent session or from a record with no native
    # session identity (fail closed when unknown), so file and preference
    # contents can never persist. Identity extraction already saw the
    # complete text.
    excerpt = privacy.submission_excerpt(
        text, is_genuine=kind == "genuine",
        is_main_session=r.agent_id is None and r.native_session is not None)
    genuine = 1 if kind == "genuine" else 0
    cur = r.con.execute(
        "INSERT OR IGNORE INTO user_messages(native_id, source_id, session_key,"
        " turn_id, ordinal_num, ts, kind, text_hash, text_excerpt, is_genuine)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"{HARNESS}:{native_id}", r.src.source_id, r.session_key, r.turn, ordinal,
         ts, kind, text_hash(text), excerpt, genuine))
    if cur.rowcount:
        r.stats["user_messages_inserted"] += 1
    else:
        # Parsed native evidence refreshes derived classification and
        # privacy fields even when the source's privacy version is current.
        r.con.execute(
            "UPDATE user_messages SET source_id=?, session_key=?, turn_id=?,"
            " ordinal_num=?, ts=?, kind=?, text_hash=?, text_excerpt=?,"
            " is_genuine=? WHERE native_id=?",
            (r.src.source_id, r.session_key, r.turn, ordinal, ts, kind,
             text_hash(text), excerpt, genuine, f"{HARNESS}:{native_id}"))
        r.stats["user_messages_updated"] = \
            r.stats.get("user_messages_updated", 0) + 1
    r.con.execute(f"INSERT INTO {_NATIVE_KINDS_TABLE}(native_id,kind,origin_allowed) VALUES(?,?,?)"
                  " ON CONFLICT(native_id) DO UPDATE SET kind=excluded.kind,origin_allowed=excluded.origin_allowed",
                  (f"{HARNESS}:{native_id}", native_kind or kind, int(origin_allowed)))
    intervention_evidence.record(
        r.con, f"{HARNESS}:{native_id}", text,
        sdk=r.meta.get('entrypoint') == 'sdk-ts',
        allowed=origin_allowed)


def _seed_queue_identity(r: _Reader) -> None:
    """Resolve the available source identity before accepting queued text.

    Native enqueue records omit entrypoint and child identity. Inspect the
    complete new portion, and use persisted metadata for incremental suffixes.
    A later child identity also corrects earlier queued rows in place.
    """
    session = r.con.execute("SELECT entrypoint FROM sessions WHERE session_key=?",
                            (r.session_key,)).fetchone()
    stored_entrypoint = session["entrypoint"] if session else None
    if os.path.basename(os.path.dirname(r.src.path)) == "subagents":
        name = os.path.basename(r.src.path)
        if name.startswith("agent-") and name.endswith(".jsonl"):
            r.agent_id = name[len("agent-"):-len(".jsonl")]
    for ordinal, obj, _ in r.src.records():
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "user":
            native = obj.get("uuid") or f"ordinal:{ordinal}"
            r.native_user_ids.add(f"{HARNESS}:{native}")
        if isinstance(obj.get("sessionId"), str) and obj["sessionId"] and not r.native_session:
            r.native_session = obj["sessionId"]
        if obj.get("isSidechain") and isinstance(obj.get("agentId"), str) and obj["agentId"]:
            r.agent_id = obj["agentId"]
        if isinstance(obj.get("entrypoint"), str) and obj["entrypoint"] and "entrypoint" not in r.meta:
            r.meta["entrypoint"] = obj["entrypoint"]
    if "entrypoint" not in r.meta and stored_entrypoint:
        r.meta["entrypoint"] = stored_entrypoint
    if r.src.incremental and r.meta.get("entrypoint") != stored_entrypoint:
        # Recover old queue text through the parser and privacy gate when
        # late entrypoint metadata resolves an earlier unknown.
        r.src.start_offset = r.src.end_offset = 0
    if r.agent_id:
        r.con.execute(
            "UPDATE user_messages SET session_key=?,kind='synthetic',is_genuine=0,text_excerpt=''"
            " WHERE source_id=? AND (native_id LIKE 'claude:queue:%' OR alias_id LIKE 'claude:queue:%')",
            (r.session_key, r.src.source_id))
        r.con.execute(
            f"UPDATE {_NATIVE_KINDS_TABLE} SET kind='synthetic',origin_allowed=0 WHERE native_id IN"
            " (SELECT native_id FROM user_messages WHERE source_id=?"
            " AND (native_id LIKE 'claude:queue:%' OR alias_id LIKE 'claude:queue:%'))",
            (r.src.source_id,))


def _queue_operation(r: _Reader, obj: dict, ordinal: int, ts) -> None:
    operation = obj.get("operation")
    if operation in ("dequeue", "remove"):
        return  # No new input: the enqueue remains the input evidence.
    if operation != "enqueue":
        raise _UnsupportedSchema("unsupported queue operation")
    text = obj.get("content")
    if text is None or text == "":
        return  # Native queues can carry a non-text command without content.
    if not isinstance(text, str):
        raise ValueError("unsupported queued content")
    native = f"queue:{r.native_session or 'file'}:{os.path.basename(r.src.path)}:{ordinal}"
    full = f"{HARNESS}:{native}"
    existing = r.con.execute(
        "SELECT native_id,alias_id FROM user_messages WHERE native_id=? OR alias_id=?",
        (full, full)).fetchone()
    if existing and (existing["native_id"] != full or existing["alias_id"]):
        return  # A matched user's classification and source location win.
    # On adapter upgrade, the later user record may already be indexed.
    # Keep that existing primary key, just as a queue-first import keeps its key.
    candidates = r.con.execute(
        "SELECT u.native_id FROM user_messages u LEFT JOIN claude_native_kinds k ON k.native_id=u.native_id"
        " WHERE session_key=? AND text_hash=? AND ordinal_num>? AND alias_id IS NULL"
        " AND u.native_id NOT LIKE 'claude:queue:%'"
        " AND (?=0 OR (k.kind='synthetic' AND k.origin_allowed=0))"
        " ORDER BY ordinal_num,u.native_id",
        (r.session_key, text_hash(text), ordinal, bool(obj.get("isSidechain") or r.agent_id))).fetchall()
    user = next((row for row in candidates if row["native_id"] in r.native_user_ids), None)
    if user and not existing:
        r.con.execute("UPDATE user_messages SET alias_id=? WHERE native_id=?",
                      (full, user["native_id"]))
        return
    entrypoint = r.meta.get("entrypoint")
    evidence = obj
    if isinstance(entrypoint, str) and entrypoint.startswith("sdk"):
        evidence = {**obj, "promptSource": "sdk"}
    kind = _native_user_kind(evidence, text, agent_id=r.agent_id,
                             entrypoint=entrypoint)
    origin = obj.get("origin") if isinstance(obj.get("origin"), dict) else {}
    explicit_human = origin.get("kind") == "human" or obj.get("promptSource") == "typed"
    if kind == "genuine" and entrypoint != "cli" and not explicit_human:
        kind = "unknown"  # Queue presence is not proof of a human CLI session.
    _submission(r, native, ordinal, ts, kind, text, origin_allowed=not (
        obj.get("isSidechain") or r.agent_id
        or kind in ("interrupt", "command", "scaffolding")))


def _queued_user_id(r: _Reader, native: str, ordinal: int, text: str, *, is_sidechain: bool) -> str:
    """Pair the earliest unpaired enqueue with a later equal user record.

    Session, hash and native order define the pair, independently of turns.
    The first stored primary key never changes, including when assignments or
    delegations already refer to it. alias_id carries the other native identity.
    """
    full = f"{HARNESS}:{native}"
    existing = r.con.execute(
        "SELECT native_id FROM user_messages WHERE native_id=? OR alias_id=?",
        (full, full)).fetchone()
    if existing:
        return existing["native_id"][len(HARNESS) + 1:]
    queued = r.con.execute(
        "SELECT native_id FROM user_messages WHERE session_key=? AND text_hash=?"
        " AND ordinal_num<? AND alias_id IS NULL AND native_id LIKE 'claude:queue:%'"
        " AND (?=1 OR NOT EXISTS (SELECT 1 FROM claude_native_kinds k"
        " WHERE k.native_id=user_messages.native_id AND k.kind='synthetic' AND k.origin_allowed=0))"
        " ORDER BY ordinal_num,native_id LIMIT 1",
        (r.session_key, text_hash(text), ordinal, is_sidechain)).fetchone()
    if queued:
        r.con.execute("UPDATE user_messages SET alias_id=? WHERE native_id=?",
                      (full, queued["native_id"]))
        return queued["native_id"][len(HARNESS) + 1:]
    return native


def prompt_native_id(row) -> str:
    """The consumed user uuid behind a stable queue-first primary key."""
    if row["native_id"].startswith("claude:queue:") and row["alias_id"]:
        return row["alias_id"]
    return row["native_id"]


def _t3_actor(con, uuid: str) -> str | None:
    """The T3 turn origin for one user-record uuid, if T3 recorded it.

    T3 Code drives Claude through the SDK, so its prompts carry
    ``promptSource: "sdk"`` with no ``origin`` and used to read as
    ``synthetic``. A uuid listed as a T3 turn start is human
    (``client``) or dispatched (``server``); anything unlisted (typed
    prompts, Router ``sdk-cli`` workers) is untouched by this lookup.
    A missing T3 mirror table reads as unknown, never as an origin.
    """
    try:
        from . import t3 as _t3
    except ImportError:
        return None
    return _t3.turn_actor(con, uuid)


def _t3_prompt_actor(con, native_session, uuid: str) -> str | None:
    """The Chromeria V2 origin by native session and user-message uuid."""
    try:
        from . import t3 as _t3
    except ImportError:
        return None
    return _t3.prompt_actor(con, f"claude:{native_session}", f"claude:{uuid}")


def _native_user_kind(obj: dict, text: str, *, agent_id=None, entrypoint=None) -> str:
    if obj.get("isSidechain") or agent_id:
        return "synthetic"
    stripped = text.lstrip()
    if stripped.startswith(INTERRUPT_PREFIX):
        return "interrupt"
    # Known synthetic and scaffolding markers are rejected before any
    # human-origin metadata is accepted. A contradictory record carrying
    # origin.kind=human or promptSource=typed is never genuine and keeps an
    # empty excerpt (fail closed on privacy).
    if stripped.startswith(SKILL_BASE_PREFIX):
        # A loaded skill body arrives as message text: skill-load evidence,
        # never a genuine human submission.
        return "scaffolding"
    if obj.get("isMeta") or stripped.startswith(SCAFFOLD_PREFIXES):
        return "command" if stripped.startswith("<command-name>") else "scaffolding"
    origin = obj.get("origin") if isinstance(obj.get("origin"), dict) else {}
    if origin.get("kind") == "human" or obj.get("promptSource") == "typed":
        return "genuine"
    if obj.get("origin") is None and obj.get("promptSource") is None and stripped:
        # Older transcripts carry no origin: plain typed text is genuine.
        return "genuine"
    if obj.get("promptSource") == "sdk" and entrypoint != "sdk-cli":
        return "unknown"
    return "synthetic"


def _user_kind(con, native_session, agent_id, obj: dict, native_kind: str) -> str:
    if obj.get("isSidechain") or agent_id or native_kind in ("interrupt", "command", "scaffolding"):
        return native_kind
    uuid = obj.get("uuid")
    if isinstance(uuid, str) and uuid:
        actor = _t3_actor(con, uuid) or _t3_prompt_actor(con, native_session, uuid)
        if actor == "client":
            return "genuine"
        if actor == "server":
            return "synthetic"
    return native_kind


def _user(r: _Reader, obj: dict, ordinal: int, ts) -> None:
    message = obj.get("message") or {}
    content = message.get("content")
    results = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_result"] \
        if isinstance(content, list) else []
    for result in results:
        _tool_result(r, obj, result, ordinal, ts)
    text = _text(content)
    for value in _strings(content):
        r.identity.observe_text(value)
    if not text and (results or not content):
        return
    if text.startswith(SKILL_BASE_PREFIX):
        # The loaded Skill body arrives as a meta message naming its
        # installed directory: that is the skill-load evidence. Rule 6
        # via agent_observer/privacy.py: only the validated native skill
        # identifier survives in name, target and detail. The installed
        # directory path itself is never a target.
        base = text[len(SKILL_BASE_PREFIX):].splitlines()[0].strip()
        skill_file = base.rstrip("/") + "/SKILL.md"
        r.identity.observe_path(skill_file)
        raw_skill = skill_from_path(base + "/")
        skill = privacy.filter_target(raw_skill,
                                      family="skill_read")
        r.event("skill_read", obj.get("uuid") or f"ordinal:{ordinal}", ordinal, ts,
                name=skill, target=skill, status="ok", size_bytes=len(text),
                detail={"skill": raw_skill, "skill_path": skill_file}
                if (raw_skill or skill_file) else None)
    native_kind = _native_user_kind(obj, text, agent_id=r.agent_id,
                                   entrypoint=r.meta.get("entrypoint"))
    kind = _user_kind(r.con, r.native_session, r.agent_id, obj, native_kind)
    if not text and kind == "synthetic" and not (obj.get("isSidechain") or r.agent_id):
        kind = "unknown"  # A file/image-only native input still has an identity.
    if not text and native_kind == "synthetic" and not (obj.get("isSidechain") or r.agent_id):
        native_kind = "unknown"
    native = obj.get("uuid") or f"ordinal:{ordinal}"
    if kind == "genuine":
        r.turn = f"{HARNESS}:{obj.get('promptId') or native}"
    stored_native = _queued_user_id(r, native, ordinal, text,
                                    is_sidechain=bool(obj.get("isSidechain") or r.agent_id))
    _submission(r, stored_native, ordinal, ts, kind, text, native_kind,
                origin_allowed=not (obj.get("isSidechain") or r.agent_id
                                    or native_kind in ("interrupt", "command", "scaffolding")))


# Claude's toolDenialKind values seen in native records; any other value
# stays out of the ledger.
DENIAL_KINDS = frozenset({"user-rejected", "permission-rule", "automode-blocked",
                          "interrupted"})

def _tool_result(r: _Reader, obj: dict, result: dict, ordinal: int, ts) -> None:
    call_id = result.get("tool_use_id")
    if not call_id:
        return
    name, tool_input = r.pending.pop(call_id, (None, {}))
    body = result.get("content")
    size = len(body) if isinstance(body, str) else len(json.dumps(body or ""))
    denied = obj.get("toolDenialKind")
    status = "denied" if denied else ("error" if result.get("is_error") else "ok")
    structured = obj.get("toolUseResult") if isinstance(obj.get("toolUseResult"), dict) else {}
    detail = {"exit_code": structured.get("exit_code"),
              "exitCode": structured.get("exitCode")}
    r.event("tool_result", call_id, ordinal, ts, name=name or "unknown",
            target=_target(name or "", tool_input), status=status, size_bytes=size,
            detail=detail)
    if denied:
        r.event("permission", call_id, ordinal, ts, name=name or "unknown",
                status="denied",
                detail={"denial": denied} if denied in DENIAL_KINDS else None)
    file_info = structured.get("file") if isinstance(structured.get("file"), dict) else None
    path = (file_info or {}).get("filePath") or (tool_input or {}).get(READ_TOOLS.get(name or "", ""), None)
    if not isinstance(path, str):
        path = None
    if name in READ_TOOLS and path and status == "ok":
        r.identity.observe_path(path)
        raw_skill = skill_from_path(path)
        safe_skill = privacy.filter_target(raw_skill, family="skill_read") \
            if raw_skill else None
        looks_like_skill = path.endswith("SKILL.md") or "/skills/" in path
        family = "skill_read" if (safe_skill or looks_like_skill) else "read"
        # Rule 6 via agent_observer/privacy.py: a skill_read target holds
        # only the validated native skill identifier, never the path.
        # The file path lives only in detail skill_path. A plain read
        # keeps the observed path as its target.
        start = (file_info or {}).get("startLine")
        lines = (file_info or {}).get("numLines")
        if family == "skill_read":
            r.event(family, f"{call_id}:read", ordinal, ts, name=os.path.basename(path),
                    target=safe_skill, status="ok", size_bytes=size,
                    fingerprint=fingerprint(path, start, lines),
                    detail={"start_line": start, "num_lines": lines,
                            "skill": raw_skill, "skill_path": path})
        else:
            r.event(family, f"{call_id}:read", ordinal, ts, name=os.path.basename(path),
                    target=path, status="ok", size_bytes=size,
                    fingerprint=fingerprint(path, start, lines),
                    detail={"start_line": start, "num_lines": lines})


def _attachment(r: _Reader, obj: dict, ordinal: int, ts) -> None:
    attachment = obj.get("attachment") or {}
    atype = attachment.get("type")
    if atype == "hook_additional_context":
        for value in _strings(attachment):
            r.identity.observe_text(value)
    elif atype == "instructions":
        for entry in attachment.get("files") or []:
            if isinstance(entry, dict) and str(entry.get("path") or "").endswith(
                    ("/.claude/CLAUDE.md", "/AGENTS.md")):
                r.identity.observe_loaded_instructions(str(entry.get("content") or ""))
    elif atype == "queued_command":
        prompt = attachment.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            return
        stripped = prompt.lstrip()
        # Scaffolding markers fail closed before human metadata: a queued
        # prompt that is a known marker is never genuine even when it
        # carries humanTurn or origin.kind=human.
        if stripped.startswith(SCAFFOLD_PREFIXES) or stripped.startswith(
                (SKILL_BASE_PREFIX, INTERRUPT_PREFIX)):
            kind = "interrupt" if stripped.startswith(
                INTERRUPT_PREFIX) else "synthetic"
        else:
            origin = attachment.get("origin") if isinstance(attachment.get("origin"), dict) else {}
            human = attachment.get("humanTurn") is True or origin.get("kind") == "human"
            kind = "genuine" if human and attachment.get("commandMode") in (None, "prompt") \
                else "synthetic"
        native = attachment.get("source_uuid") or obj.get("uuid") or f"ordinal:{ordinal}"
        if kind == "genuine":
            r.turn = f"{HARNESS}:{native}"
        _submission(r, native, ordinal, iso_ts(attachment.get("timestamp")) or ts,
                    kind, prompt)


def _system(r: _Reader, obj: dict, ordinal: int, ts) -> None:
    subtype = obj.get("subtype")
    native = obj.get("uuid") or f"ordinal:{ordinal}"
    if subtype == "compact_boundary":
        r.stats["compactions"] += 1
        r.event("compaction", native, ordinal, ts, name="compact_boundary")
    elif subtype == "turn_duration":
        r.event("lifecycle", native, ordinal, ts, name="turn_duration",
                duration_ms=obj.get("durationMs"))
    elif subtype in ("api_error", "stop_hook_summary", "informational"):
        r.event("lifecycle", native, ordinal, ts, name=subtype,
                status=obj.get("level"))


def _prepare_ancestry(r):
    r.con.execute("""
        CREATE TABLE IF NOT EXISTS claude_response_ancestry (
            session_key TEXT NOT NULL, uuid TEXT NOT NULL, parent TEXT NOT NULL,
            input_id TEXT NOT NULL, response_id TEXT NOT NULL,
            source_id INTEGER NOT NULL REFERENCES sources(id),
            link_status TEXT NOT NULL DEFAULT 'unknown',
            PRIMARY KEY(session_key, uuid, parent, input_id, response_id, source_id)
        ) WITHOUT ROWID""")
    if "link_status" not in {col[1] for col in r.con.execute("PRAGMA table_info(claude_response_ancestry)")}:
        r.con.execute("ALTER TABLE claude_response_ancestry ADD COLUMN link_status TEXT NOT NULL DEFAULT 'unknown'")
    r.con.execute("CREATE INDEX IF NOT EXISTS claude_ancestry_source"
                  " ON claude_response_ancestry(source_id)")
    r.con.execute("""CREATE TABLE IF NOT EXISTS claude_ancestry_sources (
        session_key TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES sources(id),
        PRIMARY KEY(session_key,source_id)) WITHOUT ROWID""")
    r.con.execute("""CREATE TABLE IF NOT EXISTS claude_rejected_measurements (
        session_key TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES sources(id),
        uuid TEXT NOT NULL, parent TEXT NOT NULL, response_id TEXT NOT NULL, reason TEXT NOT NULL,
        PRIMARY KEY(session_key, source_id, uuid, parent, response_id, reason)) WITHOUT ROWID""")
    r.con.execute("""CREATE TABLE IF NOT EXISTS claude_final_observations (
        session_key TEXT NOT NULL, response_id TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES sources(id),
        payload TEXT NOT NULL, PRIMARY KEY(session_key,response_id,source_id,payload)) WITHOUT ROWID""")
    r.con.execute("CREATE INDEX IF NOT EXISTS claude_final_observation_source ON claude_final_observations(source_id)")
    r.con.execute("""CREATE TABLE IF NOT EXISTS claude_retained_final_fields (
        session_key TEXT NOT NULL, response_id TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES sources(id),
        field TEXT NOT NULL, value NOT NULL,
        PRIMARY KEY(session_key,response_id,source_id,field,value)) WITHOUT ROWID""")
    r.con.execute("CREATE INDEX IF NOT EXISTS claude_retained_field_source ON claude_retained_final_fields(source_id)")
    r.con.execute("""CREATE TABLE IF NOT EXISTS claude_final_blocks (
        session_key TEXT NOT NULL, response_id TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES sources(id),
        uuid TEXT NOT NULL, parent TEXT NOT NULL, link_status TEXT NOT NULL, turn_id TEXT, ordinal_num INTEGER NOT NULL,
        ts REAL, PRIMARY KEY(session_key,response_id,source_id,ordinal_num)) WITHOUT ROWID""")
    r.con.execute("CREATE INDEX IF NOT EXISTS claude_block_source ON claude_final_blocks(source_id)")
    r.con.execute("""CREATE TABLE IF NOT EXISTS claude_withdrawn_finals (
        session_key TEXT NOT NULL,response_id TEXT NOT NULL, PRIMARY KEY(session_key,response_id)) WITHOUT ROWID""")
    r.con.execute("""CREATE TABLE IF NOT EXISTS claude_event_observations (
        source_id INTEGER NOT NULL REFERENCES sources(id),event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        turn_id TEXT NOT NULL, PRIMARY KEY(event_id,source_id,turn_id)) WITHOUT ROWID""")
    r.con.execute("CREATE INDEX IF NOT EXISTS claude_event_observation_source ON claude_event_observations(source_id)")
    if r.src.row["claude_usage_version"] and r.src.row["claude_usage_version"] < _IMPORT_VERSION:
        stale = _unsupported_projections(r.con,
            "(r.source_id=? OR (r.session_key,r.response_id) IN (SELECT session_key,response_id "
            "FROM claude_final_observations WHERE source_id=?))", (r.src.source_id, r.src.source_id))
        r.con.executemany("INSERT OR IGNORE INTO claude_withdrawn_finals VALUES(?,?)", stale)
    if not db._has_table(r.con, "claude_expected_finals"):
        r.con.execute("""CREATE TABLE claude_expected_finals (
            source_id INTEGER NOT NULL REFERENCES sources(id), session_key TEXT NOT NULL, response_id TEXT NOT NULL,
            PRIMARY KEY(session_key,source_id,response_id)) WITHOUT ROWID""")
        r.con.execute("""INSERT OR IGNORE INTO claude_expected_finals
            SELECT source_id,session_key,response_id FROM claude_final_observations UNION
            SELECT source_id,session_key,response_id FROM claude_rejected_measurements
            WHERE reason IN ('missing_final_usage','malformed_final_usage','unreadable_record')""")
    if "requires_complete" not in {c[1] for c in r.con.execute("PRAGMA table_info(claude_expected_finals)")}:
        r.con.execute("ALTER TABLE claude_expected_finals ADD COLUMN requires_complete INTEGER NOT NULL DEFAULT 0")
        r.con.execute("""UPDATE claude_expected_finals SET requires_complete=1 WHERE
            (source_id,session_key,response_id) IN (SELECT source_id,session_key,response_id
            FROM claude_final_observations WHERE json_extract(payload,'$.total_tokens') IS NOT NULL)""")
    r.con.execute("""CREATE TABLE IF NOT EXISTS claude_unreadable_sources (
        session_key TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES sources(id),
        PRIMARY KEY(session_key,source_id)) WITHOUT ROWID""")
    r.linked_sessions = set()
    if not r.src.incremental:
        r.con.execute("DELETE FROM claude_event_observations WHERE source_id=?", (r.src.source_id,))
        r.con.execute("DELETE FROM claude_final_blocks WHERE source_id=?", (r.src.source_id,))
        # Keep identities only until the replacement proves it could read
        # every row. A broken line cannot silently retract a competing copy.
        r.con.execute("CREATE TEMP TABLE claude_prior_edges AS "
                      "SELECT session_key,source_id,uuid,parent,response_id,'' AS reason FROM claude_response_ancestry "
                      "WHERE source_id=? AND response_id<>'' UNION "
                      "SELECT session_key,source_id,uuid,parent,response_id,reason FROM claude_rejected_measurements "
                      "WHERE source_id=?", (r.src.source_id, r.src.source_id))
        r.con.execute("CREATE TEMP TABLE claude_prior_finals AS SELECT session_key,response_id,source_id,payload "
                      "FROM claude_final_observations WHERE source_id=?", (r.src.source_id,))
        r.con.execute("DELETE FROM claude_unreadable_sources WHERE source_id=?", (r.src.source_id,))
        if r.src.row["claude_usage_version"] in (6, 7, 8, 9):
            r.con.execute("INSERT OR IGNORE INTO claude_ancestry_sources "
                          "SELECT DISTINCT session_key,source_id FROM claude_response_ancestry "
                          "WHERE source_id=? AND parent<>''", (r.src.source_id,))
        if db._has_table(r.con, "claude_invalid_ancestry"):
            r.con.execute("INSERT OR IGNORE INTO claude_ancestry_sources "
                          "SELECT session_key,source_id FROM claude_invalid_ancestry WHERE source_id=?",
                          (r.src.source_id,))
            r.con.execute("DELETE FROM claude_invalid_ancestry WHERE source_id=?", (r.src.source_id,))
        r.con.execute("DELETE FROM claude_rejected_measurements WHERE source_id=?", (r.src.source_id,))
        r.con.execute("DELETE FROM claude_final_observations WHERE source_id=?", (r.src.source_id,))
        r.con.execute("DELETE FROM claude_response_ancestry WHERE source_id=?", (r.src.source_id,))


def _record_ancestry(r, obj, rejected=None):
    uuid = native_response.identifier(obj.get("uuid"))
    if (not r.native_session or obj.get("sessionId") != r.native_session
            or (obj.get("agentId") and obj["agentId"] != r.agent_id)):
        return
    parent = native_response.identifier(obj.get("parentUuid"))
    kind = obj.get("type")
    message = obj.get("message")
    if kind in ("user", "assistant") and (
            not isinstance(message, dict) or message.get("role", kind) != kind):
        return
    input_id = response_id = ""
    if kind == "user":
        content = message.get("content")
        if ((isinstance(content, str) and content) or
                (isinstance(content, list) and any(
                    isinstance(block, dict) and block.get("type") in ("text", "image", "document")
                    for block in content))):
            input_id = uuid
        elif not (isinstance(content, list) and content and all(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content)):
            return
    elif kind == "assistant":
        mid = native_response.identifier(message.get("id"))
        if mid and message.get("model") != "<synthetic>":
            response_id = f"{HARNESS}:{mid}"
            if _final_stop(message):
                r.con.execute("INSERT OR IGNORE INTO claude_expected_finals(source_id,session_key,response_id)"
                              " VALUES(?,?,?)",
                              (r.src.source_id, r.session_key, response_id))
                if rejected == "malformed_usage":
                    rejected = "malformed_final_usage"
                elif not rejected and not message.get("usage"):
                    rejected = "missing_final_usage"
            for family, event_id in r.message_events:
                native_response.record_event(r.con, r.src.source_id, r.session_key, family, event_id, response_id)
    elif kind == "attachment" and isinstance(obj.get("attachment"), dict):
        if obj["attachment"].get("type") == "queued_command":
            # A consumed queued input has its own identity. A missing identity
            # must not send its descendants back to the previous prompt.
            input_id = native_response.identifier(obj["attachment"].get("source_uuid"))
            if input_id is None:
                return
    elif kind not in ("system", "progress"):
        return
    if not uuid and not response_id and "parentUuid" not in obj and "uuid" not in obj:
        return
    invalid = ((not uuid and ("uuid" in obj or obj.get("parentUuid") is not None))
               or (not parent and obj.get("parentUuid") is not None))
    if invalid:
        status = "invalid"
    elif parent:
        status = "linked"
    elif "parentUuid" in obj and not input_id:
        status = "root"
    else:
        status = "absent"
    # Erasing the last parent on rewrite cannot turn this source into a
    # pre-ancestry format. Remember the format independently of ownership.
    if status != "absent":
        r.linked_sessions.add(r.session_key)
    if response_id and r.final_location is not None:
        r.con.execute("INSERT OR REPLACE INTO claude_final_blocks VALUES(?,?,?,?,?,?,?,?,?)",
                      (r.session_key, response_id, r.src.source_id, uuid or "", parent or "", status,
                       r.turn, *r.final_location))
    if rejected and response_id:
        r.con.execute("INSERT OR IGNORE INTO claude_rejected_measurements VALUES(?,?,?,?,?,?)",
                      (r.session_key, r.src.source_id, uuid or "", parent or "", response_id, rejected))
        return
    _save_edge(r.con, r.session_key, uuid or "", "" if input_id else parent or "",
               input_id, response_id, r.src.source_id, status)


def _save_edge(con, session, uuid, parent, input_id, response_id, source, status):
    con.execute("INSERT INTO claude_response_ancestry"
                "(session_key,uuid,parent,input_id,response_id,source_id,link_status)"
                " VALUES(?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET link_status="
                "CASE WHEN link_status='invalid' OR excluded.link_status='invalid' THEN 'invalid' "
                "WHEN link_status='linked' OR excluded.link_status='linked' THEN 'linked' "
                "WHEN link_status='root' OR excluded.link_status='root' THEN 'root' ELSE excluded.link_status END",
                (session, uuid, parent, input_id, response_id, source, status))


def legacy_capture_sources(con, session_key):
    """Sources with historical turn capture, or positive evidence of a parentless format."""
    legacy = {r[0] for r in con.execute(
        "SELECT DISTINCT e.source_id FROM events e JOIN sources s ON s.id=e.source_id "
        "WHERE e.session_key=? AND COALESCE(s.claude_usage_version,0)<6", (session_key,))}
    if "link_status" in {c[1] for c in con.execute("PRAGMA table_info(claude_response_ancestry)")}:
        legacy.update(r[0] for r in con.execute(
            "SELECT source_id FROM claude_response_ancestry WHERE session_key=? GROUP BY source_id "
            "HAVING MIN(link_status='absent' AND parent='') AND MAX(response_id<>'')", (session_key,)))
    if db._has_table(con, "claude_ancestry_sources"):
        legacy.difference_update(source for source, in con.execute(
            "SELECT source_id FROM claude_ancestry_sources WHERE session_key=?", (session_key,)))
    return legacy


def response_witnesses(con, session_key):
    """Resolve every UUID once, stopping each chain at its nearest native input.

    A missing parent or cycle leaves that chain unresolved. Multiple observed
    parents must all resolve, and competing inputs survive for the consumer.
    """
    if not db._has_table(con, "claude_response_ancestry"):
        unresolved = {rid: set() for rid, in con.execute(
            "SELECT r.response_id FROM responses r JOIN sources s ON s.id=r.source_id "
            "WHERE r.session_key=? AND r.is_overlap=0 AND s.claude_usage_version>=6", (session_key,))}
        return unresolved, set()
    parents, children, anchors, blocks = (defaultdict(set) for _ in range(4))
    seen, disputed = {}, set()
    legacy_sources = legacy_capture_sources(con, session_key)
    accepted = set()
    recorded_blocks = set()
    unlinked = defaultdict(set)
    unusable, unlocated, linked_responses = set(), set(), set()
    columns = {col[1] for col in con.execute("PRAGMA table_info(claude_response_ancestry)")}
    status_column = "link_status" if "link_status" in columns else "'unknown'"
    for uuid, parent, input_id, rid, source, status in con.execute(
            f"SELECT uuid,parent,input_id,response_id,source_id,{status_column} "
            "FROM claude_response_ancestry WHERE session_key=?", (session_key,)):
        if rid and uuid:
            recorded_blocks.add((rid, source))
            accepted.add((uuid, parent, rid))
        if rid and status == "absent":
            unlinked[rid].add(source)
        if rid and parent:
            linked_responses.add(rid)
        if rid and status in {"root", "invalid"}:
            unusable.add(rid)
        if not uuid:
            if rid:
                unlocated.add(rid)
            continue  # No graph vertex. The response still has a source observation.
        edge = (parent, input_id, rid)
        if seen.setdefault(uuid, edge) != edge:
            disputed.add(uuid)
        if input_id:
            anchors[uuid].add(input_id)
        elif parent:
            parents[uuid].add(parent)
            children[parent].add(uuid)
        if rid:
            blocks[rid].add(uuid)
    if db._has_table(con, "native_response_events"):
        # Version 7 kept an event edge even when it dropped the response's
        # malformed graph position. That copy cannot validate another copy.
        for rid, source in con.execute(
                "SELECT DISTINCT e.response_id,e.source_id FROM native_response_events e JOIN sources s ON s.id=e.source_id "
                "WHERE e.session_key=? AND s.claude_usage_version BETWEEN 7 AND 9", (session_key,)):
            if (rid, source) not in recorded_blocks:
                unlocated.add(rid)
    conflicts = set()
    if db._has_table(con, "claude_invalid_ancestry"):
        for uuid, response_id in con.execute(
                "SELECT uuid,response_id FROM claude_invalid_ancestry WHERE session_key=?", (session_key,)):
            if uuid:
                disputed.add(uuid)
            if response_id:
                conflicts.add(response_id)
    if db._has_table(con, "claude_rejected_measurements"):
        for uuid, parent, rid, reason, same_source in con.execute(
                "SELECT q.uuid,q.parent,q.response_id,q.reason,EXISTS (SELECT 1 FROM claude_response_ancestry a "
                "WHERE a.source_id=q.source_id AND a.session_key=q.session_key AND a.response_id=q.response_id) "
                "FROM claude_rejected_measurements q WHERE q.session_key=?",
                (session_key,)):
            # Rejected measurements cannot grant ownership. A later usable
            # block can confirm the same edge. A counter conflict on a new
            # block cannot revise an already finalized response either.
            if (reason != "unreadable_record" and (uuid, parent, rid) in accepted
                    or (reason == "usage_conflict" and same_source and uuid not in seen)):
                continue
            conflicts.add(rid)
            if uuid:
                disputed.add(uuid)
    # Contradictory copies of any ancestor also dispute its descendants,
    # even when a missing parent keeps the ordinary walk from reaching them.
    pending = deque(disputed)
    while pending:
        for child in children.get(pending.popleft(), ()):
            if child not in disputed:
                disputed.add(child)
                pending.append(child)
    # Process an interior node only after all its parents have resolved.
    ready = deque(uuid for uuid in anchors if uuid not in parents)
    resolved = set()
    while ready:
        uuid = ready.popleft()
        resolved.add(uuid)
        for child in children.get(uuid, ()):
            parents[child].remove(uuid)
            anchors[child].update(anchors[uuid])
            if not parents[child]:
                ready.append(child)
    results = {}
    for response_id, source, version in con.execute(
            "SELECT r.response_id,r.source_id,s.claude_usage_version FROM responses r "
            "JOIN sources s ON s.id=r.source_id WHERE r.session_key=? AND r.is_overlap=0", (session_key,)):
        nodes = blocks[response_id]
        if nodes & disputed or (nodes & resolved and (nodes - resolved or response_id in unlocated)):
            conflicts.add(response_id)
        elif nodes and nodes <= resolved:
            results[response_id] = set.union(*(anchors[node] for node in nodes))
        elif (response_id in unusable or response_id in linked_responses or unlinked[response_id] - legacy_sources
              or ((version or 0) >= 6 and source not in unlinked[response_id])):
            # Only a positively observed parentless record from a legacy source
            # can fall back to its captured display turn. Versions 6/7 lost the
            # distinction between absent and malformed parents until replay.
            results[response_id] = set()
    return results, conflicts


def response_inputs(con, session_key):
    witnesses, conflicts = response_witnesses(con, session_key)
    return {rid: ids for rid, ids in witnesses.items() if ids and rid not in conflicts}
