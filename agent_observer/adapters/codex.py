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
import json
import os
import sqlite3

from .. import db, privacy
from ..identity import SessionIdentity, skill_from_path
from ..ingest import (JsonlSource, MissingNativeId, fingerprint, insert_event,
                      iso_ts, text_hash)

HARNESS = "codex"
SEMANTICS = "codex:input_includes_cached,output_includes_reasoning"
# Older rollouts carry usage only in token_count events: last_token_usage per
# response and a cumulative total that repeats when nothing new happened.
TOKEN_COUNT_SEMANTICS = SEMANTICS + ";source=token_count"
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
        self.thread_source = src.row["thread_source"]
        self.cli_version = src.row["cli_version"]
        self.identity = SessionIdentity()
        self.model = None
        self.effort = None
        self.first_ts = None
        self.last_ts = None
        self.meta: dict = {}
        self.token_counts: list = []
        # Compaction context for deferred token_count flushing: token_count
        # checkpoints are collected during ingestion and reconciled only at
        # flush, so each checkpoint captures whether a compaction boundary
        # (compacted record or ContextCompaction item) already preceded it.
        # A repeated cumulative checkpoint after compaction whose cumulative
        # bucket is unchanged is duplicate evidence, not a conflict.
        self.compaction_seen = False
        # Ordering guard for the deferred flush (set by import_codex_file
        # before ingestion): the structural ordinal where this import's
        # prefix begins, and a high-water mark over the events table. A
        # checkpoint counts as post-compaction only when the boundary
        # precedes it: the captured flag covers this import, and the ledger
        # query below additionally requires id <= prior_event_ceiling (so
        # compaction events written later in this same import never leak
        # in) and ordinal_num < import_start_ordinal (so only evidence from
        # before the imported prefix counts on incremental imports; full
        # re-reads start at zero and consult nothing but the captured flag).
        self.import_start_ordinal = 0
        self.prior_event_ceiling = None
        # Every own-thread identity seen so far (persisted plus prescanned
        # plus streamed): the main-session gate fails closed on divergence.
        # Referenced worker threads (spawn targets) never enter this set.
        self.observed_threads: set = set()
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

    def event(self, obj, family, native_id, **kw):
        insert_event(self.con, self.stats, source_id=self.src.source_id,
                     session_key=self.session_key, family=family,
                     native_id=native_id, ordinal=obj.get("ordinal"),
                     ts=iso_ts(obj.get("timestamp")),
                     update=self.src.privacy_stale, **kw)


def _prescan_thread_meta(path: str, start_offset: int) -> dict:
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
        "submissions_inserted": 0, "events_inserted": 0,
        "events_duplicate": 0, "compactions": 0, "malformed": 0,
    }
    src = JsonlSource(con, HARNESS, path, full=full)
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
    # Seed the rollout's own thread identity before any row is written, so
    # a genuine user message keeps its excerpt only when the whole new
    # portion (plus persisted metadata) already proves a human main
    # session, and the session key is stable from the first insert.
    pre = _prescan_thread_meta(path, src.start_offset)
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
    for ordinal, obj, line in src.records():
        stats["lines"] += 1
        if obj is None or not isinstance(obj, dict):
            stats["malformed"] += 1
            src.error(ordinal,
                      "malformed_json" if obj is None else "unknown_record",
                      line)
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
        key_fix = con.execute(
            "UPDATE submissions SET session_key=? WHERE source_id=?"
            " AND session_key!=?",
            (reader.session_key, src.source_id, reader.session_key))
        if key_fix.rowcount:
            stats["submissions_updated"] = \
                stats.get("submissions_updated", 0) + key_fix.rowcount
        if not reader.is_main:
            cleared = con.execute(
                "UPDATE submissions SET text_excerpt='' WHERE source_id=?"
                " AND text_excerpt!=''", (src.source_id,))
            if cleared.rowcount:
                stats["submissions_updated"] = \
                    stats.get("submissions_updated", 0) + cleared.rowcount
    fields = {"started_at": reader.first_ts, "ended_at": reader.last_ts,
              **reader.meta, **reader.identity.fields(con)}
    db.upsert_session(con, reader.session_key, HARNESS,
                      reader.session_key.split(":", 1)[1], src.source_id,
                      **fields)
    stats.update(src.finish(session_id=reader.session_id,
                            thread_id=reader.thread_id,
                            cli_version=reader.cli_version,
                            thread_source=reader.thread_source))
    stats["session_key"] = reader.session_key
    con.commit()
    return stats


def _ingest_record(r: _Reader, obj: dict, ordinal: int) -> None:
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
        _ingest_response_item(r, obj)
    elif rtype == "turn_context":
        _ingest_turn(r, obj)
    elif rtype == "event_msg":
        _ingest_event_msg(r, obj, ordinal)
    elif rtype == "compacted":
        _ingest_compacted(r, obj)
    # world_state and inter_agent_communication_metadata carry no ledger rows
    # beyond source metadata; they stay private in the raw file.


def _reconcile_fallback(r: _Reader) -> None:
    """Mark only genuinely reconciled fallback checkpoints as overlap.

    A legacy cumulative checkpoint is covered when an authoritative
    response's own thread span (thread_total minus its own total up to
    thread_total) contains that cumulative point: the authoritative record
    then accounts for the same native work. Checkpoints outside every
    authoritative span stay counted, so partial transitions never undercount
    and no representation is ever counted twice. The rows stay as evidence.
    """
    r.con.execute(
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
        (r.session_key, TOKEN_COUNT_SEMANTICS, SEMANTICS))


def _cumulative_sig(total_raw: dict) -> tuple:
    """The cumulative accounting bucket of one fallback checkpoint.

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


def _flush_token_counts(r: _Reader) -> None:
    """Count token_count checkpoints for rollouts without full usage cover.

    Each rising cumulative total is one new response keyed by that total, so
    appended logs, copied snapshots and full reimports all converge. Every
    duplicate fallback key is compared across all fallback counters: an exact
    repeat is a no-op and any difference is quarantined without overwriting,
    with one narrow exception. A repeated checkpoint with the same cumulative
    identity that arrives after a compaction boundary, and whose cumulative
    bucket repeats this import's first checkpoint for that total while only
    last-token or rate-limit metadata changed, is duplicate compaction
    evidence: it is ignored without inserting another response, changing the
    stored row, or recording an error. Pre-compaction repeats with changed
    counters, and post-compaction repeats with a changed cumulative bucket,
    stay quarantined as usage conflicts. Malformed buckets are quarantined
    before any SQL and later valid records still import. Rising checkpoints
    are always preserved as evidence, wherever they appear; reconciliation
    then marks only the checkpoints an authoritative span genuinely covers
    as overlap."""

    if r.token_counts:
        row = r.con.execute(
            "SELECT MAX(thread_total_tokens) t FROM responses"
            " WHERE session_key=? AND semantics=?",
            (r.session_key, TOKEN_COUNT_SEMANTICS)).fetchone()
        previous = row["t"] or 0
        # First cumulative bucket per total seen in this import, in file
        # order. The map is seeded from the persisted first-seen
        # signatures of earlier imports, so an incremental suffix that
        # begins with a post-compaction repeat compares against the same
        # reference as a full import. A repeat can only be compaction
        # evidence when the total's first checkpoint is already known;
        # a lone repeat with no prior signature fails closed into the
        # usage_conflict path below.
        first_cumulative: dict = _load_cumulative_sigs(r.con, r.session_key)
        prior_compaction = None

        def _post_compaction(captured: bool) -> bool:
            """Whether a compaction boundary precedes this checkpoint.

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
                        " AND family='compaction' AND id <= ?"
                        " AND ordinal_num IS NOT NULL"
                        " AND ordinal_num < ? LIMIT 1",
                        (r.session_key, r.prior_event_ceiling,
                         r.import_start_ordinal)).fetchone()
                    prior_compaction = hit is not None
            return prior_compaction

        for struct_ordinal, obj, info, compacted_before in r.token_counts:
            ordinal = struct_ordinal
            try:
                if not isinstance(info, dict):
                    raise _MalformedUsage("malformed usage")
                last_raw = info.get("last_token_usage")
                total_raw = info.get("total_token_usage")
                if last_raw is None or total_raw is None:
                    # A checkpoint without both usages is malformed: quarantine
                    # before any SQL and continue with later checkpoints.
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
                     r.session_key, r.thread_id, obj.get("ordinal"), iso_ts(obj.get("timestamp")),
                     r.model, r.effort, last_raw.get("input_tokens"), last_raw.get("cached_input_tokens"),
                     last_raw.get("cache_write_input_tokens"), last_raw.get("output_tokens"),
                     last_raw.get("reasoning_output_tokens"), last_raw.get("total_tokens"), total,
                     TOKEN_COUNT_SEMANTICS))
                if cur.rowcount:
                    r.stats["responses_inserted"] += 1
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
    # session holds no fallback checkpoints or no authoritative responses,
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
    _reconcile_fallback(r)


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
         obj.get("ordinal"), iso_ts(obj.get("timestamp")), r.model, r.effort,
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
        cur = r.con.execute(
            "INSERT OR IGNORE INTO submissions(native_id, source_id,"
            " session_key, turn_id, ordinal_num, ts, kind, text_hash,"
            " text_excerpt, is_genuine) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"{HARNESS}:{native}", r.src.source_id, r.session_key, turn_id,
             obj.get("ordinal"), iso_ts(obj.get("timestamp")), kind,
             text_hash(text), excerpt, genuine))
        if cur.rowcount:
            r.stats["submissions_inserted"] += 1
        elif r.src.privacy_stale:
            # Rule 3: a privacy version change corrects rows in place.
            r.con.execute(
                "UPDATE submissions SET source_id=?, session_key=?, turn_id=?,"
                " ordinal_num=?, ts=?, kind=?, text_hash=?, text_excerpt=?,"
                " is_genuine=? WHERE native_id=?",
                (r.src.source_id, r.session_key, turn_id, obj.get("ordinal"),
                 iso_ts(obj.get("timestamp")), kind, text_hash(text), excerpt,
                 genuine, f"{HARNESS}:{native}"))
            r.stats["submissions_updated"] = \
                r.stats.get("submissions_updated", 0) + 1
        return
    if ptype == "message" and p.get("role") == "assistant":
        text = _text_of_message(p)
        if text:
            excerpt = privacy.assistant_excerpt(text)
            r.event(obj, "assistant_message", p.get("id") or f"ordinal:{obj.get('ordinal')}",
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
                name=f"{prefix}{name}",
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
                    target=target[:500] if target else None,
                    status=item.get("status"))
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
    elif etype == "token_count":
        # A checkpoint of counters also carried by token_usage_record in
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
        # counters quarantine there without losing later valid checkpoints.
        # The structural source ordinal travels along so deferred errors
        # deduplicate NULL-safely instead of passing a native ordinal that
        # may be absent. The compaction flag travels along for the same
        # reason: flushing happens after the whole file is read, so each
        # checkpoint must remember whether a compaction boundary already
        # preceded it at collection time.
        r.token_counts.append((ordinal, obj, info_raw, r.compaction_seen))
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
        r.event(obj, "lifecycle", f"turn_aborted:{p.get('turn_id')}",
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
            name="exec", target=shown[:500] if shown else None,
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
