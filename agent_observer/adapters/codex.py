"""Codex native adapter: session rollout JSONL into the ledger.

Reads persisted Codex rollouts (`~/.codex/sessions/**/rollout-*.jsonl`
and `~/.codex/archived_sessions/**/rollout-*.jsonl`).
Counter rule: each token_usage_record usage block is one atomic response.
turn_token_usage and thread_token_usage are overlapping usage snapshots and are
stored per row but never summed. The compaction latest_token_usage_record is
overlapping evidence of an existing response and is never inserted as a row.
In this schema cached input is a subset of input and reasoning is part of
output, so total_tokens = input_tokens + output_tokens.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3

from .. import db, privacy
from ..identity import SessionIdentity, skill_from_path
from ..ingest import (JsonlSource, MissingNativeId, fingerprint, insert_event,
                      iso_ts, text_hash)

def _native_ordinal(obj) -> int | None:
    if not isinstance(obj, dict):
        return None
    value = obj.get("ordinal")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _note_occurrence(r, kind: str, key, obj, position: int | None = None,
                     model=None, effort=None, session_key: str | None = None) -> None:
    """Record this source's first position for one identity. A later sight
    in the same source does not move it. Another source inserts its own row.
    """
    if position is None:
        position = r.record_position
    if position is None or key is None or key == "":
        return
    r.con.execute(
        "INSERT OR IGNORE INTO codex_occurrences(source_id, session_key,"
        " row_kind, row_key, record_position, native_ordinal, model, effort)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (r.src.source_id, session_key or r.session_key, kind, str(key),
         position, _native_ordinal(obj), model, effort))


def _occurrences_ready(row) -> bool:
    if row is None:
        return False
    try:
        return row["occurrence_backfill"] == 1
    except (KeyError, IndexError, TypeError):
        return False


HARNESS = "codex"
SEMANTICS = "codex:input_includes_cached,output_includes_reasoning"
# Older rollouts carry usage only in token_count events: last_token_usage per
# response and a cumulative total that repeats when nothing new happened.
TOKEN_COUNT_SEMANTICS = SEMANTICS + ";source=token_count"
SYNTHETIC_PREFIX = "<send_user_message_question_reply>"
DEFAULT_ROOT = os.path.expanduser("~/.codex/sessions")
ARCHIVE_ROOT = os.path.expanduser("~/.codex/archived_sessions")


CAPABILITIES = [
    # (family, supported, detail)
    ("model_usage", True, "token_usage_record per response_id; input includes cached input; output includes reasoning"),
    ("tool_calls", True, "response_item function_call/custom_tool_call with call_id join to outputs"),
    ("tool_results", True, "function_call_output/custom_tool_call_output joined by call_id; completed CommandExecution/McpToolCall retained with native ids"),
    ("compaction", True, "compacted record with window ids and compaction_response_id; latest usage is overlap"),
    ("lifecycle_task", True, "event_msg task_started/task_complete with turn timing; turn_aborted marks cancelled turns"),
    ("lifecycle_dispatch", True, "collaboration.spawn_agent call plus SubAgentActivity started; child join needs explicit delegation capture"),
    ("file_change", True, "FileChange items with path, change kind, content size/hash; contents never stored"),
    ("read_evidence", True, "CommandExecution parsed_cmd entries of type read; target/size/owner/window preserved"),
    ("skill_file_reads", True, "read events under an installed Skill directory; mentions never count"),
    ("skill_invocation", False, "no native skill-invocation event observed in Codex sample; stays unknown"),
    ("timing_tool", True, "CommandExecution duration when reported; otherwise unknown, never invented"),
    ("instruction_identity", True, "AgentsMD direction block in injected messages and versioned plugin paths read"),
    ("quota", False, "rate-limit snapshots are account evidence, not task attribution; stays unknown at task level"),
]


def _fallback_native_id(path: str, position: int) -> str:
    """Native rollout name and complete-record position, stable on replay.

    Continuation files share a session but have independent record positions.
    Keeping the filename also preserves identity when a rollout is archived.
    """
    return f"file:{os.path.basename(path)}:ordinal:{position}"


class _MalformedUsage(ValueError):
    """A usage bucket that is not NULL or a real integer counter."""


class _UsageConflict(ValueError):
    """An exact-ID repeat whose counters differ from the stored row."""


class _UnsupportedSchema(ValueError):
    """A record type the adapter does not support."""


_CUMULATIVE_TABLE = "codex_fallback_cumulative"


def _ensure_cumulative_table(con: sqlite3.Connection) -> None:
    """Adapter-owned store of first-seen cumulative fallback signatures.

    One row per session and cumulative total holds all six
    _CODEX_BUCKET_KEYS from total_token_usage. It seeds the
    post-compaction repeat comparison on later incremental imports whose
    suffix begins with the repeat, so a split import is treated exactly
    like a full one. Unknown prior signatures stay absent and fail
    closed into usage_conflict.
    """
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {_CUMULATIVE_TABLE}"
        "(session_key TEXT NOT NULL, thread_total INTEGER NOT NULL,"
        " input_tokens INTEGER, cached_input_tokens INTEGER,"
        " cache_write_input_tokens INTEGER, output_tokens INTEGER,"
        " reasoning_output_tokens INTEGER, total_tokens INTEGER,"
        " PRIMARY KEY(session_key, thread_total))")


def _load_cumulative_sigs(con: sqlite3.Connection,
                          session_key: str) -> dict:
    """First-seen cumulative signatures persisted by earlier imports."""
    sigs: dict = {}
    try:
        rows = con.execute(
            f"SELECT input_tokens, cached_input_tokens,"
            f" cache_write_input_tokens, output_tokens,"
            f" reasoning_output_tokens, total_tokens, thread_total"
            f" FROM {_CUMULATIVE_TABLE} WHERE session_key=?",
            (session_key,))
    except sqlite3.DatabaseError:
        return sigs
    for row in rows:
        try:
            total = row["thread_total"]
            sigs[total] = tuple((k, row[k]) for k in _CODEX_BUCKET_KEYS)
        except (KeyError, IndexError, TypeError):
            continue
    return sigs


def _remember_cumulative_sig(r: _Reader, total: int, total_raw: dict) -> None:
    """Persist the first-seen cumulative signature for one total.

    First-seen only (INSERT OR IGNORE): a later observation never moves
    the reference, so a changed cumulative bucket always conflicts
    instead of becoming its own proof.
    """
    try:
        r.con.execute(
            f"INSERT OR IGNORE INTO {_CUMULATIVE_TABLE}"
            "(session_key, thread_total, input_tokens,"
            " cached_input_tokens, cache_write_input_tokens,"
            " output_tokens, reasoning_output_tokens, total_tokens)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (r.session_key, total,
             total_raw.get("input_tokens"),
             total_raw.get("cached_input_tokens"),
             total_raw.get("cache_write_input_tokens"),
             total_raw.get("output_tokens"),
             total_raw.get("reasoning_output_tokens"),
             total_raw.get("total_tokens")))
    except sqlite3.DatabaseError:
        pass


def _deferred_line(obj) -> str:
    """Key-only JSON form of a deferred record for the privacy helper.

    The deferred token_count path carries the parsed record but no raw
    line; the privacy line_excerpt keeps only sorted top-level key names
    (never values), so a names-only object is sufficient and safest.
    """
    if isinstance(obj, dict):
        try:
            return json.dumps({k: None for k in obj})
        except (TypeError, ValueError):
            return ""
    return ""


_CODEX_BUCKET_KEYS = ("input_tokens", "cached_input_tokens",
                      "cache_write_input_tokens", "output_tokens",
                      "reasoning_output_tokens", "total_tokens")


def _valid_counter(value) -> bool:
    """Only NULL or a real integer counter; booleans are malformed."""
    if value is None:
        return True
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_bucket(bucket, *, allow_none: bool = True):
    """Validate one usage bucket before any SQL.

    None/missing stays unknown (NULL). A dict is validated counter by
    counter; any other shape, including falsey lists, strings or numbers,
    is malformed and quarantined.
    """
    if bucket is None:
        if allow_none:
            return
        raise _MalformedUsage("malformed usage")
    if not isinstance(bucket, dict):
        raise _MalformedUsage("malformed usage")
    for key in _CODEX_BUCKET_KEYS:
        if key in bucket and not _valid_counter(bucket[key]):
            raise _MalformedUsage("malformed usage counter")


def _rollout_paths(root: str) -> list[str]:
    """Rollout files under root. A missing directory contributes none."""
    return glob.glob(os.path.join(root, "**", "rollout-*.jsonl"), recursive=True)


def discover(root: str | None = None) -> list[str]:
    """Rollout files under one root, or the live tree plus the archive.

    An explicit root is that directory only. With root unset, scan
    ``~/.codex/sessions`` and ``~/.codex/archived_sessions``. The same
    rollout basename in both trees is the live file; continuation files
    (``rollout-...-<id>_<segment>.jsonl``) have different names and both
    stay. Dedup is by basename, not by thread id.
    """
    if root:
        return sorted(_rollout_paths(root))
    live = _rollout_paths(DEFAULT_ROOT)
    live_names = {os.path.basename(path) for path in live}
    archived = [path for path in _rollout_paths(ARCHIVE_ROOT)
                if os.path.basename(path) not in live_names]
    return sorted(live + archived)


def sync(con: sqlite3.Connection, root: str | None = None, full: bool = False,
         source: str | None = None) -> dict:
    """Import every rollout under root, or one source file.

    With root unset, import the live session tree and the archive.
    """
    paths = [source] if source else discover(root)
    totals = {"harness": HARNESS, "sources": 0, "unchanged": 0,
              "responses_inserted": 0, "events_inserted": 0,
              "user_messages_inserted": 0, "malformed": 0, "failed": []}
    for path in paths:
        try:
            stats = import_codex_file(con, path, full=full)
        except (OSError, sqlite3.DatabaseError) as exc:
            # A failed replay leaves its transaction open. The next source
            # would commit that partial replacement, including a deleted
            # occurrence set whose readiness marker still says complete.
            if con.in_transaction:
                con.rollback()
            totals["failed"].append({"path": path, "error": str(exc)})
            continue
        totals["sources"] += 1
        totals["unchanged"] += 1 if stats.get("unchanged") else 0
        for key in ("responses_inserted", "events_inserted",
                    "user_messages_inserted", "malformed"):
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
        self.thread_source = src.row["thread_source"]
        self.cli_version = src.row["cli_version"]
        self.identity = SessionIdentity()
        self.model = src.row["codex_model"] if src.start_offset else None
        self.effort = src.row["codex_effort"] if src.start_offset else None
        self.first_ts = None
        self.last_ts = None
        self.meta: dict = {}
        self.token_counts: list = []
        self.usage_context_seen: set[str] = set()
        # First sight of a native id inside this import. A later exact
        # repeat in the same file must not move that position. Another
        # file records its own occurrence and does not take this one.
        self.events_seen: set[tuple] = set()
        self.user_messages_seen: set[str] = set()
        # Compaction context for deferred token_count flushing: token_count
        # usage snapshots are collected during ingestion and reconciled only at
        # flush, so each usage snapshot captures whether a compaction boundary
        # (compacted record or ContextCompaction item) already preceded it.
        # A repeated cumulative usage snapshot after compaction whose cumulative
        # bucket is unchanged is duplicate evidence, not a conflict.
        self.compaction_seen = False
        # Ordering guard for the deferred flush (set by import_codex_file
        # before ingestion): the structural ordinal where this import's
        # prefix begins, and a high-water mark over the events table. A
        # usage snapshot counts as post-compaction only when the boundary
        # precedes it: the captured flag covers this import, and the ledger
        # query below additionally requires id <= prior_event_ceiling (so
        # compaction events written later in this same import never leak
        # in) and ordinal_num < import_start_ordinal (so only evidence from
        # before the imported prefix counts on incremental imports; full
        # re-reads start at zero and consult nothing but the captured flag).
        self.import_start_ordinal = 0
        self.record_position = None
        self.prior_event_ceiling = None
        # Every own-thread identity seen so far (persisted plus prescanned
        # plus streamed): the main-session gate fails closed on divergence.
        # Referenced worker threads (spawn targets) never enter this set.
        self.observed_threads: set = set()
        self.own: _Own | None = None
        # True only for a ledger imported before privacy 9, whose locators
        # are still raw metadata ordinals. A later privacy refresh is stale
        # too, and its locators are already file positions.
        self.metadata_namespace = False
        if isinstance(self.thread_id, str) and self.thread_id:
            self.observed_threads.add(self.thread_id)

    @property
    def session_key(self) -> str:
        native = self.thread_id or self.session_id
        if native:
            return f"{HARNESS}:{native}"
        return f"{HARNESS}:file:{os.path.basename(self.src.path)}"

    @property
    def is_main(self) -> bool:
        """Privacy rule 1 gate via agent_observer/privacy.py.

        Only a proven human main session keeps submission excerpts; child
        and worker rollouts and unknown metadata keep none.
        """
        return privacy.is_main_session(
            thread_source=self.thread_source,
            session_id=self.session_id,
            thread_id=self.thread_id,
            observed_thread_ids=tuple(self.observed_threads))

    def _legacy_source_matches(self, source_id) -> bool:
        if source_id == self.src.source_id:
            return True
        row = self.con.execute("SELECT path FROM sources WHERE id=? AND harness=?",
                               (source_id, HARNESS)).fetchone()
        # Archival relocation keeps the native rollout filename. A different
        # continuation file has a different filename, even in the same session.
        return bool(row and os.path.basename(row["path"]) == os.path.basename(self.src.path))

    def event(self, obj, family, native_id, *, legacy_id=None, **kw):
        if self.src.privacy_stale and legacy_id:
            prior = self.con.execute(
                "SELECT id,source_id FROM events WHERE session_key=?"
                " AND family=? AND native_id=?",
                (self.session_key, family, legacy_id)).fetchone()
            if prior and self._legacy_source_matches(prior["source_id"]):
                # The old parser retained the first such record. Move that
                # row, and any verdict referring to it, before replaying.
                self.con.execute("UPDATE events SET native_id=?,ordinal_num=?,source_id=? WHERE id=?",
                                 (native_id, self.record_position, self.src.source_id, prior["id"]))
                self.con.execute("UPDATE verdicts SET intervention_id=? WHERE intervention_id=?",
                                 (f"ev:{self.session_key}/{family}/{native_id}",
                                  f"ev:{self.session_key}/{family}/{legacy_id}"))
        identity = (family, native_id)
        prior = self.con.execute(
            "SELECT id, source_id FROM events WHERE session_key=?"
            " AND family=? AND native_id=?",
            (self.session_key, family, native_id)).fetchone()
        # A privacy reimport may adopt this file's position only for a row
        # this source already holds. A different file keeps its own locator.
        same_source = prior is None or prior["source_id"] == self.src.source_id
        first = identity not in self.events_seen
        self.events_seen.add(identity)
        insert_event(self.con, self.stats, source_id=self.src.source_id,
                     session_key=self.session_key, family=family,
                     native_id=native_id, ordinal=self.record_position,
                     ts=iso_ts(obj.get("timestamp")),
                     update=self.src.privacy_stale and same_source,
                     migrate_ordinal=first and same_source, **kw)
        event_id = prior["id"] if prior is not None else None
        if event_id is None:
            stored = self.con.execute(
                "SELECT id FROM events WHERE session_key=? AND family=? AND native_id=?",
                (self.session_key, family, native_id)).fetchone()
            event_id = stored["id"] if stored is not None else None
        if event_id is not None:
            _note_occurrence(self, "event", event_id, obj)


class _Own:
    """The rollout's own identity, from its first session_meta (#69).

    A forked child rollout starts with its own session_meta, then a copy of
    the parent's session_meta and the parent's history up to
    subagent_history_start_ordinal (a native ordinal), then its own
    records. The copied records belong to the session they came from, which
    imports them from its own rollout.
    """

    def __init__(self, payload: dict):
        self.thread = payload["id"]
        self.session = payload.get("session_id") or payload["id"]
        self.source = payload.get("thread_source")
        start = payload.get("subagent_history_start_ordinal")
        self.history_start = start if isinstance(start, int) \
            and not isinstance(start, bool) else None
        parent = payload.get("forked_from_id") or \
            payload.get("parent_thread_id")
        self.parent = parent if isinstance(parent, str) and parent \
            and parent != self.thread else None

    def inherited(self, obj: dict) -> bool:
        """A record copied from another session into this rollout."""
        payload = obj.get("payload")
        if obj.get("type") == "session_meta" and isinstance(payload, dict):
            return payload.get("id") != self.thread
        ordinal = obj.get("ordinal")
        return self.history_start is not None and isinstance(ordinal, int) \
            and ordinal < self.history_start


def _first_meta(path: str) -> "_Own | None":
    """The first record's session_meta identity, or None."""
    try:
        with open(path, "rb") as fh:
            raw = fh.readline()
    except OSError:
        return None
    if not raw.endswith(b"\n"):
        return None
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    payload = obj.get("payload") if isinstance(obj, dict) else None
    if obj.get("type") != "session_meta" or not isinstance(payload, dict) \
            or not isinstance(payload.get("id"), str) or not payload["id"]:
        return None
    return _Own(payload)


def _before_own_records(own: "_Own", obj: dict) -> bool:
    """Copied history, or a session_meta, which writes no row."""
    payload = obj.get("payload")
    if obj.get("type") == "session_meta" and isinstance(payload, dict):
        return True
    return own.inherited(obj)


def _history_file_boundary(path: str, own: "_Own") -> int | None:
    """Complete-line index of the first record that is not copied history.

    ``history_start`` is a metadata ordinal. Stored ``ordinal_num`` is the
    complete-line index on an append, a full replay, and a repair of rows
    imported before the fork boundary was known. The cutoff passed to
    ``_move_rows`` is that index. The rollout's own session_meta writes no
    row, so a low metadata ordinal on it is not the boundary.
    """
    if own.history_start is None:
        return None
    position = 0
    with open(path, "rb") as fh:
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            text = raw.decode("utf-8", "replace")
            if not text.strip():
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                return position
            if not isinstance(obj, dict) or not _before_own_records(own, obj):
                return position
            position += 1
    return position


def _move_rows(con: sqlite3.Connection, source_id: int, old: str, new: str,
               start: int | None, copies: bool) -> bool:
    """Move one rollout's rows from session `old` to session `new` (#69).

    ``start`` is the complete-line index where the fork's own records
    begin, from ``_history_file_boundary``. Without `copies`, rows at or
    after that index move from the parent's key to the fork's. With
    `copies`, rows before that index move from the fork's key to the
    parent's. A re-read never imports copied records again, so a copy may
    be the ledger's only evidence of the parent's work: rows are moved,
    never dropped, unless `new` already holds the same row (same event,
    or the same fallback usage total). Fallback snapshots carry their
    session key in their id ("<key>:tc:<total>") and are renamed with it.
    Overlap flags of both sessions are reset for the caller to recompute.
    Returns whether anything changed.
    """
    rows = "source_id=? AND session_key=?"
    args: list = [source_id, old]
    if start is not None:
        rows += " AND ordinal_num < ?" if copies else " AND ordinal_num >= ?"
        args.append(start)
    moved = 0
    moved += con.execute(
        f"UPDATE OR IGNORE events SET session_key=? WHERE {rows}",
        [new] + args).rowcount
    # Left behind only when `new` holds the same event.
    moved += con.execute(f"DELETE FROM events WHERE {rows}", args).rowcount
    prefix = old + ":tc:"
    fallback = f"{rows} AND substr(response_id, 1, ?) = ?"
    moved += con.execute(
        "UPDATE OR IGNORE responses SET session_key=?, response_id=?"
        f" || substr(response_id, ?) WHERE {fallback}",
        [new, new + ":tc:", len(prefix) + 1] + args
        + [len(prefix), prefix]).rowcount
    # Left behind only when `new` holds a snapshot at the same total.
    moved += con.execute(f"DELETE FROM responses WHERE {fallback}",
                         args + [len(prefix), prefix]).rowcount
    for table in ("responses", "user_messages"):
        moved += con.execute(
            f"UPDATE {table} SET session_key=? WHERE {rows}",
            [new] + args).rowcount
    occ = "source_id=? AND session_key=?"
    occ_args: list = [source_id, old]
    if start is not None:
        occ += " AND record_position < ?" if copies else " AND record_position >= ?"
        occ_args.append(start)
    prefix_len = len(prefix)
    con.execute(
        "UPDATE codex_occurrences SET session_key=?, row_key=? || substr(row_key, ?)"
        f" WHERE row_kind='response' AND substr(row_key, 1, ?)=? AND {occ}",
        [new, new + ":tc:", prefix_len + 1, prefix_len, prefix] + occ_args)
    con.execute(
        "UPDATE codex_occurrences SET session_key=?"
        f" WHERE NOT (row_kind='response' AND substr(row_key, 1, ?)=?) AND {occ}",
        [new, prefix_len, prefix] + occ_args)
    if not moved:
        return False
    # A turn follows its rows when none of them stayed behind.
    used = ("SELECT turn_id FROM events WHERE session_key={k}"
            " AND turn_id IS NOT NULL"
            " UNION SELECT turn_id FROM responses WHERE session_key={k}"
            " AND turn_id IS NOT NULL"
            " UNION SELECT turn_id FROM user_messages WHERE session_key={k}"
            " AND turn_id IS NOT NULL")
    con.execute(
        "UPDATE turns SET session_key=:new WHERE source_id=:src"
        " AND session_key=:old"
        f" AND turn_id IN ({used.format(k=':new')})"
        f" AND turn_id NOT IN ({used.format(k=':old')})",
        {"new": new, "old": old, "src": source_id})
    # First-seen fallback signatures that moved with their snapshots.
    con.execute(
        f"DELETE FROM {_CUMULATIVE_TABLE} WHERE session_key=?"
        " AND thread_total IN (SELECT thread_total_tokens FROM responses"
        " WHERE session_key=? AND source_id=? AND semantics=?)"
        " AND thread_total NOT IN (SELECT thread_total_tokens FROM responses"
        " WHERE session_key=? AND thread_total_tokens IS NOT NULL)",
        (old, new, source_id, TOKEN_COUNT_SEMANTICS, old))
    con.execute(
        "UPDATE responses SET is_overlap=0 WHERE session_key IN (?, ?)"
        " AND semantics=?", (old, new, TOKEN_COUNT_SEMANTICS))
    return True


def _settle(con: sqlite3.Connection, key: str, native_id: str,
            lost_rows: bool) -> None:
    """Another session after rows moved: its overlap flags and a row if it
    had none; when rows left it, its end time from what remains."""
    _reconcile_fallback(con, key)
    db.upsert_session(con, key, HARNESS, native_id)
    if not lost_rows:
        return
    con.execute(
        "UPDATE sessions SET ended_at=(SELECT MAX(ts) FROM ("
        " SELECT ts FROM events WHERE session_key=?1"
        " UNION ALL SELECT ts FROM responses WHERE session_key=?1"
        " UNION ALL SELECT ts FROM user_messages WHERE session_key=?1))"
        " WHERE session_key=?1 AND EXISTS (SELECT 1 FROM events"
        " WHERE session_key=?1)", (key,))


def _prescan_thread_meta(path: str, start_offset: int,
                         own: "_Own | None" = None) -> dict:
    """Own-thread identity in the not-yet-imported file portion.

    Reads complete lines from start_offset (the same framing as
    JsonlSource.records) and returns the first session_id, thread_id and
    thread_source plus every observed own-thread identity, in file order.
    A user message that precedes the native thread identity is therefore
    judged with that identity already known. Referenced worker threads
    (spawn targets) are not own-thread identities and are ignored.
    """
    first_session = None
    first_thread = None
    first_source = None
    observed: list = []

    def _note_thread(value) -> None:
        if value is not None and value not in observed:
            observed.append(value)

    try:
        with open(path, "rb") as fh:
            fh.seek(start_offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break
                text = raw.decode("utf-8", "replace")
                if not text.strip():
                    continue
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                if own is not None and own.inherited(obj):
                    continue
                if obj.get("type") == "session_meta":
                    if first_session is None:
                        first_session = (payload.get("session_id")
                                         or payload.get("id"))
                    if first_thread is None and payload.get("id"):
                        first_thread = payload["id"]
                    if first_source is None and payload.get("thread_source"):
                        first_source = payload["thread_source"]
                    if payload.get("id") is not None:
                        _note_thread(payload["id"])
                if payload.get("thread_id") is not None:
                    _note_thread(payload["thread_id"])
                    if first_thread is None:
                        first_thread = payload["thread_id"]
                if payload.get("session_id") is not None and \
                        first_session is None:
                    first_session = payload["session_id"]
    except OSError:
        pass
    return {"session_id": first_session, "thread_id": first_thread,
            "thread_source": first_source, "observed_thread_ids": observed}


def import_codex_file(con: sqlite3.Connection, path: str,
                      full: bool = False) -> dict:
    """Import one Codex rollout. Idempotent; growing logs update in place.

    Malformed lines are recorded in import_errors and skipped. Previously
    valid data is never destroyed.
    """
    stats = {
        "lines": 0, "responses_inserted": 0, "responses_duplicate": 0,
        "user_messages_inserted": 0, "events_inserted": 0,
        "events_duplicate": 0, "compactions": 0, "malformed": 0,
    }
    src = JsonlSource(con, HARNESS, path, full=full)
    if (src.row["codex_context_version"] != 3
            or src.row["codex_context_offset"] != src.row["read_offset"]):
        # Replay older indexes once, including search extensions previously
        # discarded from otherwise unchanged sources (#94).
        src.start_offset = 0
        src.end_offset = 0
        src.unchanged = False
    if not _occurrences_ready(src.row):
        # Occurrence backfill is not privacy. A source already at the
        # current privacy version is still read once from the start so
        # each file can record its own positions.
        src.start_offset = 0
        src.end_offset = 0
        src.unchanged = False
    if src.unchanged and src.recheck_unchanged():
        # Unchanged fast path: same size, mtime, inode and tail bytes at
        # the recorded offset, same privacy version, not full, no new
        # complete records. The recheck re-stats immediately before
        # returning so an append (or same-size rewrite) racing the first
        # check falls through to the import path below with the corrected
        # offset instead of skipping new bytes. Skip every per-source SQL
        # beyond JsonlSource's stat/tail check: no cumulative table, no
        # reader, no prescan, no prior-event query, no JSON parsing, no
        # token flush, no fallback reconciliation, no submission or
        # session writes, and no finish() source bookkeeping. The stored
        # fingerprint is the sha256 contract existing tests compare.
        try:
            stored_sha = src.row["sha256"]
            stored_ordinal = src.row["ordinal_max"]
            sess_id = src.row["session_id"]
            thr_id = src.row["thread_id"]
        except (KeyError, TypeError, IndexError):
            stored_sha = ""
            stored_ordinal = -1
            sess_id = None
            thr_id = None
        native = thr_id or sess_id
        if native:
            session_key = f"{HARNESS}:{native}"
        else:
            session_key = f"{HARNESS}:file:{os.path.basename(path)}"
        stats.update({
            "source_id": src.source_id,
            "sha256": stored_sha,
            "ordinal_max": stored_ordinal,
            "incremental": src.incremental,
            "unchanged": True,
            "session_key": session_key,
        })
        return stats
    reader = _Reader(con, src, stats)
    _ensure_cumulative_table(con)
    own = _first_meta(path)
    boundary = _history_file_boundary(path, own) if own is not None else None
    # Privacy 9 is the writer that stores file positions. A ledger
    # imported under an older version still holds raw metadata ordinals,
    # so the move uses that cutoff before replay. A current ledger, and
    # any later privacy version, already holds file positions.
    stored_privacy = (src.row["privacy_version"]
                      if src.row is not None and "privacy_version" in src.row.keys()
                      else None)
    metadata_namespace = (isinstance(stored_privacy, int)
                          and not isinstance(stored_privacy, bool)
                          and stored_privacy < 9)
    reader.metadata_namespace = metadata_namespace
    move_at = own.history_start if own is not None and metadata_namespace \
        else boundary
    settle: dict = {}
    if own is not None:
        stored = src.row["thread_id"]
        reader.own = own
        reader.thread_id = own.thread
        reader.session_id = own.session
        reader.thread_source = own.source or reader.thread_source
        reader.observed_threads = {own.thread}
        if isinstance(stored, str) and stored and stored != own.thread:
            # Before #69 a fork was keyed by the copied parent session_meta.
            old = f"{HARNESS}:{stored}"
            if _move_rows(con, src.source_id, old, reader.session_key,
                          move_at, copies=False):
                settle[old] = (stored, True)
            # The parent's row may point at this rollout as its source.
            con.execute(
                "UPDATE sessions SET source_id=(SELECT id FROM sources"
                " WHERE harness=? AND thread_id=? AND id!=? ORDER BY id"
                " LIMIT 1) WHERE session_key=? AND source_id=?",
                (HARNESS, stored, src.source_id, old, src.source_id))
        if own.history_start is not None and own.parent:
            # Copied history an earlier import stored in this session.
            parent = f"{HARNESS}:{own.parent}"
            if _move_rows(con, src.source_id, reader.session_key, parent,
                          move_at, copies=True):
                settle.setdefault(parent, (own.parent, False))
    # Seed the rollout's own thread identity before any row is written, so
    # a genuine user message keeps its excerpt only when the whole new
    # portion (plus persisted metadata) already proves a human main
    # session, and the session key is stable from the first insert.
    pre = _prescan_thread_meta(path, src.start_offset, own)
    if reader.session_id is None:
        reader.session_id = pre["session_id"]
    if reader.thread_id is None:
        reader.thread_id = pre["thread_id"]
    if reader.thread_source is None:
        reader.thread_source = pre["thread_source"]
    reader.observed_threads.update(pre["observed_thread_ids"])
    # Ordering guard for the deferred token_count flush, snapshotted before
    # any row of this import is written. The start ordinal mirrors
    # JsonlSource.records: an incremental import resumes after the previous
    # max, a full re-read starts at zero. The ceiling marks every event that
    # already existed, so the flush can tell prior-prefix compaction evidence
    # apart from compaction events this import is about to write.
    reader.import_start_ordinal = \
        (src.row["ordinal_max"] + 1) if src.incremental else 0
    reader.prior_event_ceiling = con.execute(
        "SELECT MAX(id) FROM events").fetchone()[0]
    if src.start_offset == 0:
        con.execute("DELETE FROM codex_occurrences WHERE source_id=?",
                    (src.source_id,))
    for ordinal, obj, line in src.records():
        stats["lines"] += 1
        if obj is None or not isinstance(obj, dict):
            stats["malformed"] += 1
            src.error(ordinal,
                      "malformed_json" if obj is None else "unknown_record",
                      line)
            continue
        if own is not None and own.inherited(obj):
            stats["inherited"] = stats.get("inherited", 0) + 1
            _note_inherited(reader, obj, ordinal)
            continue
        try:
            _ingest_record(reader, obj, ordinal)
        except _UsageConflict:
            stats["malformed"] += 1
            src.error(ordinal, "usage_conflict", line)
            continue
        except _MalformedUsage:
            stats["malformed"] += 1
            src.error(ordinal, "malformed_usage", line)
            continue
        except MissingNativeId:
            stats["malformed"] += 1
            src.error(ordinal, "missing_id", line)
            continue
        except _UnsupportedSchema:
            stats["malformed"] += 1
            src.error(ordinal, "unsupported_schema", line)
            continue
        except (KeyError, TypeError, ValueError, AttributeError):
            stats["malformed"] += 1
            src.error(ordinal, "schema_error", line)
            continue
        ts = iso_ts(obj.get("timestamp"))
        if ts is not None:
            reader.first_ts = ts if reader.first_ts is None else min(reader.first_ts, ts)
            reader.last_ts = ts if reader.last_ts is None else max(reader.last_ts, ts)
    _flush_token_counts(reader)
    if not src.privacy_stale:
        # Same-version imports reconcile this source's submissions with the
        # final rollout identity: rows stored before the native thread
        # identity arrived carry a stale session key, and a rollout that
        # proves to be a child, worker or unknown source keeps no
        # excerpts. Clearing needs no native text, only stores less. Stale
        # privacy-version re-imports already correct every row in place
        # above, so they skip this net.
        # A fork's copied history (#69) belongs to another session.
        own_part, own_args = "", []
        if own is not None and own.history_start is not None:
            own_part = " AND (ordinal_num IS NULL OR ordinal_num >= ?)"
            own_args = [boundary]
        key_fix = con.execute(
            "UPDATE user_messages SET session_key=? WHERE source_id=?"
            " AND session_key!=?" + own_part,
            [reader.session_key, src.source_id, reader.session_key]
            + own_args)
        if key_fix.rowcount:
            stats["user_messages_updated"] = \
                stats.get("user_messages_updated", 0) + key_fix.rowcount
        if not reader.is_main:
            cleared = con.execute(
                "UPDATE user_messages SET text_excerpt='' WHERE source_id=?"
                " AND text_excerpt!=''" + own_part,
                [src.source_id] + own_args)
            if cleared.rowcount:
                stats["user_messages_updated"] = \
                    stats.get("user_messages_updated", 0) + cleared.rowcount
    fields = {"started_at": reader.first_ts, "ended_at": reader.last_ts,
              **reader.meta, **reader.identity.fields(con)}
    if own is not None and own.parent:
        fields["parent_session_key"] = f"{HARNESS}:{own.parent}"
    db.upsert_session(con, reader.session_key, HARNESS,
                      reader.session_key.split(":", 1)[1], src.source_id,
                      **fields)
    stats.update(src.finish(session_id=reader.session_id,
                            thread_id=reader.thread_id,
                            cli_version=reader.cli_version,
                            thread_source=reader.thread_source))
    backfill = 1 if _backfill_complete(
        con, src.source_id, getattr(src, "last_ordinal", None)) else 0
    con.execute("UPDATE sources SET codex_context_version=3,codex_context_offset=?,"
                "codex_model=?,codex_effort=?,occurrence_backfill=? WHERE id=?",
                (src.end_offset, reader.model, reader.effort, backfill,
                 src.source_id))
    for key, (native, lost_rows) in settle.items():
        _settle(con, key, native, lost_rows)
    if settle:
        stats["moved_rows_with"] = sorted(settle)
    stats["session_key"] = reader.session_key
    con.commit()
    return stats


def _note_inherited(r: _Reader, obj: dict, position: int) -> None:
    """Adopt retained rows for one copied record this replay does not insert.

    The match is the native id that record stored. A raw metadata ordinal
    can belong to several explicit ids and can be missing, so it is not an
    identity. Rows keep their identity and session. A ledger imported
    before privacy 9 still holds metadata ordinals, and those locators
    become file positions. A later privacy refresh already holds file
    positions and does not read them as metadata ordinals. Every matched
    event, user, and response is recorded, including derived reads and
    fallback usage from that record. A copy that matches nothing stays
    unknown instead of becoming a new identity.
    """
    r.record_position = position
    rewrite = position if r.metadata_namespace else None
    seen = getattr(r, "adopted_rows", None)
    if seen is None:
        seen = set()
        r.adopted_rows = seen
    for spec in _copied_identities(r, obj, position):
        kind = "response" if spec[0] == "fallback" else spec[0]
        for row in _rows_for_copied(r, spec):
            marker = (kind, row["key"])
            if marker in seen:
                continue
            seen.add(marker)
            if rewrite is not None:
                _rewrite_copied_locator(r, kind, row["key"], rewrite)
            _note_occurrence(
                r, kind, row["key"], obj, position=position,
                model=r.model if kind == "response" else None,
                effort=r.effort if kind == "response" else None,
                session_key=row["session_key"])


def _copied_identities(r: _Reader, obj: dict, position: int):
    """Native rows one copied record would already have stored.

    These are the same ids ingest uses. The raw ordinal is only the
    id-less legacy form, never a query for every row at that number.
    """
    payload = obj.get("payload") if isinstance(obj, dict) else None
    if not isinstance(payload, dict):
        return
    rtype = obj.get("type")
    raw = _native_ordinal(obj)
    if rtype == "token_usage_record":
        rid = payload.get("response_id")
        if isinstance(rid, str) and rid:
            yield ("response", f"{HARNESS}:{rid}")
        return
    if rtype == "compacted":
        if "window_id" not in payload:
            yield ("event", "compaction", f"compacted:{obj.get('ordinal')}")
        elif isinstance(payload.get("window_id"), str) and payload.get("window_id"):
            yield ("event", "compaction", payload["window_id"])
        return
    if rtype == "response_item":
        yield from _copied_response_item(r, payload, raw, position)
        return
    if rtype == "event_msg":
        yield from _copied_event_msg(r, obj, payload, position)


def _backfill_complete(con, source_id: int, ceiling) -> bool:
    """Every retained locator for this source is inside the file.

    A metadata ordinal left outside the file means a copied row was not
    adopted. The marker stays unset so the next read tries again.
    """
    if isinstance(ceiling, bool) or not isinstance(ceiling, int):
        return True
    row = con.execute(
        "SELECT 1 FROM events WHERE source_id=? AND ordinal_num>?"
        " UNION ALL SELECT 1 FROM user_messages WHERE source_id=?"
        " AND ordinal_num>?"
        " UNION ALL SELECT 1 FROM responses WHERE source_id=?"
        " AND ordinal_num>? LIMIT 1",
        (source_id, ceiling, source_id, ceiling, source_id, ceiling)).fetchone()
    return row is None


def _copied_user_ids(r: _Reader, payload: dict, raw, position: int):
    native = payload.get("id")
    if isinstance(native, str) and native:
        yield f"{HARNESS}:{native}"
        return
    if raw is not None:
        yield f"{HARNESS}:ordinal:{raw}"
    fallback = _fallback_native_id(r.src.path, position)
    threads = [r.session_key.split(":", 1)[1]]
    if r.own is not None and r.own.parent and r.own.parent not in threads:
        threads.append(r.own.parent)
    for thread in threads:
        yield f"{HARNESS}:{thread}:{fallback}"


def _copied_response_item(r: _Reader, payload: dict, raw, position: int):
    ptype = payload.get("type")
    if ptype == "message" and payload.get("role") == "user":
        for native in _copied_user_ids(r, payload, raw, position):
            yield ("user", native)
        return
    if ptype == "message" and payload.get("role") == "assistant":
        if not _text_of_message(payload):
            return
        native = payload.get("id")
        if isinstance(native, str) and native:
            yield ("event", "assistant_message", native)
        else:
            if raw is not None:
                yield ("event", "assistant_message", f"ordinal:{raw}")
            yield ("event", "assistant_message",
                   _fallback_native_id(r.src.path, position))
        return
    if ptype in ("function_call", "custom_tool_call",
                 "function_call_output", "custom_tool_call_output"):
        native = payload.get("call_id") or payload.get("id")
        if isinstance(native, str) and native:
            family = "tool_call" if ptype in (
                "function_call", "custom_tool_call") else "tool_result"
            yield ("event", family, native)


def _copied_event_msg(r: _Reader, obj: dict, payload: dict, position: int):
    etype = payload.get("type")
    if etype == "task_complete":
        yield ("event", "lifecycle", f"task_complete:{payload.get('turn_id')}")
        return
    if etype == "thread_goal_updated":
        yield ("event", "lifecycle",
               f"thread_goal_updated:{obj.get('ordinal')}")
        return
    if etype == "turn_aborted":
        if payload.get("turn_id"):
            yield ("event", "lifecycle", f"turn_aborted:{payload['turn_id']}")
        else:
            yield ("event", "lifecycle", "turn_aborted:None")
            yield ("event", "lifecycle",
                   "turn_aborted:" + _fallback_native_id(r.src.path, position))
        return
    if etype == "token_count":
        info = payload.get("info")
        total_raw = info.get("total_token_usage") if isinstance(info, dict) else None
        total = total_raw.get("total_tokens") if isinstance(total_raw, dict) else None
        if isinstance(total, int) and not isinstance(total, bool):
            yield ("fallback", total)
        return
    if etype != "item_completed":
        return
    item = payload.get("item")
    if isinstance(item, dict):
        yield from _copied_item(obj, item)


def _copied_item(obj: dict, item: dict):
    """Same native ids as the completed-item ingest, including derived reads."""
    itype = item.get("type")
    if itype == "CommandExecution":
        native = item.get("id")
        if not isinstance(native, str) or not native:
            return
        yield ("event", "tool_result", native)
        for entry in item.get("parsed_cmd") or []:
            if not isinstance(entry, dict) or entry.get("type") != "read":
                continue
            target = entry.get("path")
            if not isinstance(target, str) or not target:
                target = entry.get("name")
            if not isinstance(target, str) or not target:
                continue
            raw_skill = skill_from_path(target)
            safe_skill = privacy.filter_target(raw_skill, family="skill_read") \
                if raw_skill else None
            looks_like_skill = target.endswith("SKILL.md") or "/skills/" in target
            family = "skill_read" if (safe_skill or looks_like_skill) else "read"
            yield ("event", family, f"{native}:{target}")
        return
    if itype == "Extension":
        if item.get("kind") == "web.search":
            native = item.get("call_id") or item.get("id")
            if isinstance(native, str) and native:
                yield ("event", "tool_result", native)
        return
    if itype in ("UserMessage", "AgentMessage", "Reasoning"):
        return
    if itype == "McpToolCall":
        native = item.get("id")
        family = "tool_result"
    elif itype == "FileChange":
        native = item.get("id")
        family = "file_change"
    elif itype == "ContextCompaction":
        native = item.get("id")
        family = "compaction"
    elif itype in ("SubAgentActivity", "CollabAgentToolCall"):
        native = item.get("id")
        family = "lifecycle"
    elif itype in ("ImageView", "WebSearch", "DynamicToolCall",
                   "FunctionCallOutput"):
        native = item.get("call_id") or item.get("id")
        family = "tool_result"
    elif itype in ("Plan", "HookPrompt", "EnteredReviewMode", "ExitedReviewMode"):
        native = item.get("id") or f"{itype}:{obj.get('ordinal')}"
        family = "lifecycle"
    else:
        return
    if isinstance(native, str) and native:
        yield ("event", family, native)


def _rows_for_copied(r: _Reader, spec):
    kind = spec[0]
    if kind == "user":
        return r.con.execute(
            "SELECT native_id AS key, session_key FROM user_messages"
            " WHERE source_id=? AND native_id=?",
            (r.src.source_id, spec[1])).fetchall()
    if kind == "event":
        return r.con.execute(
            "SELECT id AS key, session_key FROM events"
            " WHERE source_id=? AND family=? AND native_id=?",
            (r.src.source_id, spec[1], spec[2])).fetchall()
    if kind == "response":
        return r.con.execute(
            "SELECT response_id AS key, session_key FROM responses"
            " WHERE source_id=? AND response_id=?",
            (r.src.source_id, spec[1])).fetchall()
    if kind != "fallback":
        return []
    # The move renames a copied snapshot onto the parent session. Prefer
    # that id so a child snapshot with the same total stays where it is.
    total = spec[1]
    candidates = []
    if r.own is not None and r.own.parent:
        candidates.append(f"{HARNESS}:{r.own.parent}:tc:{total}")
    candidates.append(f"{r.session_key}:tc:{total}")
    for rid in candidates:
        row = r.con.execute(
            "SELECT response_id AS key, session_key FROM responses"
            " WHERE source_id=? AND response_id=?",
            (r.src.source_id, rid)).fetchone()
        if row is not None:
            return [row]
    return []


def _rewrite_copied_locator(r: _Reader, kind: str, key, position: int) -> None:
    if kind == "user":
        r.con.execute(
            "UPDATE user_messages SET ordinal_num=? WHERE source_id=?"
            " AND native_id=?",
            (position, r.src.source_id, key))
    elif kind == "event":
        r.con.execute(
            "UPDATE events SET ordinal_num=? WHERE source_id=? AND id=?",
            (position, r.src.source_id, key))
    else:
        r.con.execute(
            "UPDATE responses SET ordinal_num=? WHERE source_id=?"
            " AND response_id=?",
            (position, r.src.source_id, key))


def _ingest_record(r: _Reader, obj: dict, ordinal: int) -> None:
    # record_position is the one chronology for every Codex row. The raw
    # metadata ordinal stays on obj and is the only ordinal copied into
    # native ids or legacy identity (ordinal:N).
    native_ordinal = obj.get("ordinal")
    r.record_position = ordinal
    rtype = obj.get("type")
    if rtype not in ("session_meta", "event_msg", "response_item",
                     "token_usage_record", "turn_context", "compacted",
                     "world_state", "inter_agent_communication_metadata"):
        raise _UnsupportedSchema(f"unsupported record type: {rtype!r}")
    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError(f"unsupported payload shape for {rtype}: "
                         f"{type(payload).__name__}")
    if rtype == "session_meta":
        if r.own is not None and payload.get("id") != r.own.thread:
            return
        r.session_id = payload.get("session_id") or payload.get("id") or r.session_id
        r.thread_id = payload.get("id") or r.thread_id
        r.thread_source = payload.get("thread_source") or r.thread_source
        if payload.get("id") is not None and payload.get("id") not in r.observed_threads:
            r.observed_threads.add(payload["id"])
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
        if payload.get("thread_id") is not None and \
                payload["thread_id"] not in r.observed_threads:
            r.observed_threads.add(payload["thread_id"])
        if payload.get("thread_id") and r.thread_id is None:
            r.thread_id = payload["thread_id"]
        if payload.get("session_id") and r.session_id is None:
            r.session_id = payload["session_id"]
    if rtype == "token_usage_record":
        _ingest_usage(r, obj)
    elif rtype == "response_item":
        _ingest_response_item(r, obj, native_ordinal, ordinal)
    elif rtype == "turn_context":
        _ingest_turn(r, obj)
    elif rtype == "event_msg":
        _ingest_event_msg(r, obj, ordinal)
    elif rtype == "compacted":
        _ingest_compacted(r, obj)
    # world_state and inter_agent_communication_metadata carry no ledger rows
    # beyond source metadata; they stay private in the raw file.


def _reconcile_fallback(con: sqlite3.Connection, session_key: str) -> None:
    """Mark only genuinely reconciled fallback usage snapshots as overlap.

    A legacy cumulative usage snapshot is covered when an authoritative
    response's own thread span (thread_total minus its own total up to
    thread_total) contains that cumulative point: the authoritative record
    then accounts for the same native work. Usage snapshots outside every
    authoritative span stay counted, so partial transitions never undercount
    and no representation is ever counted twice. The rows stay as evidence.
    """
    con.execute(
        "UPDATE responses SET is_overlap=1 WHERE session_key=?"
        " AND semantics=? AND is_overlap=0"
        " AND thread_total_tokens IS NOT NULL"
        " AND EXISTS (SELECT 1 FROM responses auth"
        " WHERE auth.session_key=responses.session_key"
        " AND auth.semantics=? AND auth.is_overlap=0"
        " AND auth.thread_total_tokens IS NOT NULL"
        " AND auth.total_tokens IS NOT NULL"
        " AND auth.thread_total_tokens - auth.total_tokens"
        " < responses.thread_total_tokens"
        " AND responses.thread_total_tokens <= auth.thread_total_tokens)",
        (session_key, TOKEN_COUNT_SEMANTICS, SEMANTICS))


def _cumulative_sig(total_raw: dict) -> tuple:
    """The cumulative accounting bucket of one fallback usage snapshot.

    Only the six known usage counters identify the cumulative point; extra
    native metadata (context window, rate-limit snapshots) never does, so
    differing metadata alone can never look like an accounting change.
    """
    return tuple((key, total_raw.get(key)) for key in _CODEX_BUCKET_KEYS)


def _fallback_want(last: dict, total: int) -> dict:
    return {
        "input_tokens": last.get("input_tokens"),
        "cached_input_tokens": last.get("cached_input_tokens"),
        "cache_write_input_tokens": last.get("cache_write_input_tokens"),
        "output_tokens": last.get("output_tokens"),
        "reasoning_output_tokens": last.get("reasoning_output_tokens"),
        "total_tokens": last.get("total_tokens"),
        "thread_total_tokens": total,
    }


def _migrate_response_position(r: _Reader, rid: str, position: int) -> None:
    """Adopt a file position only from the first sight in this source.

    A later exact repeat leaves that index. Another file records its own
    occurrence and does not move this row.
    """
    if not r.src.privacy_stale or rid in r.usage_context_seen:
        return
    row = r.con.execute(
        "SELECT source_id FROM responses WHERE response_id=?", (rid,)).fetchone()
    if row is None or row["source_id"] != r.src.source_id:
        return
    r.con.execute(
        "UPDATE responses SET ordinal_num=? WHERE response_id=? AND source_id=?",
        (position, rid, r.src.source_id))


def _repair_usage_context(r: _Reader, rid: str, obj: dict, model, effort,
                          position: int) -> None:
    """Only the original record in a full source replay can repair context.

    Repeated counters after a model switch are not new usage. A copied source
    cannot establish the original record's context either. Exact replay may
    restore unknown values that an older reader incorrectly inferred.
    """
    first = rid not in r.usage_context_seen
    r.usage_context_seen.add(rid)
    if first and r.src.start_offset == 0:
        r.con.execute(
            "UPDATE responses SET model=?,effort=? WHERE response_id=?"
            " AND source_id=? AND ordinal_num IS ? AND ts IS ?",
            (model, effort, rid, r.src.source_id, position,
             iso_ts(obj.get("timestamp"))))


def _flush_token_counts(r: _Reader) -> None:
    """Count token_count usage snapshots for rollouts without full usage cover.

    Each rising cumulative total is one new response keyed by that total, so
    appended logs, copied snapshots and full reimports all converge. Every
    duplicate fallback key is compared across all fallback counters: an exact
    repeat is a no-op and any difference is quarantined without overwriting,
    with one narrow exception. A repeated usage snapshot with the same cumulative
    identity that arrives after a compaction boundary, and whose cumulative
    bucket repeats this import's first usage snapshot for that total while only
    last-token or rate-limit metadata changed, is duplicate compaction
    evidence: it is ignored without inserting another response, changing the
    stored row, or recording an error. Pre-compaction repeats with changed
    counters, and post-compaction repeats with a changed cumulative bucket,
    stay quarantined as usage conflicts. Malformed buckets are quarantined
    before any SQL and later valid records still import. Rising usage snapshots
    are always preserved as evidence, wherever they appear; reconciliation
    then marks only the usage snapshots an authoritative span genuinely covers
    as overlap."""

    if r.token_counts:
        row = r.con.execute(
            "SELECT MAX(thread_total_tokens) t FROM responses"
            " WHERE session_key=? AND semantics=?",
            (r.session_key, TOKEN_COUNT_SEMANTICS)).fetchone()
        # The ledger's highest total seeds only an incremental resume. A
        # full re-read walks the whole file in order, as a fresh import
        # does, so it also restores snapshots an earlier import skipped.
        previous = (row["t"] or 0) if r.src.incremental else 0
        # First cumulative bucket per total seen in this import, in file
        # order. The map is seeded from the persisted first-seen
        # signatures of earlier imports, so an incremental suffix that
        # begins with a post-compaction repeat compares against the same
        # reference as a full import. A repeat can only be compaction
        # evidence when the total's first usage snapshot is already known;
        # a lone repeat with no prior signature fails closed into the
        # usage_conflict path below.
        first_cumulative: dict = _load_cumulative_sigs(r.con, r.session_key)
        prior_compaction = None

        def _post_compaction(captured: bool) -> bool:
            """Whether a compaction boundary precedes this usage snapshot.

            The flag captured at collection covers this import in file order.
            The ledger covers earlier incremental prefixes only: the row-id
            ceiling excludes compaction events this import wrote (including
            ones positioned later in the same file), and the ordinal bound
            excludes anything not from before the imported prefix. A
            same-total conflict before a later compaction therefore still
            quarantines; a repeat after an earlier-prefix boundary does not.
            """
            nonlocal prior_compaction
            if captured:
                return True
            if prior_compaction is None:
                prior_compaction = False
                if r.prior_event_ceiling is not None:
                    hit = r.con.execute(
                        "SELECT 1 FROM events WHERE session_key=?"
                        " AND source_id=? AND family='compaction' AND id <= ?"
                        " AND ordinal_num IS NOT NULL"
                        " AND ordinal_num < ? LIMIT 1",
                        (r.session_key, r.src.source_id, r.prior_event_ceiling,
                         r.import_start_ordinal)).fetchone()
                    prior_compaction = hit is not None
            return prior_compaction

        for struct_ordinal, obj, info, compacted_before, model, effort in r.token_counts:
            ordinal = struct_ordinal
            try:
                if not isinstance(info, dict):
                    raise _MalformedUsage("malformed usage")
                last_raw = info.get("last_token_usage")
                total_raw = info.get("total_token_usage")
                if last_raw is None or total_raw is None:
                    # A usage snapshot without both usages is malformed: quarantine
                    # before any SQL and continue with later usage snapshots.
                    raise _MalformedUsage("malformed usage")
                _validate_bucket(last_raw)
                _validate_bucket(total_raw)
                if not isinstance(last_raw, dict) or not isinstance(
                        total_raw, dict):
                    raise _MalformedUsage("malformed usage")
                total = total_raw.get("total_tokens")
                if not _valid_counter(total) or total is None:
                    raise _MalformedUsage("malformed usage counter")
                if not isinstance(total, int) or isinstance(total, bool):
                    raise _MalformedUsage("malformed usage counter")
                rid = f"{r.session_key}:tc:{total}"
                sig = _cumulative_sig(total_raw)
                first = first_cumulative.get(total)
                if total not in first_cumulative:
                    first_cumulative[total] = sig
                    _remember_cumulative_sig(r, total, total_raw)
                existing = r.con.execute(
                    "SELECT input_tokens, cached_input_tokens,"
                    " cache_write_input_tokens, output_tokens,"
                    " reasoning_output_tokens, total_tokens,"
                    " thread_total_tokens FROM responses"
                    " WHERE response_id=?", (rid,)).fetchone()
                if existing is not None:
                    # The rising-total shortcut never bypasses comparison:
                    # every duplicate key is validated across all fallback
                    # counters.
                    seen = dict(existing)
                    want = _fallback_want(last_raw, total)
                    if seen != want:
                        if first is not None and first == sig \
                                and _post_compaction(compacted_before):
                            # Post-compaction duplicate evidence: no new
                            # response, no row change, no error.
                            r.stats["responses_duplicate"] += 1
                            if total > previous:
                                previous = total
                            continue
                        raise _UsageConflict("usage conflict")
                    _migrate_response_position(r, rid, ordinal)
                    _repair_usage_context(r, rid, obj, model, effort, ordinal)
                    _note_occurrence(r, "response", rid, obj, position=ordinal,
                                     model=model, effort=effort)
                    r.stats["responses_duplicate"] += 1
                    if total > previous:
                        previous = total
                    continue
                if total <= previous:
                    continue
                previous = total
                cur = r.con.execute(
                    "INSERT OR IGNORE INTO responses(response_id, source_id, harness,"
                    " session_key, thread_id, ordinal_num, ts, model, effort, input_tokens,"
                    " cached_input_tokens, cache_write_input_tokens, output_tokens,"
                    " reasoning_output_tokens, total_tokens, thread_total_tokens, semantics)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, r.src.source_id, HARNESS,
                     r.session_key, r.thread_id, ordinal, iso_ts(obj.get("timestamp")),
                     model, effort, last_raw.get("input_tokens"), last_raw.get("cached_input_tokens"),
                     last_raw.get("cache_write_input_tokens"), last_raw.get("output_tokens"),
                     last_raw.get("reasoning_output_tokens"), last_raw.get("total_tokens"), total,
                     TOKEN_COUNT_SEMANTICS))
                if cur.rowcount:
                    r.usage_context_seen.add(rid)
                    r.stats["responses_inserted"] += 1
                    _note_occurrence(r, "response", rid, obj, position=ordinal,
                                     model=model, effort=effort)
                else:
                    # Lost a race with an existing row: compare before counting.
                    # The same post-compaction duplicate-evidence rule applies
                    # as above, so a copied snapshot converging here stays
                    # silent while genuine mismatches still quarantine.
                    existing = r.con.execute(
                        "SELECT input_tokens, cached_input_tokens,"
                        " cache_write_input_tokens, output_tokens,"
                        " reasoning_output_tokens, total_tokens,"
                        " thread_total_tokens FROM responses"
                        " WHERE response_id=?", (rid,)).fetchone()
                    seen = dict(existing) if existing is not None else {}
                    want = _fallback_want(last_raw, total)
                    if seen != want:
                        if first is not None and first == sig \
                                and _post_compaction(compacted_before):
                            r.stats["responses_duplicate"] += 1
                            continue
                        raise _UsageConflict("usage conflict")
                    _note_occurrence(r, "response", rid, obj, position=ordinal,
                                     model=model, effort=effort)
                    r.stats["responses_duplicate"] += 1
            except _UsageConflict:
                r.stats["malformed"] += 1
                r.src.error(ordinal, "usage_conflict", _deferred_line(obj))
                continue
            except _MalformedUsage:
                r.stats["malformed"] += 1
                r.src.error(ordinal, "malformed_usage", _deferred_line(obj))
                continue
            except (KeyError, TypeError, ValueError, AttributeError):
                r.stats["malformed"] += 1
                r.src.error(ordinal, "schema_error", _deferred_line(obj))
                continue
        r.token_counts = []
    # Cheap guards before the correlated reconciliation UPDATE: when the
    # session holds no fallback usage snapshots or no authoritative responses,
    # the UPDATE could match nothing and its full scan is pure overhead.
    # Both checks are indexed point lookups through idx_responses_reconcile.
    # Changed/full imports that actually need reconciliation (both sides
    # present) still run it; the unchanged fast path above never reaches
    # here at all.
    try:
        has_fallback = r.con.execute(
            "SELECT 1 FROM responses WHERE session_key=? AND semantics=?"
            " LIMIT 1",
            (r.session_key, TOKEN_COUNT_SEMANTICS)).fetchone() is not None
    except sqlite3.DatabaseError:
        has_fallback = True
    if not has_fallback:
        return
    try:
        has_auth = r.con.execute(
            "SELECT 1 FROM responses WHERE session_key=? AND semantics=?"
            " LIMIT 1",
            (r.session_key, SEMANTICS)).fetchone() is not None
    except sqlite3.DatabaseError:
        has_auth = True
    if not has_auth:
        return
    _reconcile_fallback(r.con, r.session_key)


def _ingest_usage(r: _Reader, obj: dict) -> None:
    p = obj["payload"]
    for k in ("response_id", "usage"):
        if k not in p:
            raise ValueError(f"token_usage_record missing {k}")
    # No falsey fallbacks: None/missing stays unknown, any other non-dict
    # shape (including falsey lists, strings or numbers) is malformed and
    # quarantined before any SQL.
    u_raw = p.get("usage")
    tt_raw = p.get("turn_token_usage")
    th_raw = p.get("thread_token_usage")
    _validate_bucket(u_raw)
    _validate_bucket(tt_raw)
    _validate_bucket(th_raw)
    u = u_raw if isinstance(u_raw, dict) else {}
    tt = tt_raw if isinstance(tt_raw, dict) else {}
    th = th_raw if isinstance(th_raw, dict) else {}
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
         r.record_position, iso_ts(obj.get("timestamp")), r.model, r.effort,
         u.get("input_tokens"), u.get("cached_input_tokens"),
         u.get("cache_write_input_tokens"), u.get("output_tokens"),
         u.get("reasoning_output_tokens"), u.get("total_tokens"),
         tt.get("total_tokens"), th.get("total_tokens"), SEMANTICS))
    if cur.rowcount == 0:
        # Same response_id seen again: every immutable counter must agree.
        # An exact repeat is a no-op; any mismatch is quarantined and later
        # valid records still import.
        existing = r.con.execute(
            "SELECT input_tokens, cached_input_tokens,"
            " cache_write_input_tokens, output_tokens, reasoning_output_tokens,"
            " total_tokens, turn_total_tokens, thread_total_tokens"
            " FROM responses WHERE response_id=?", (rid,)).fetchone()
        seen = dict(existing) if existing is not None else {}
        want = {"input_tokens": u.get("input_tokens"),
                "cached_input_tokens": u.get("cached_input_tokens"),
                "cache_write_input_tokens": u.get("cache_write_input_tokens"),
                "output_tokens": u.get("output_tokens"),
                "reasoning_output_tokens": u.get("reasoning_output_tokens"),
                "total_tokens": u.get("total_tokens"),
                "turn_total_tokens": tt.get("total_tokens"),
                "thread_total_tokens": th.get("total_tokens")}
        if seen != want:
            raise _UsageConflict("usage conflict")
        _migrate_response_position(r, rid, r.record_position)
        _repair_usage_context(r, rid, obj, r.model, r.effort, r.record_position)
        _note_occurrence(r, "response", rid, obj, model=r.model, effort=r.effort)
        r.stats["responses_duplicate"] += 1
    else:
        r.usage_context_seen.add(rid)
        r.stats["responses_inserted"] += 1
        _note_occurrence(r, "response", rid, obj, model=r.model, effort=r.effort)
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


def _ingest_response_item(r: _Reader, obj: dict, native_ordinal, position: int) -> None:
    p = obj["payload"]
    ptype = p.get("type")
    fallback = _fallback_native_id(r.src.path, position)
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
        native = p.get("id") or f"{r.session_key.partition(':')[2]}:{fallback}"
        if not p.get("id") and r.src.privacy_stale:
            legacy = f"{HARNESS}:ordinal:{native_ordinal}"
            prior = r.con.execute("SELECT source_id FROM user_messages WHERE native_id=? AND session_key=?",
                                  (legacy, r.session_key)).fetchone()
            if prior and r._legacy_source_matches(prior["source_id"]):
                # Preserve captured ownership for every source-owned old
                # fallback, including the first collapsed None identity.
                r.con.execute("PRAGMA defer_foreign_keys=ON")
                r.con.execute(
                    "UPDATE user_messages SET native_id=?, source_id=?, ordinal_num=?"
                    " WHERE native_id=?",
                    (f"{HARNESS}:{native}", r.src.source_id, r.record_position, legacy))
                r.con.execute("UPDATE assignments SET user_message_id=? WHERE user_message_id=?",
                              (f"{HARNESS}:{native}", legacy))
                r.con.execute("UPDATE delegations SET owning_user_message=? WHERE owning_user_message=?",
                              (f"{HARNESS}:{native}", legacy))
                r.con.execute("UPDATE verdicts SET intervention_id=? WHERE intervention_id=?",
                              (f"sub:{HARNESS}:{native}", f"sub:{legacy}"))
        # Privacy rule 1 via agent_observer/privacy.py: only a genuine
        # human submission of a proven main session keeps an excerpt,
        # truncated at the first tag-like marker. The main-session gate
        # reads the prescanned native thread/source metadata (fail closed
        # when unknown), so a child or worker rollout with a
        # genuine-looking message still stores nothing. Identity extraction
        # above already saw the complete text.
        excerpt = privacy.submission_excerpt(
            text, is_genuine=kind == "genuine", is_main_session=r.is_main)
        genuine = 1 if kind == "genuine" else 0
        native_key = f"{HARNESS}:{native}"
        # This source may refresh its own row. A continuation of the same
        # session records an occurrence and leaves the row. The parent
        # rollout can claim a copy that a child stored before the parent
        # was read.
        prior = r.con.execute(
            "SELECT source_id, session_key FROM user_messages WHERE native_id=?",
            (native_key,)).fetchone()
        same_source = prior is None or prior["source_id"] == r.src.source_id
        claims = prior is not None and prior["session_key"] != r.session_key
        first = native_key not in r.user_messages_seen
        r.user_messages_seen.add(native_key)
        cur = r.con.execute(
            "INSERT OR IGNORE INTO user_messages(native_id, source_id,"
            " session_key, turn_id, ordinal_num, ts, kind, text_hash,"
            " text_excerpt, is_genuine) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (native_key, r.src.source_id, r.session_key, turn_id,
             r.record_position, iso_ts(obj.get("timestamp")), kind,
             text_hash(text), excerpt, genuine))
        if cur.rowcount:
            r.stats["user_messages_inserted"] += 1
        elif r.src.privacy_stale and (same_source or claims):
            # Rule 3: a privacy version change corrects rows in place.
            fields = ("source_id=?, session_key=?, turn_id=?, ts=?, kind=?,"
                      " text_hash=?, text_excerpt=?, is_genuine=?")
            values = [r.src.source_id, r.session_key, turn_id,
                      iso_ts(obj.get("timestamp")), kind, text_hash(text),
                      excerpt, genuine]
            if first:
                fields = ("source_id=?, session_key=?, turn_id=?, ordinal_num=?,"
                          " ts=?, kind=?, text_hash=?, text_excerpt=?, is_genuine=?")
                values = [r.src.source_id, r.session_key, turn_id,
                          r.record_position, iso_ts(obj.get("timestamp")), kind,
                          text_hash(text), excerpt, genuine]
            r.con.execute(
                f"UPDATE user_messages SET {fields} WHERE native_id=?",
                values + [native_key])
            r.stats["user_messages_updated"] = \
                r.stats.get("user_messages_updated", 0) + 1
        _note_occurrence(r, "user", native_key, obj)
        return
    if ptype == "message" and p.get("role") == "assistant":
        text = _text_of_message(p)
        if text:
            excerpt = privacy.assistant_excerpt(text)
            r.event(obj, "assistant_message", p.get("id") or fallback,
                    legacy_id=f"ordinal:{native_ordinal}" if not p.get("id") else None,
                    turn_id=turn_id, name="assistant_message",
                    size_bytes=len(text), fingerprint=text_hash(text),
                    detail={"excerpt": excerpt} if excerpt else None)
        return
    if ptype in ("function_call", "custom_tool_call"):
        call_id = p.get("call_id")
        name = p.get("name") or (
            p.get("custom_tool_call") or {}).get("name", "unknown")
        if not isinstance(name, str):
            name = "unknown"
        namespace = p.get("namespace")
        prefix = f"{namespace}." if isinstance(namespace, str) else ""
        args = p.get("arguments") if ptype == "function_call" else p.get("input")
        r.event(obj, "tool_call", call_id or p.get("id"), turn_id=turn_id,
                name=f"{prefix}{name}", target=_call_target(
                    args, custom=ptype == "custom_tool_call"),
                status=p.get("status"),
                fingerprint=fingerprint(name, str(args)[:2000]))
    elif ptype in ("function_call_output", "custom_tool_call_output"):
        call_id = p.get("call_id")
        out = p.get("output")
        if isinstance(out, list):
            size = sum(len(str(c.get("text", ""))) for c in out
                       if isinstance(c, dict))
        else:
            size = len(str(out or ""))
        r.event(obj, "tool_result", call_id or p.get("id"), turn_id=turn_id,
                name=ptype, size_bytes=size)


_PATH_KEYS = ("file_path", "path", "filePath", "target_file", "filename")


def _call_target(args, custom: bool = False) -> str | None:
    """The path or full command a tool call names (privacy rule 8).

    A custom tool input string (a code-mode exec program or an apply_patch
    body) is the command itself. Function arguments are a JSON object whose
    path key or cmd/command (a string, or string parts joined by spaces)
    is the target. Other arguments, such as agent messages, are free text
    and stay out. Only a custom tool input string is taken raw; function
    arguments that are not a JSON object yield no target.
    """
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except ValueError:
            return args if custom else None
        if not isinstance(parsed, dict):
            return args if custom else None
        args = parsed
    if not isinstance(args, dict):
        return None
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("cmd", "command"):
        value = args.get(key)
        if isinstance(value, list):
            value = " ".join(v for v in value if isinstance(v, str))
        if isinstance(value, str) and value:
            return value
    return None


def _ingest_event_msg(r: _Reader, obj: dict, ordinal: int) -> None:
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
                duration_ms=p.get("duration_ms"))
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
            # Privacy rule 6: the MCP server and tool are instance values,
            # not canonical kinds, so every MCP call normalizes to the
            # canonical unknown-kind member; the call stays joinable on its
            # native id.
            r.event(obj, "tool_result", item.get("id"), turn_id=turn_id,
                    name="mcp.unknown",
                    status=item.get("status"), size_bytes=size)
        elif itype == "SubAgentActivity":
            r.event(obj, "lifecycle", item.get("id"), turn_id=turn_id,
                    name="subagent_activity", status=item.get("kind"))
        elif itype == "FileChange":
            # Paths are reread evidence; contents stay out. Detail keeps the
            # sorted path list only (privacy rule 6).
            changes = item.get("changes") or {}
            paths = sorted(p for p in changes.keys()
                           if isinstance(p, str) and p)
            r.event(obj, "file_change", item.get("id"), turn_id=turn_id,
                    name="file_change",
                    target=paths[0] if paths else None,
                    fingerprint=fingerprint(paths),
                    detail={"paths": paths} if paths else None)
        elif itype == "ContextCompaction":
            # Sparse native compaction marker: boundary identity only.
            r.stats["compactions"] += 1
            r.compaction_seen = True
            r.event(obj, "compaction", item.get("id"),
                    turn_id=f"{HARNESS}:{item['turn_id']}" if item.get("turn_id") else turn_id,
                    name="context_compaction")
        elif itype == "CollabAgentToolCall":
            # Sub-agent collaboration (spawn, send, wait): the dispatch edge
            # between threads, kept with its native thread identities. The
            # native tool is an instance value, so the name normalizes to
            # the canonical unknown-kind member (privacy rule 6).
            r.event(obj, "lifecycle", item.get("id"), turn_id=turn_id,
                    name="collab.unknown",
                    status=item.get("status"))
        elif itype in ("ImageView", "WebSearch", "DynamicToolCall",
                       "FunctionCallOutput"):
            raw_target = item.get("path") or item.get("tool")
            target = raw_target if isinstance(raw_target, str) else None
            # Fixed native mappings stay canonical; an arbitrary dynamic
            # tool normalizes to the unknown-kind member (privacy rule 6).
            dynamic_name = ({"ImageView": "image_view", "WebSearch": "web_search",
                             "FunctionCallOutput": "function_call_output"}.get(itype)
                            or "dynamic.unknown")
            r.event(obj, "tool_result", item.get("call_id") or item.get("id"),
                    turn_id=turn_id,
                    name=dynamic_name,
                    target=target,
                    status=item.get("status"))
        elif itype in ("Plan", "HookPrompt", "EnteredReviewMode",
                       "ExitedReviewMode"):
            r.event(obj, "lifecycle", item.get("id") or f"{itype}:{obj.get('ordinal')}",
                    turn_id=turn_id, name=itype, status=item.get("status"))
        elif itype == "Extension":
            if item.get("kind") != "web.search":
                raise _UnsupportedSchema("unsupported extension kind")
            r.event(obj, "tool_result", item.get("call_id") or item.get("id"),
                    turn_id=turn_id, name="web_search", status=item.get("status"))
        elif itype in ("UserMessage", "AgentMessage", "Reasoning"):
            # Content payload, not an operational event; captured from
            # response_item to avoid double counting wrapper and inner forms.
            return
        else:
            raise ValueError(f"unsupported completed item: {itype!r}")
    elif etype == "thread_goal_updated":
        r.event(obj, "lifecycle", f"thread_goal_updated:{obj.get('ordinal')}",
                turn_id=turn_id, name="thread_goal_updated")
    elif etype == "token_count":
        # A usage snapshot of counters also carried by token_usage_record in
        # current rollouts; kept only as the fallback for older ones.
        # No falsey shortcuts: malformed buckets are collected for flush to
        # quarantine before any SQL, so later valid records still import.
        info_raw = p.get("info")
        if info_raw is None:
            return
        if not isinstance(info_raw, dict):
            raise _MalformedUsage("malformed usage")
        last_raw = info_raw.get("last_token_usage")
        total_raw = info_raw.get("total_token_usage")
        if last_raw is None and total_raw is None:
            return
        # Collect for validated flush; missing halves, malformed shapes and
        # counters quarantine there without losing later valid usage snapshots.
        # The structural source ordinal travels along so deferred errors
        # deduplicate NULL-safely instead of passing a native ordinal that
        # may be absent. The compaction flag travels along for the same
        # reason: flushing happens after the whole file is read, so each
        # usage snapshot must remember whether a compaction boundary already
        # preceded it at collection time.
        r.token_counts.append((ordinal, obj, info_raw, r.compaction_seen, r.model, r.effort))
    elif etype == "thread_settings_applied":
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
        native_id = (f"turn_aborted:{p['turn_id']}" if p.get("turn_id")
                     else f"turn_aborted:{_fallback_native_id(r.src.path, ordinal)}")
        r.event(obj, "lifecycle", native_id,
                legacy_id="turn_aborted:None" if not p.get("turn_id") else None,
                turn_id=turn_id, name="turn_aborted", status="cancelled",
                duration_ms=p.get("duration_ms"))
    else:
        raise ValueError(f"unsupported event_msg type: {etype!r}")


def _ingest_command(r: _Reader, obj: dict, item: dict, turn_id) -> None:
    cmd = item.get("command") or []
    output = item.get("stdout") or item.get("output") or ""
    size = len(str(output))
    # The target is the executed shell command (string parts only); native
    # structures never stringify into the ledger.
    shown = None
    if isinstance(cmd, list):
        parts = [p for p in cmd if isinstance(p, str) and p]
        shown = parts[-1] if parts else None
    elif isinstance(cmd, str) and cmd:
        shown = cmd
    r.event(obj, "tool_result", item.get("id"), turn_id=turn_id,
            name="exec", target=shown,
            status=item.get("status"), duration_ms=_duration(item),
            size_bytes=size,
            truncated=1 if item.get("truncated") else None,
            fingerprint=fingerprint(cmd),
            detail={"exit_code": item.get("exit_code")})
    # Observed file reads come only from parsed_cmd entries, never mentions.
    # Only a native string path or name is accepted: anything else drops the
    # read safely, so a non-string value can never reach events.target,
    # identity, or the derived native id through coercion.
    for entry in item.get("parsed_cmd") or []:
        if isinstance(entry, dict) and entry.get("type") == "read":
            target = entry.get("path")
            if not isinstance(target, str) or not target:
                target = entry.get("name")
            if not isinstance(target, str) or not target:
                continue
            r.identity.observe_path(target)
            raw_skill = skill_from_path(target)
            safe_skill = privacy.filter_target(raw_skill, family="skill_read") \
                if raw_skill else None
            looks_like_skill = target.endswith("SKILL.md") \
                or "/skills/" in target
            fam = "skill_read" if (safe_skill or looks_like_skill) else "read"
            if fam == "skill_read":
                # Rule 6: skill_read target holds only the validated skill
                # identifier, never the installed path. The file path lives
                # only in detail skill_path.
                r.event(obj, fam, f"{item.get('id')}:{target}", turn_id=turn_id,
                        name=os.path.basename(target), target=safe_skill,
                        status=item.get("status"),
                        duration_ms=_duration(item), size_bytes=size,
                        fingerprint=fingerprint(target),
                        detail={"cmd": entry.get("cmd"), "skill": raw_skill,
                                "skill_path": target})
            else:
                r.event(obj, fam, f"{item.get('id')}:{target}", turn_id=turn_id,
                        name=os.path.basename(target), target=target,
                        status=item.get("status"),
                        duration_ms=_duration(item), size_bytes=size,
                        fingerprint=fingerprint(target),
                        detail={"cmd": entry.get("cmd")})


def _ingest_compacted(r: _Reader, obj: dict) -> None:
    p = obj["payload"]
    r.stats["compactions"] += 1
    r.compaction_seen = True
    if "window_id" not in p:
        # Older rollouts record a compaction with its replacement history
        # but no window identity; the boundary is kept by position.
        r.event(obj, "compaction", f"compacted:{obj.get('ordinal')}",
                name="context_compaction")
        return
    latest = p.get("latest_token_usage_record") or {}
    rid = latest.get("response_id")
    # The embedded latest usage repeats an already counted response and is
    # never inserted; the boundary event alone marks the compaction.
    r.event(obj, "compaction", p.get("window_id"), name="context_compaction")


def _duration(item: dict):
    d = item.get("duration") or item.get("duration_ms") or item.get("elapsed")
    if isinstance(d, (int, float)):
        return int(d * 1000) if d < 100000 and d != int(d) else int(d)
    return None
