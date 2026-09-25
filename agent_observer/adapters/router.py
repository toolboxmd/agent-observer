"""Model Router ledger adapter: router jobs into the workload ledger.

Reads Model Router's ``jobs.db`` read-only (SQLite URI ``mode=ro``) and
copies jobs, invocations and readings into the Observer ``router_*``
tables, then maps that ledger onto the existing workload tables (tasks,
attempts, outcomes, session_assignments). Router-owned Codex rollouts
under the state directory are imported through the Codex adapter so
native usage and events land under the same session keys.

Privacy (fail closed, planner ruling 2026-09-23; agent_observer/privacy.py
is the one implementation):
- Never stores the task goal or other native free text. The task title is
  the validated issue reference when available, otherwise the validated
  request id.
- Invocation reason is stored only as an exact member of the router's
  closed reason vocabulary, otherwise NULL.
- Raw task, usage, native-id, skills, tools and other JSON strings are
  never copied into non-exempt tables. Usage evidence, when retained, is
  a new numeric-only projection without arbitrary members such as
  ``source``; otherwise the destination stays NULL. Native ids are parsed
  in memory only for session binding.
- Every import error uses privacy.error_category (unknown maps to the
  fixed fallback) and every shape excerpt uses privacy.line_excerpt
  (sorted top-level key names only, empty for non-objects, max 200).
  Router ledger event payloads never enter the ledger; any event detail
  or target would go through privacy.filter_detail/filter_target, and the
  Codex rollout path supplies those through the core importer.
- Missing or dangling ids are quarantined with ``missing_id`` and a
  shape-only excerpt; the row is skipped and later rows continue.
- The jobs.db ledger is a versioned Observer source under its canonical
  path: the same sources row is reused, privacy.PRIVACY_VERSION is
  recorded after a successful import, an unchanged ledger under the same
  version writes nothing, a version mismatch first replaces that
  source's prior router import_errors then re-reads fully with in-place
  updates, and a failed import leaves the source stale for retry.
- Token-shaped fields stay fail closed: block_reason only from a closed
  set of known block classes, direction_supply only from its closed enum,
  hashes only as fixed-length hex for their native type (40-char commit
  SHAs, 64-char content hashes), numerics never bool/NaN/infinity, and
  reading timestamps validated before storage.
- Outcomes carry durable router provenance in ``router_outcome_provenance``.
  A pre-existing outcome without provenance, or one that differs from the
  last router snapshot, is preserved byte-for-byte (this keeps a human
  blank unknown row and a human row whose repairs collide with the
  ``router_status:`` prefix).

Counter rule: the router's ``usage_json`` is ledger evidence kept on the
attempt row only as a numeric-only projection. It is never added to
``responses`` and never summed with native counters; a cross-check against
the native total for the bound session is recorded in ``import_errors``
when the two disagree.

Stdlib only. Never writes the router database or rollout files.
"""

from __future__ import annotations

import glob
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import urllib.parse

from .. import db, privacy
from ..ingest import iso_ts
from . import codex as _codex

HARNESS = "router"
ROUTER_SCHEMA_VERSION = 2
DEFAULT_ROOT = os.path.expanduser("~/.local/share/durable-runner")
LEGACY_ROOT = os.path.expanduser("~/.local/state/model-router")

CAPABILITIES = [
    ("router_ledger", True, "router jobs, invocations and readings copied read-only; usage stays numeric-only evidence on attempts, never native responses"),
    ("tool_calls", False, "no router tool-call import; native rollouts carry tool evidence"),
    ("tool_results", False, "no router tool-result import; native rollouts carry tool evidence"),
    ("read_evidence", False, "no file-read import from the router ledger"),
    ("lifecycle_task", True, "invocation terminal_class maps onto attempt state; router job status stays outcome evidence only"),
    ("human_input", False, "no submissions are synthesized from router rows"),
    ("instruction_identity", False, "no instruction identity is inferred from router rows"),
    ("workload_binding", True, "jobs map to tasks and invocations to attempts; worker sessions bind through session_assignments"),
]

# Closed router vocabularies, derived from Model Router's own contract
# (installed runner: store.TERMINAL, store.ACTIVE_WORKSPACE_STATUSES,
# core.terminal_class_for, core.JOB_KINDS, core.PLANNER_HARNESSES,
# harnesses.INVOCATION_KINDS and harness stage/session_kind values,
# policy.STAGES, policy.IMPLEMENTATION_LANES, policy.SIGNAL_CLASSES,
# controller/direction persisted reasons) plus the live ledger's distinct
# values. Anything outside these sets is dropped to NULL (fail closed)
# rather than stored.
#
# Persisted invocations.reason values: core's "initial" default and
# "compact_after_submit"; controller dispatch "initial", "resume",
# "dispatch_stalled", "dispatch_exhausted" and the preflight reasons;
# "planner_question"; the ladder's "correction" and "escalation"; the
# capacity moves "pool_move", "lateral", "larger_context" and
# "stalled_retry". _switch_route appends "_concurrent" to a move reason
# when the target was concurrency-full, giving the suffixed variants.
# "dispatch_fallback rc=N" is a dynamic f-string, never a closed value.
REASONS = frozenset({
    "initial",
    "resume",
    "planner_question",
    "compact_after_submit",
    "correction",
    "escalation",
    "pool_move",
    "lateral",
    "larger_context",
    "stalled_retry",
    "dispatch_stalled",
    "dispatch_exhausted",
    "preflight_exhausted",
    "preflight_degraded",
    "preflight_one_turn",
    "preflight_concurrent",
    "pool_move_concurrent",
    "lateral_concurrent",
    "larger_context_concurrent",
    "correction_concurrent",
    "escalation_concurrent",
    "preflight_exhausted_concurrent",
    "preflight_degraded_concurrent",
    "preflight_one_turn_concurrent",
})

# store.TERMINAL plus store.ACTIVE_WORKSPACE_STATUSES: the only job
# statuses the contract writes (live ledger: cancelled, succeeded,
# blocked, running). Observer words such as "complete" are never router
# job statuses.
JOB_STATUSES = frozenset({
    "pending",
    "running",
    "question_pending",
    "blocked",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
})

# Every core.terminal_class_for output: rc/signal mapping (crashed from
# the crashed flag; quota/overloaded/stalled/context/hard_error from
# policy.SIGNAL_CLASSES exhausted/overloaded/stalled/context/hard;
# completed/timeout/cancelled/failed from rc 0/124/143,-15/other;
# infrastructure for a startup failure with an explicit startup marker).
# Live ledger: completed, failed, stalled.
TERMINAL_CLASSES = frozenset({
    "completed",
    "failed",
    "timeout",
    "overloaded",
    "stalled",
    "context",
    "hard_error",
    "infrastructure",
    "cancelled",
    "crashed",
    "quota",
})

# harnesses.INVOCATION_KINDS plus the durable proof kind
# (runner/core.record_verification_attempt writes kind proof for an
# executed verification). Live ledger holds all but opencode_serve
# and grok_control; proof rows appear only after Router87.
INVOCATION_KINDS = frozenset({
    "codex_dispatch",
    "codex_resume",
    "claude_callback",
    "claude_compact",
    "opencode_control",
    "opencode_serve",
    "grok_control",
    "proof",
})

# Harness stage_for values plus the durable verification stage
# (proof invocations carry stage verification). Live ledger holds
# dispatch, planning and implementation; verification appears only
# after Router87.
STAGES = frozenset({
    "dispatch",
    "planning",
    "implementation",
    "verification",
})

SESSION_KINDS = frozenset({
    "codex_task_id",
    "opencode_session_id",
    "planner_session_id",
    "grok_session_id",
})

# policy.IMPLEMENTATION_LANES. Submit stores the resolved lane
# (policy.resolve_lane), so raw aliases such as "default" never persist;
# live ledger: implementation_small, implementation_default.
LANES = frozenset({
    "implementation_default",
    "implementation_small",
    "implementation_hard",
})

# core.JOB_KINDS (live ledger: ordinary).
JOB_KINDS = frozenset({
    "ordinary",
    "experiment",
    "replay",
})

# core.PLANNER_HARNESSES: only claude runs the planner callback
# (live ledger: claude).
PLANNER_HARNESSES = frozenset({
    "claude",
})

# Closed block classes observed in the router ledger. An arbitrary prefix
# or message is not a class and becomes NULL (fail closed).
BLOCK_CLASSES = frozenset({
    "codex_auth_failed",
    "quota_blocked",
})

# Closed direction-supply vocabulary persisted by the router schema
# (runner/direction.py supply_for_harness and session_input). Anything
# else becomes NULL (fail closed).
DIRECTION_SUPPLIES = frozenset({
    "hook",
    "runner",
    "none",
})

# Executed proof classes from runner/core.PROOF_CLASSES. A proof
# invocation row carries its class in reason and in meta_json
# proof_class; skipped/none never have invocation rows. Anything
# else becomes NULL (fail closed).
PROOF_CLASSES = frozenset({
    "pass",
    "failed",
    "timeout",
    "not_found",
    "error",
    "skipped",
    "none",
})

# Whitelisted Router ledger event kinds retained as sanitized
# projections. Every other kind is validated and discarded, never
# stored. question_posted is retained only for qid recovery-decision;
# its prompt text is never stored.
EVENT_KINDS = frozenset({
    "recovery_decision",
    "recovery_next_attempt",
    "recovery_attempt_result",
    "verification_attempt",
    "route_switched",
    "planner_route_rejected",
    "question_posted",
})

# Closed route-switch scopes: dispatch moves record why the dispatch
# moved routes apart from the worker reason; worker moves are ordinary
# capacity moves. Anything else becomes NULL.
ROUTE_SCOPES = frozenset({
    "dispatch",
    "worker",
})

# Closed recovery outcomes for recovery_attempt_result.
RECOVERY_OUTCOMES = frozenset({
    "ok",
    "failed",
})

# Closed ladder rungs for recovery_decision (controller LADDER_RUNGS
# plus the planner-directed rung).
RECOVERY_RUNGS = frozenset({
    "initial",
    "correction",
    "correction_fresh",
    "recovery",
    "recovery_directed",
})

# Closed recovery reasons: the ladder's correction/escalation and the
# planner-directed reason. Anything else becomes NULL.
RECOVERY_REASONS = frozenset({
    "correction",
    "escalation",
    "planner_directed",
})

# The only planner question identity retained. Its prompt text is
# never stored.
RECOVERY_QUESTION_ID = "recovery-decision"

_ID_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
# Fixed lengths for the native hash types: git commit SHAs are 40 hex
# chars, content hashes (kit, direction) are 64 hex chars. Anything else
# is not a hash and becomes NULL.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_CONTENT_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def discover(root: str | None = None) -> list[str]:
    roots = [root] if root is not None else [
        os.environ.get("DURABLE_RUNNER_STATE_DIR"), DEFAULT_ROOT, LEGACY_ROOT]
    paths = []
    for candidate in roots:
        if not candidate:
            continue
        path, _ = _resolve_paths(os.path.expanduser(candidate), None)
        canonical = _canonical_source_path(path)
        if os.path.isfile(canonical) and canonical not in paths:
            paths.append(canonical)
    return paths


def sync(con: sqlite3.Connection, root: str | None = None,
         full: bool = False, source: str | None = None) -> dict:
    totals = {"harness": HARNESS, "sources": 0, "unchanged": 0,
              "responses_inserted": 0, "events_inserted": 0,
              "submissions_inserted": 0, "malformed": 0, "failed": [],
              "jobs": 0, "invocations": 0, "bindings": 0, "readings": 0,
              "rollouts": 0}
    if root is None and source is None:
        paths = discover()
        if paths:
            for path in paths:
                result = sync(con, source=path, full=full)
                for key, value in result.items():
                    if key == "failed":
                        totals[key].extend(value)
                    elif key != "harness":
                        totals[key] += value
            return totals
    db_path, rollout_root = _resolve_paths(root, source)
    if db_path is not None:
        db_path = _canonical_source_path(db_path)
    ledger_changed = False
    pending_reconcile = None
    if db_path is None or not os.path.isfile(db_path):
        totals["failed"].append({
            "path": db_path or source or "",
            "error": privacy.error_category("source_unreadable")})
    else:
        try:
            changed, invocations, bound = _sync_ledger(
                con, db_path, totals, full=full)
        except sqlite3.DatabaseError:
            # The ledger could not be read: the source stays stale (no
            # version is recorded) so a later sync retries it.
            totals["failed"].append({
                "path": db_path,
                "error": privacy.error_category("source_unreadable")})
            con.commit()
            return totals
        if totals["failed"]:
            # The schema guard refused the ledger: import nothing further
            # from this source, not even its rollouts.
            con.commit()
            return totals
        ledger_changed = changed
        totals["sources"] += 1
        pending_reconcile = (db_path, invocations, bound)
    rollouts_changed = _sync_rollouts(con, rollout_root, totals, full=full)
    if pending_reconcile is not None:
        # Reconcile after the router-owned rollouts land, so a session
        # newly imported by this sync can still be checked.
        before = totals["malformed"]
        _reconcile_usage(con, *pending_reconcile, totals)
        ledger_changed = ledger_changed or totals["malformed"] != before
    if not totals["failed"] and (ledger_changed or rollouts_changed):
        totals["unchanged"] = 0
    elif not totals["failed"] and totals["sources"]:
        totals["unchanged"] = 1
    con.commit()
    return totals


def _resolve_paths(root: str | None, source: str | None) -> tuple:
    """Return (jobs.db path or None, rollout root directory)."""
    if source:
        db_path = source
        rollout_root = root if root is not None else os.path.dirname(
            os.path.abspath(source))
        return db_path, rollout_root
    rollout_root = root or DEFAULT_ROOT
    return os.path.join(rollout_root, "jobs.db"), rollout_root


def _canonical_source_path(path: str) -> str:
    """Canonical Observer identity for the router jobs.db source."""
    return os.path.realpath(os.path.abspath(path))


def _open_ro(path: str) -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro"
    src = sqlite3.connect(uri, uri=True)
    src.row_factory = sqlite3.Row
    src.execute("PRAGMA query_only=ON")
    return src


def _shape_excerpt(record: object) -> str:
    """Shape-only excerpt through the shared privacy module.

    Sorted top-level key names of a JSON object record, never values;
    empty for any other record, at most 200 characters.
    """
    if not isinstance(record, dict):
        return privacy.line_excerpt("")
    try:
        raw = json.dumps(record, default=str)
    except (TypeError, ValueError):
        return ""
    return privacy.line_excerpt(raw)


def _columns_excerpt(columns) -> str:
    """Shape-only excerpt from source column names via privacy."""
    try:
        raw = json.dumps({str(c): None for c in columns})
    except (TypeError, ValueError):
        return ""
    return privacy.line_excerpt(raw)


def _record_error(con: sqlite3.Connection, source_path: str, error: str,
                  excerpt: str = "") -> None:
    """Quarantine a fixed category plus shape-only excerpt, never content.

    The error column holds exactly one closed category from
    agent_observer/privacy.py; anything else maps to its fixed fallback.
    Re-imports of the same evidence are no-ops so errors never accumulate.
    """
    safe = privacy.error_category(error)
    excerpt = (excerpt or "")[:200]
    if con.execute(
            "SELECT 1 FROM import_errors WHERE harness=? AND source_path=?"
            " AND error=? AND COALESCE(line_excerpt, '')=COALESCE(?, '')",
            (HARNESS, source_path, safe, excerpt)).fetchone():
        return False
    con.execute(
        "INSERT INTO import_errors(harness, source_path, ordinal_num, error,"
        " line_excerpt, created_at) VALUES(?,?,?,?,?,?)",
        (HARNESS, source_path, None, safe, excerpt, db.now()))
    return True


def _record_malformed(con: sqlite3.Connection, source_path: str, error: str,
                      excerpt: str, totals: dict) -> None:
    """Count a quarantine only when its row is newly inserted."""
    if _record_error(con, source_path, error, excerpt):
        totals["malformed"] += 1


def _ledger_fingerprint(jobs: list[dict], invocations: list[dict],
                        readings: list[dict], events: list[dict]) -> str:
    """Content fingerprint of the router ledger snapshot, never values out.

    The fingerprint stays inside the sources row; only shape-only excerpts
    ever reach import_errors. v3 covers the Router87 contract additions
    (proof kind, verification stage, rc/meta_seq/proof_class,
    cancel_requested and whitelisted recovery events) so existing
    sources re-import once to pick up the new projections.
    """
    parts = ["observer-router-contract-v3"]
    for rows in (jobs, invocations, readings, events):
        parts.append(sorted(
            json.dumps(r, sort_keys=True, default=str) for r in rows))
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _get_source_row(con: sqlite3.Connection, canonical: str):
    return con.execute(
        "SELECT * FROM sources WHERE harness=? AND path=?",
        (HARNESS, canonical)).fetchone()


def _record_source_success(con: sqlite3.Connection, canonical: str,
                           fingerprint: str) -> None:
    """Keep one sources row per ledger path with the current version.

    The version is written only after a successful import; a failed import
    leaves the source stale so a later sync retries it.
    """
    try:
        size = os.path.getsize(canonical)
    except OSError:
        size = 0
    if _get_source_row(con, canonical) is None:
        con.execute(
            "INSERT OR IGNORE INTO sources(harness, path, sha256,"
            " size_bytes, imported_at, privacy_version)"
            " VALUES(?,?,?,?,?,?)",
            (HARNESS, canonical, fingerprint, size, db.now(),
             privacy.PRIVACY_VERSION))
    else:
        con.execute(
            "UPDATE sources SET sha256=?, size_bytes=?, imported_at=?,"
            " privacy_version=? WHERE harness=? AND path=?",
            (fingerprint, size, db.now(), privacy.PRIVACY_VERSION,
             HARNESS, canonical))


def _job_task_id(job: dict) -> str | None:
    request_id = _valid_request_id(job.get("request_id"))
    if request_id is None:
        return None
    task = _parse_json_object(job.get("task_json")) or {}
    explicit = _valid_id_token(task.get("observer_task_id"))
    return explicit or f"router:{request_id}"


def _snapshot_bindings(con: sqlite3.Connection, jobs: list[dict],
                       invocations: list[dict]) -> tuple:
    """Non-mutating binding map from an already-read ledger snapshot.

    Read-only counterpart of the projection path: ids and parent/task
    references are validated the same way, the native session key is
    derived in memory, and no session_assignments row is written. Used by
    the unchanged fast path so usage reconciliation still sees the bound
    sessions after the router-owned rollouts land.
    """
    job_ids: dict[str, str] = {}
    for job in jobs:
        request_id = _valid_request_id(job.get("request_id"))
        if request_id is not None:
            job_ids[request_id] = _job_task_id(job)
    bound: dict[str, str] = {}
    bound_pairs: set[tuple[str, str]] = set()
    for inv in invocations:
        invocation_id = _valid_id_token(inv.get("invocation_id"))
        request_id = _valid_request_id(inv.get("request_id"))
        if invocation_id is None or request_id is None:
            continue
        if request_id not in job_ids:
            continue
        key = _session_key_for(inv)
        if key is None:
            continue
        if not con.execute("SELECT 1 FROM tasks WHERE task_id=?",
                           (job_ids[request_id],)).fetchone():
            continue
        if not con.execute("SELECT 1 FROM session_assignments WHERE session_key=? AND task_id=?",
                           (key, job_ids[request_id])).fetchone():
            continue
        bound[invocation_id] = key
        bound_pairs.add((key, job_ids[request_id]))
    return bound, bound_pairs


def _sync_ledger(con: sqlite3.Connection, db_path: str, totals: dict,
                 full: bool = False) -> tuple:
    """Validate the guard, copy the ledger, map workload.

    Rule 3 lifecycle for the jobs.db source, keyed by its canonical path:
    the same sources row is reused across syncs and carries
    privacy.PRIVACY_VERSION after a successful import. When the stored
    version differs, that source's prior router import_errors are replaced
    before validation runs, so a guard failure still leaves exactly the
    current error behind. An unchanged ledger under the same version skips
    projection writes but still returns the invocation snapshot with a
    non-mutating binding map for usage reconciliation. A failed import
    records no version, leaving the source stale for retry.

    Returns (changed, invocations, bound) so the caller can reconcile
    usage after the router-owned rollouts are imported.
    """
    src = _open_ro(db_path)
    try:
        row = _get_source_row(con, db_path)
        stale = (row is None
                 or row["privacy_version"] != privacy.PRIVACY_VERSION)
        if row is not None and stale:
            # Rule 3: errors recorded under older rules are replaced
            # instead of duplicated. This runs before the guard so a
            # refused ledger still leaves exactly the current error.
            con.execute(
                "DELETE FROM import_errors WHERE harness=?"
                " AND source_path=?",
                (HARNESS, db_path))
        guard = _check_schema_version(src)
        if guard is not None:
            error, excerpt = guard
            totals["failed"].append({
                "path": db_path,
                "error": privacy.error_category(error)})
            _record_error(con, db_path, error, excerpt)
            return False, [], {}
        changed = False
        jobs = [dict(r) for r in src.execute("SELECT * FROM jobs")]
        invocations = [dict(r) for r in
                       src.execute("SELECT * FROM invocations")]
        readings = [dict(r) for r in src.execute("SELECT * FROM readings")]
        try:
            source_events = [dict(r) for r in src.execute("SELECT * FROM events")]
        except sqlite3.DatabaseError:
            source_events = []
        totals["jobs"] = len(jobs)
        totals["invocations"] = len(invocations)
        totals["readings"] = len(readings)
        fingerprint = _ledger_fingerprint(
            jobs, invocations, readings, source_events)
        if not stale and not full and row["sha256"] == fingerprint:
            # Unchanged under the same version: skip projection writes,
            # but keep the invocation snapshot and bindings for usage
            # reconciliation once the router-owned rollouts land.
            bound, bound_pairs = _snapshot_bindings(con, jobs, invocations)
            totals["bindings"] = len(bound_pairs)
            return False, invocations, bound
        task_ids = {j.get("request_id"): _job_task_id(j) for j in jobs}
        for job in jobs:
            if _import_job(con, db_path, job, totals):
                changed = True
        bound: dict[str, str] = {}
        bound_pairs: set[tuple[str, str]] = set()
        for inv in invocations:
            if _import_invocation(con, db_path, inv, totals,
                                  task_ids.get(inv.get("request_id"))):
                changed = True
            key = _binding_key(con, inv, task_ids.get(inv.get("request_id")))
            if key is not None:
                invocation_id = _valid_id_token(inv.get("invocation_id"))
                request_id = _valid_request_id(inv.get("request_id"))
                if invocation_id is not None and request_id is not None:
                    bound[invocation_id] = key
                    bound_pairs.add((key, task_ids[request_id]))
        totals["bindings"] = len(bound_pairs)
        for reading in readings:
            if _import_reading(con, db_path, reading, totals):
                changed = True
        for event in source_events:
            if _validate_source_event(con, db_path, event, totals):
                changed = True
        _record_source_success(con, db_path, fingerprint)
        return changed, invocations, bound
    finally:
        src.close()


def _check_schema_version(src: sqlite3.Connection) -> tuple | None:
    """Refuse the ledger unless invocations and events are all version 2.

    Returns (fixed category, shape-only excerpt) so the quarantine row
    carries no ids, versions or other values, only sorted source column
    names for the offending table.
    """
    for table, id_col in (("invocations", "invocation_id"), ("events", "id")):
        info = list(src.execute(f"PRAGMA table_info({table})").fetchall())
        cols = [r["name"] for r in info]
        if "schema_version" not in set(cols):
            return ("unsupported_schema", _columns_excerpt(cols))
        bad = src.execute(
            f"SELECT {id_col}, schema_version FROM {table}"
            f" WHERE schema_version IS NOT {ROUTER_SCHEMA_VERSION}"
            " LIMIT 1").fetchone()
        if bad is not None:
            return ("unsupported_schema", _columns_excerpt(cols))
    return None


def _parse_json_object(text) -> dict | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _valid_json_object(con: sqlite3.Connection, db_path: str, inv: dict,
                       column: str, totals: dict) -> None:
    raw = inv.get(column)
    if not raw:
        return
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        obj = None
    if not isinstance(obj, dict):
        category = ("malformed_usage" if column == "usage_json"
                    else "malformed_json")
        _record_malformed(
            con, db_path, category,
            _shape_excerpt(inv), totals)


def _valid_id_token(value) -> str | None:
    """Validated identifier token: no spaces, no markup, bounded."""
    if not isinstance(value, str):
        return None
    if not value or value != value.strip():
        return None
    if not 1 <= len(value) <= 128:
        return None
    if "<" in value or "\n" in value or "\r" in value:
        return None
    if not _ID_TOKEN_RE.fullmatch(value):
        return None
    return value


def _valid_request_id(value) -> str | None:
    return _valid_id_token(value)


def _valid_issue_ref(value) -> str | None:
    """Validated issue reference, never free text.

    Issue references carry no whitespace and name an issue (``#``) or a
    path (``/``). Anything else, including a task goal with spaces, is
    dropped to NULL so the title falls back to the request id.
    """
    if not isinstance(value, str):
        return None
    if not value or value != value.strip():
        return None
    if not 1 <= len(value) <= 200:
        return None
    if "<" in value or "<<<" in value or "\n" in value or "\r" in value:
        return None
    if any(ch.isspace() for ch in value):
        return None
    if "#" not in value and "/" not in value:
        return None
    return value


def _valid_model_name(value, max_len: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    if not value or value != value.strip():
        return None
    if not 1 <= len(value) <= max_len:
        return None
    if "<" in value or "\n" in value or "\r" in value:
        return None
    if any(ch.isspace() for ch in value):
        return None
    if not _MODEL_NAME_RE.fullmatch(value):
        return None
    return value


def _valid_path(value, max_len: int = 500) -> str | None:
    """Validated path: bounded string with path shape, never free text."""
    if not isinstance(value, str):
        return None
    if not value or value != value.strip():
        return None
    if not 1 <= len(value) <= max_len:
        return None
    if "<" in value or "<<<" in value or "\n" in value or "\r" in value:
        return None
    if "\x00" in value:
        return None
    if "/" not in value and not value.startswith("."):
        return None
    return value


def _valid_commit_sha(value) -> str | None:
    """Native git commit identifier: exactly 40 hex chars, else NULL."""
    if not isinstance(value, str):
        return None
    if not value or value != value.strip():
        return None
    if not _COMMIT_SHA_RE.fullmatch(value):
        return None
    return value


def _valid_content_hash(value) -> str | None:
    """Native content hash (kit, direction): exactly 64 hex chars."""
    if not isinstance(value, str):
        return None
    if not value or value != value.strip():
        return None
    if not _CONTENT_HASH_RE.fullmatch(value):
        return None
    return value


def _valid_number(value):
    """Validated numeric counter: bool, NaN and infinity become NULL."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    return None


def _valid_reason(value) -> str | None:
    if isinstance(value, str) and value in REASONS:
        return value
    return None


def _valid_job_status(value) -> str | None:
    if isinstance(value, str) and value in JOB_STATUSES:
        return value
    return None


def _valid_terminal_class(value) -> str | None:
    if isinstance(value, str) and value in TERMINAL_CLASSES:
        return value
    return None


def _valid_direction_supply(value) -> str | None:
    if isinstance(value, str) and value in DIRECTION_SUPPLIES:
        return value
    return None


def _valid_proof_class(value) -> str | None:
    if isinstance(value, str) and value in PROOF_CLASSES:
        return value
    return None


def _valid_event_kind(value) -> str | None:
    if isinstance(value, str) and value in EVENT_KINDS:
        return value
    return None


def _valid_route_scope(value) -> str | None:
    if isinstance(value, str) and value in ROUTE_SCOPES:
        return value
    return None


def _valid_recovery_outcome(value) -> str | None:
    if isinstance(value, str) and value in RECOVERY_OUTCOMES:
        return value
    return None


def _valid_recovery_rung(value) -> str | None:
    if isinstance(value, str) and value in RECOVERY_RUNGS:
        return value
    return None


def _valid_recovery_reason(value) -> str | None:
    if isinstance(value, str) and value in RECOVERY_REASONS:
        return value
    return None


def _valid_seq(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _valid_rc(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _valid_cancel_requested(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value in (0, 1, 2):
        return value
    return None


def _meta_seq(meta_text) -> int | None:
    """Seq from meta_json for actual joins, else NULL.

    Only the integer seq used to join recovery events to invocations
    is retained; every other meta member (prompts, routes held for
    other purposes, direction blocks, runtimes) is never stored.
    """
    obj = _parse_json_object(meta_text)
    if obj is None:
        return None
    return _valid_seq(obj.get("seq"))


def _meta_proof_class(meta_text) -> str | None:
    """Proof class from meta_json for proof rows, else NULL."""
    obj = _parse_json_object(meta_text)
    if obj is None:
        return None
    return _valid_proof_class(obj.get("proof_class"))


def _block_class(block_reason) -> str | None:
    """Only a known closed block class; an arbitrary prefix is NULL.

    The class token before the first colon must be an exact member of
    BLOCK_CLASSES, so a malicious or novel message never persists.
    """
    if not isinstance(block_reason, str) or not block_reason.strip():
        return None
    head = block_reason.split(":", 1)[0].strip()
    if not head:
        return None
    token = head.split()[0]
    if token in BLOCK_CLASSES:
        return token
    return None


def _task_title(issue, request_id) -> str | None:
    """Task title: validated issue reference, else validated request id.

    The native goal is never stored.
    """
    if _valid_issue_ref(issue) is not None:
        return issue
    return _valid_request_id(request_id)


def _sanitize_usage(usage_text) -> str | None:
    """Numeric-only usage projection, never raw JSON.

    Keeps only validated integer counters (and opencode message totals),
    dropping arbitrary members such as ``source``. Returns a new JSON
    string or NULL when nothing valid remains.
    """
    obj = _parse_json_object(usage_text)
    if obj is None:
        return None
    out: dict = {}
    for key in ("input_tokens", "cached_input_tokens",
                "cache_write_input_tokens", "output_tokens",
                "reasoning_output_tokens"):
        value = obj.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            out[key] = value
    messages = obj.get("messages")
    if isinstance(messages, list):
        projected = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            tokens = message.get("tokens")
            if not isinstance(tokens, dict):
                continue
            total = tokens.get("total")
            if isinstance(total, bool):
                continue
            if isinstance(total, int):
                projected.append({"tokens": {"total": total}})
            elif isinstance(total, float) and math.isfinite(total):
                projected.append({"tokens": {"total": total}})
        if projected:
            out["messages"] = projected
    if not out:
        return None
    try:
        return json.dumps(out, sort_keys=True)
    except (TypeError, ValueError):
        return None


def _project_from_workspace(workspace) -> str | None:
    """Parent directory of the workspace's git common dir, read-only.

    Real workspaces are git worktrees, so --show-toplevel names the
    worktree while the common dir points at the main repository whose
    parent directory names the project.
    """
    if not workspace or not os.path.isdir(str(workspace)):
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=False, timeout=10)
    except (OSError, ValueError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    common = proc.stdout.strip()
    if not os.path.isabs(common):
        common = os.path.join(str(workspace), common)
    parent = os.path.dirname(os.path.abspath(common))
    name = os.path.basename(parent)
    if not name or "<" in name or any(ch.isspace() for ch in name):
        return None
    return name or None


def _upsert_changed(con: sqlite3.Connection, table: str, key: dict,
                    values: dict) -> bool:
    """INSERT or in-place UPDATE by natural key; True when rows changed.

    Updates never delete, so foreign-keyed workload rows survive re-import.
    """
    where = " AND ".join(f"{col}=?" for col in key)
    row = con.execute(
        f"SELECT * FROM {table} WHERE {where}",
        tuple(key.values())).fetchone()
    if row is None:
        cols = list(key) + list(values)
        con.execute(
            f"INSERT INTO {table}({','.join(cols)})"
            f" VALUES({','.join('?' for _ in cols)})",
            tuple(key[c] for c in key) + tuple(values[c] for c in values))
        return True
    diff = {c: v for c, v in values.items() if row[c] != v}
    if not diff:
        return False
    con.execute(
        f"UPDATE {table} SET {','.join(f'{c}=?' for c in diff)}"
        f" WHERE {where}",
        tuple(diff.values()) + tuple(key.values()))
    return True


def _import_job(con: sqlite3.Connection, db_path: str, job: dict,
                totals: dict) -> bool:
    request_id = _valid_request_id(job.get("request_id"))
    if request_id is None:
        _record_malformed(
            con, db_path, "missing_id", _shape_excerpt(job), totals)
        return False
    task = _parse_json_object(job.get("task_json"))
    if job.get("task_json") and task is None:
        _record_malformed(
            con, db_path, "malformed_json",
            _shape_excerpt(job), totals)
    raw_issue = task.get("issue") if task else None
    issue = _valid_issue_ref(raw_issue)
    # The native goal is never stored; the title is the validated issue
    # reference when available, otherwise the validated request id.
    title = _task_title(raw_issue, request_id)
    status = _valid_job_status(job.get("status"))
    lane = job.get("lane") if job.get("lane") in LANES else None
    job_kind = job.get("job_kind") if job.get("job_kind") in JOB_KINDS else None
    replay_of = _valid_request_id(job.get("replay_of"))
    workspace = _valid_path(job.get("workspace"))
    planner_session_id = _valid_id_token(job.get("planner_session_id"))
    planner_model = _valid_model_name(job.get("planner_model"))
    planner_harness = (job.get("planner_harness")
                       if job.get("planner_harness") in PLANNER_HARNESSES
                       else None)
    base_commit = _valid_commit_sha(job.get("base_commit"))
    head_commit = _valid_commit_sha(job.get("head_commit"))
    # Explicit cancellation intent lives only on the job's
    # cancel_requested flag. A missing column (legacy ledger) or a
    # non-integer stays NULL, which reports as unknown intent, never
    # intentional. cancel_requested=1 is explicit intent; 0 is no
    # request; 2 is a timeout drain that finalizes as failed, not an
    # intentional cancellation.
    cancel_requested = _valid_cancel_requested(job.get("cancel_requested"))
    values = {
        "status": status,
        "lane": lane,
        "job_kind": job_kind,
        "replay_of": replay_of,
        "workspace": workspace,
        "issue": issue,
        "planner_session_id": planner_session_id,
        "planner_model": planner_model,
        "planner_harness": planner_harness,
        "base_commit": base_commit,
        "head_commit": head_commit,
        "block_reason": _block_class(job.get("block_reason")),
        "created_at": iso_ts(job.get("created_at")),
        "updated_at": iso_ts(job.get("updated_at")),
        "cancel_requested": cancel_requested,
    }
    changed = _upsert_changed(con, "router_jobs",
                              {"request_id": request_id}, values)
    task_id = _job_task_id(job)
    explicit = task_id != f"router:{request_id}"
    changed = _upsert_task(
        con, request_id, issue, title, workspace,
        job.get("created_at"), task_id=task_id, preserve=explicit) or changed
    # A combined task has several job states; no last-job status is its outcome.
    changed = _upsert_outcome(con, None if explicit else status, task_id) or changed
    return changed


def _upsert_task(con: sqlite3.Connection, request_id: str, issue,
                 title, workspace, created_at, task_id=None,
                 preserve=False) -> bool:
    task_id = task_id or f"router:{request_id}"
    project = _project_from_workspace(workspace)
    row = con.execute(
        "SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO tasks(task_id, project, family, title, issue_url,"
            " origin, created_at) VALUES(?,?,?,?,?,?,?)",
            (task_id, project, None, title, issue, "router",
             iso_ts(created_at) or db.now()))
        return True
    if preserve or row["origin"] != "router":
        return False
    updates = {}
    if project is not None and row["project"] != project:
        updates["project"] = project
    if title is not None and row["title"] != title:
        updates["title"] = title
    if issue is not None and row["issue_url"] != issue:
        updates["issue_url"] = issue
    if not updates:
        return False
    con.execute(
        f"UPDATE tasks SET {','.join(f'{c}=?' for c in updates)}"
        " WHERE task_id=?",
        tuple(updates.values()) + (task_id,))
    return True


def _ensure_provenance_table(con: sqlite3.Connection) -> None:
    """Adapter-local durable router provenance for outcomes.

    Created here so agent_observer/db.py stays untouched. One row per
    task the router created, holding the last router-written snapshot.
    Fail closed: no row means the outcome is not router-owned.
    """
    con.execute(
        "CREATE TABLE IF NOT EXISTS router_outcome_provenance("
        " task_id TEXT PRIMARY KEY,"
        " acceptance_state TEXT, candidate TEXT, proof_ref TEXT,"
        " repairs TEXT, corrections TEXT)")


def _upsert_outcome(con: sqlite3.Connection, status, task_id: str) -> bool:
    """Every job holds an unknown outcome; human acceptance is never set.

    A repairs ``router_status:`` prefix alone is not provenance. Only a
    matching ``router_outcome_provenance`` snapshot marks a router-owned
    row that may advance. Every other pre-existing row, including a blank
    human unknown outcome and a human row whose repairs collide with the
    prefix, survives re-import byte-for-byte.
    """
    evidence = f"router_status:{status}" if status else None
    _ensure_provenance_table(con)
    row = con.execute(
        "SELECT * FROM outcomes WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO outcomes(task_id, candidate, proof_ref,"
            " acceptance_state, repairs, corrections, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (task_id, None, None, "unknown", evidence, None, db.now()))
        con.execute(
            "INSERT OR REPLACE INTO router_outcome_provenance(task_id,"
            " acceptance_state, candidate, proof_ref, repairs, corrections)"
            " VALUES(?,?,?,?,?,?)",
            (task_id, "unknown", None, None, evidence, None))
        return True
    prov = con.execute(
        "SELECT * FROM router_outcome_provenance WHERE task_id=?",
        (task_id,)).fetchone()
    if prov is None:
        # No durable router mark: a pre-existing human row stays untouched.
        return False
    current = (row["acceptance_state"], row["candidate"], row["proof_ref"],
               row["repairs"], row["corrections"])
    marked = (prov["acceptance_state"], prov["candidate"], prov["proof_ref"],
              prov["repairs"], prov["corrections"])
    if tuple(current) != tuple(marked):
        # A human touched the row after the router snapshot: preserve it.
        return False
    if row["repairs"] == evidence:
        return False
    con.execute(
        "UPDATE outcomes SET repairs=?, updated_at=? WHERE task_id=?",
        (evidence, db.now(), task_id))
    con.execute(
        "UPDATE router_outcome_provenance SET acceptance_state=?,"
        " candidate=?, proof_ref=?, repairs=?, corrections=?"
        " WHERE task_id=?",
        ("unknown", None, None, evidence, None, task_id))
    return True


def _attempt_state(raw_terminal_class) -> str:
    """Map the raw ledger terminal class onto the Observer attempt state.

    Takes the raw value (not the validated projection) so an absent
    class (NULL) stays distinguishable from an unrecognized one: only a
    genuinely absent terminal class is "active"; every valid finished
    class maps to its non-active state; any other non-NULL value,
    including a wrong-typed one, is the explicit "unknown" state, never
    active.
    """
    if raw_terminal_class is None:
        return "active"
    if not isinstance(raw_terminal_class, str):
        return "unknown"
    if raw_terminal_class == "completed":
        return "complete"
    if raw_terminal_class in ("cancelled", "crashed"):
        return raw_terminal_class
    if raw_terminal_class == "quota":
        return "quota_blocked"
    if raw_terminal_class in TERMINAL_CLASSES:
        return "failed"
    return "unknown"


def _import_invocation(con: sqlite3.Connection, db_path: str, inv: dict,
                       totals: dict, task_id: str | None = None) -> bool:
    invocation_id = _valid_id_token(inv.get("invocation_id"))
    if invocation_id is None:
        _record_malformed(
            con, db_path, "missing_id", _shape_excerpt(inv), totals)
        return False
    request_id = _valid_request_id(inv.get("request_id"))
    if request_id is None:
        _record_malformed(
            con, db_path, "missing_id", _shape_excerpt(inv), totals)
        return False
    if not con.execute("SELECT 1 FROM router_jobs WHERE request_id=?",
                       (request_id,)).fetchone():
        # Dangling parent reference: quarantine, never a FK failure.
        _record_malformed(
            con, db_path, "missing_id", _shape_excerpt(inv), totals)
        return False
    for column in ("usage_json", "native_ids_json"):
        _valid_json_object(con, db_path, inv, column, totals)
    # Fail closed: raw JSON never lands in non-exempt tables. Usage keeps
    # a new numeric-only projection; native ids, skills and tools stay
    # NULL (native ids are parsed in memory only for session binding).
    usage_projected = _sanitize_usage(inv.get("usage_json"))
    kind = inv.get("kind") if inv.get("kind") in INVOCATION_KINDS else None
    stage = inv.get("stage") if inv.get("stage") in STAGES else None
    requested_route = _valid_model_name(inv.get("requested_route"))
    policy_version = _valid_model_name(inv.get("policy_version"), 64)
    # Proof rows carry their proof class in reason and in meta_json;
    # the launch-reason vocabulary never classifies them. A proof
    # reason is projected as proof_class with reason NULL, so Router
    # reason never classifies a later failure.
    raw_reason = inv.get("reason")
    meta_seq = _meta_seq(inv.get("meta_json"))
    meta_proof = _meta_proof_class(inv.get("meta_json"))
    if kind == "proof":
        proof_class = _valid_proof_class(raw_reason)
        if proof_class is None:
            proof_class = meta_proof
        reason = None
    else:
        reason = _valid_reason(raw_reason)
        proof_class = meta_proof
        # Non-proof rows never carry a proof class through reason;
        # a meta proof_class on a non-proof row is ignored to keep
        # stage evidence explicit (unknown stays unknown).
        if kind != "proof":
            proof_class = None
    rc = _valid_rc(inv.get("rc"))
    terminal_class = _valid_terminal_class(inv.get("terminal_class"))
    harness_version = _valid_model_name(inv.get("harness_version"), 64)
    observed_model = _valid_model_name(inv.get("observed_model"))
    observed_variant = _valid_model_name(inv.get("observed_variant"), 64)
    elapsed_secs = _valid_number(inv.get("elapsed_secs"))
    session_id = _valid_id_token(inv.get("session_id"))
    session_kind = (inv.get("session_kind")
                    if inv.get("session_kind") in SESSION_KINDS else None)
    kit = _valid_path(inv.get("kit"))
    kit_hash = _valid_content_hash(inv.get("kit_hash"))
    direction_supply = _valid_direction_supply(inv.get("direction_supply"))
    direction_hash = _valid_content_hash(inv.get("direction_hash"))
    schema_raw = inv.get("schema_version")
    schema_version = (schema_raw if isinstance(schema_raw, int)
                      and not isinstance(schema_raw, bool) else None)
    values = {
        "request_id": request_id,
        "kind": kind,
        "stage": stage,
        "requested_route": requested_route,
        "policy_version": policy_version,
        "reason": reason,
        "terminal_class": terminal_class,
        "harness_version": harness_version,
        "observed_model": observed_model,
        "observed_variant": observed_variant,
        "elapsed_secs": elapsed_secs,
        "usage_json": usage_projected,
        "native_ids_json": None,
        "session_id": session_id,
        "session_kind": session_kind,
        "started_at": iso_ts(inv.get("started_at")),
        "ended_at": iso_ts(inv.get("ended_at")),
        "kit": kit,
        "kit_hash": kit_hash,
        "direction_supply": direction_supply,
        "direction_hash": direction_hash,
        "skills_json": None,
        "tools_json": None,
        "schema_version": schema_version,
        "rc": rc,
        "meta_seq": meta_seq,
        "proof_class": proof_class,
    }
    changed = _upsert_changed(con, "router_invocations",
                              {"invocation_id": invocation_id},
                              values)
    task_id = task_id or f"router:{request_id}"
    turn_id = f"router:{invocation_id}"
    if task_id != f"router:{request_id}":
        # Replace this invocation's old adapter-owned projection after upgrade.
        con.execute("DELETE FROM attempts WHERE task_id=? AND turn_id=?",
                    (f"router:{request_id}", turn_id))
    attempt = {
        "role": kind or "unknown",
        "harness": HARNESS,
        "session_key": _session_key_for(inv),
        "stage": stage,
        "route_requested": requested_route,
        "policy_version": policy_version,
        "reason": reason,
        "model_observed": observed_model,
        "effort_observed": observed_variant,
        "started_at": iso_ts(inv.get("started_at")),
        "ended_at": iso_ts(inv.get("ended_at")),
        "elapsed_s": elapsed_secs,
        # The state reads the raw ledger class (absent stays active,
        # unrecognized stays unknown) while the stored column keeps the
        # validated closed projection.
        "state": _attempt_state(inv.get("terminal_class")),
        "terminal_class": terminal_class,
        "usage_json": usage_projected,
        "rc": rc,
        "proof_class": proof_class,
        "meta_seq": meta_seq,
    }
    row = con.execute(
        "SELECT * FROM attempts WHERE task_id=? AND turn_id=?",
        (task_id, turn_id)).fetchone()
    if row is None:
        cols = ["task_id", "turn_id"] + list(attempt)
        con.execute(
            f"INSERT INTO attempts({','.join(cols)})"
            f" VALUES({','.join('?' for _ in cols)})",
            (task_id, turn_id) + tuple(attempt[c] for c in attempt))
        return True
    diff = {c: v for c, v in attempt.items() if row[c] != v}
    if diff:
        con.execute(
            f"UPDATE attempts SET {','.join(f'{c}=?' for c in diff)}"
            " WHERE task_id=? AND turn_id=?",
            tuple(diff.values()) + (task_id, turn_id))
        return True
    return changed


def _thread_id(native_ids_text) -> str | None:
    obj = _parse_json_object(native_ids_text)
    if not obj:
        return None
    thread = obj.get("thread_id")
    return _valid_id_token(thread)


def _session_key_for(inv: dict) -> str | None:
    """Native session key for the worker session, never the planner's."""
    kind = inv.get("session_kind")
    if kind not in SESSION_KINDS:
        return None
    candidate: str | None = None
    if kind == "opencode_session_id":
        sid = _valid_id_token(inv.get("session_id"))
        candidate = f"opencode:{sid}" if sid else None
    elif kind == "codex_task_id":
        native = _thread_id(inv.get("native_ids_json")) or _valid_id_token(
            inv.get("session_id"))
        candidate = f"codex:{native}" if native else None
    elif kind == "grok_session_id":
        sid = _valid_id_token(inv.get("session_id"))
        candidate = f"grok:{sid}" if sid else None
    elif kind == "claude_session_id":
        sid = _valid_id_token(inv.get("session_id"))
        candidate = f"claude:{sid}" if sid else None
    else:
        return None
    if candidate is None:
        return None
    # Targets follow privacy rule 6 through the shared module: only a
    # bounded string survives, then the harness-prefixed shape is enforced.
    safe = privacy.filter_target(candidate)
    if safe != candidate:
        return None
    if "<" in candidate or "\n" in candidate:
        return None
    return candidate


def _binding_key(con: sqlite3.Connection, inv: dict,
                 task_id: str | None = None) -> str | None:
    """Bind the worker session; the shared planner session stays unbound."""
    key = _session_key_for(inv)
    if key is None:
        return None
    request_id = _valid_request_id(inv.get("request_id"))
    invocation_id = _valid_id_token(inv.get("invocation_id"))
    if request_id is None or invocation_id is None:
        return None
    task_id = task_id or f"router:{request_id}"
    if not con.execute("SELECT 1 FROM tasks WHERE task_id=?",
                       (task_id,)).fetchone():
        return None
    if con.execute(
            "SELECT 1 FROM assignments a JOIN submissions s ON s.native_id=a.submission_native_id"
            " WHERE s.session_key=? AND a.task_id<>?", (key, task_id)).fetchone():
        # Explicit submission ownership outranks a purported dedicated worker.
        # The attempt retains its expected session, exposing the unbound gap.
        con.execute("DELETE FROM session_assignments WHERE session_key=? AND task_id=? AND evidence=?",
                    (key, task_id, f"router:{request_id}:{invocation_id}"))
        return None
    evidence = f"router:{request_id}:{invocation_id}"
    if privacy.filter_target(evidence) != evidence:
        return None
    if task_id != f"router:{request_id}":
        con.execute("DELETE FROM session_assignments WHERE session_key=?"
                    " AND task_id=? AND evidence=?",
                    (key, f"router:{request_id}", evidence))
    row = con.execute(
        "SELECT evidence FROM session_assignments"
        " WHERE session_key=? AND task_id=?",
        (key, task_id)).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO session_assignments(session_key, task_id,"
            " evidence, created_at) VALUES(?,?,?,?)",
            (key, task_id, evidence, db.now()))
    elif row["evidence"] != evidence:
        con.execute(
            "UPDATE session_assignments SET evidence=?"
            " WHERE session_key=? AND task_id=?",
            (evidence, key, task_id))
    return key


def _import_reading(con: sqlite3.Connection, db_path: str, reading: dict,
                    totals: dict) -> bool:
    pool = _valid_id_token(reading.get("pool"))
    model = _valid_model_name(reading.get("model"))
    window = _valid_id_token(reading.get("window"))
    observed_raw = reading.get("observed_at")
    if (isinstance(observed_raw, str) and observed_raw.strip()
            and iso_ts(observed_raw) is not None):
        observed_at = observed_raw
    else:
        observed_at = None
    if pool is None or model is None or window is None or observed_at is None:
        _record_malformed(
            con, db_path, "missing_id", _shape_excerpt(reading), totals)
        return False
    used = _valid_number(reading.get("used"))
    limit_value = _valid_number(reading.get("limit_value"))
    reset_raw = reading.get("reset_at")
    if (isinstance(reset_raw, str) and reset_raw.strip()
            and iso_ts(reset_raw) is not None):
        reset_at = reset_raw
    else:
        reset_at = None
    source_raw = reading.get("source")
    source = privacy.filter_target(source_raw)
    if source != source_raw or source is None:
        source = None
    elif _valid_id_token(source) is None and _valid_model_name(source) is None:
        source = None
    key = {"pool": pool, "model": model,
           "window": window,
           "observed_at": observed_at}
    values = {"used": used,
              "limit_value": limit_value,
              "reset_at": reset_at,
              "source": source}
    return _upsert_changed(con, "router_readings", key, values)


def _project_router_event(event: dict) -> dict | None:
    """Sanitized whitelisted projection for one Router ledger event.

    Only the closed kinds in EVENT_KINDS are retained, and only their
    whitelisted numeric/enum/route fields. Raw prompts, tool arguments,
    question text, secrets and arbitrary payload values never persist:
    the payload is parsed in memory, validated field by field, and
    discarded. Returns the projection dict or None when the event
    carries nothing retainable (unknown kind, legacy value, or a
    question_posted for another qid).
    """
    kind = _valid_event_kind(event.get("kind"))
    if kind is None:
        return None
    payload = _parse_json_object(event.get("payload_json")) or {}
    # Privacy allowlist still runs, result discarded: no raw payload
    # value reaches the ledger through another path.
    _ = privacy.filter_detail(event.get("kind") or "", payload)
    _ = privacy.filter_target(event.get("kind"))
    ts = iso_ts(event.get("ts"))
    if kind == "recovery_decision":
        failed_seq = _valid_seq(payload.get("failed_seq"))
        rung = _valid_recovery_rung(payload.get("rung"))
        target = _valid_model_name(payload.get("target")) if payload.get("target") is not None else None
        reason = _valid_recovery_reason(payload.get("reason"))
        failures = payload.get("failures")
        failures = failures if isinstance(failures, int) and not isinstance(failures, bool) else None
        if failed_seq is None and rung is None and target is None and reason is None and failures is None:
            return None
        return {"kind": kind, "ts": ts, "failed_seq": failed_seq,
                "next_seq": None, "seq": None, "route": None,
                "outcome": None, "scope": None, "rung": rung,
                "target": target, "reason": reason, "requested": None,
                "qid": None, "failures": failures}
    if kind == "recovery_next_attempt":
        failed_seq = _valid_seq(payload.get("failed_seq"))
        next_seq = _valid_seq(payload.get("next_seq"))
        route = _valid_model_name(payload.get("route")) if payload.get("route") is not None else None
        if failed_seq is None and next_seq is None and route is None:
            return None
        return {"kind": kind, "ts": ts, "failed_seq": failed_seq,
                "next_seq": next_seq, "seq": None, "route": route,
                "outcome": None, "scope": None, "rung": None,
                "target": None, "reason": None, "requested": None,
                "qid": None, "failures": None}
    if kind == "recovery_attempt_result":
        failed_seq = _valid_seq(payload.get("failed_seq"))
        next_seq = _valid_seq(payload.get("next_seq"))
        outcome = _valid_recovery_outcome(payload.get("outcome"))
        if failed_seq is None and next_seq is None and outcome is None:
            return None
        return {"kind": kind, "ts": ts, "failed_seq": failed_seq,
                "next_seq": next_seq, "seq": None, "route": None,
                "outcome": outcome, "scope": None, "rung": None,
                "target": None, "reason": None, "requested": None,
                "qid": None, "failures": None}
    if kind == "verification_attempt":
        seq = _valid_seq(payload.get("seq"))
        route = _valid_model_name(payload.get("route")) if payload.get("route") is not None else None
        rc = _valid_rc(payload.get("rc"))
        proof_class = _valid_proof_class(payload.get("proof_class"))
        if seq is None and route is None and rc is None and proof_class is None:
            return None
        return {"kind": kind, "ts": ts, "failed_seq": None,
                "next_seq": None, "seq": seq, "route": route,
                "outcome": None, "scope": None, "rung": None,
                "target": None, "reason": None, "requested": None,
                "qid": None, "failures": None,
                "_rc": rc, "_proof_class": proof_class}
    if kind == "route_switched":
        scope = _valid_route_scope(payload.get("scope"))
        # From/to are validated routes when present; reason stays in
        # the closed launch vocabulary so a dispatch cause never
        # disguises a later worker attempt.
        from_route = _valid_model_name(payload.get("from")) if payload.get("from") is not None else None
        to_route = _valid_model_name(payload.get("to")) if payload.get("to") is not None else None
        reason = _valid_reason(payload.get("reason"))
        if scope is None and from_route is None and to_route is None and reason is None:
            return None
        # Scope is the required contract field; a switch without a
        # known scope stays unretained to avoid mixing dispatch and
        # worker causes.
        if scope is None:
            return None
        return {"kind": kind, "ts": ts, "failed_seq": None,
                "next_seq": None, "seq": None, "route": to_route,
                "outcome": None, "scope": scope, "rung": None,
                "target": to_route, "reason": reason, "requested": None,
                "qid": None, "failures": None,
                "_from": from_route}
    if kind == "planner_route_rejected":
        requested = _valid_model_name(payload.get("requested")) if payload.get("requested") is not None else None
        if requested is None:
            return None
        # The free-text reason is never stored; only the validated
        # requested route persists.
        return {"kind": kind, "ts": ts, "failed_seq": None,
                "next_seq": None, "seq": None, "route": None,
                "outcome": None, "scope": None, "rung": None,
                "target": None, "reason": None, "requested": requested,
                "qid": None, "failures": None}
    if kind == "question_posted":
        qid = event.get("qid") if isinstance(event.get("qid"), str) else payload.get("qid")
        if qid != RECOVERY_QUESTION_ID:
            return None
        return {"kind": kind, "ts": ts, "failed_seq": None,
                "next_seq": None, "seq": None, "route": None,
                "outcome": None, "scope": None, "rung": None,
                "target": None, "reason": None, "requested": None,
                "qid": qid, "failures": None}
    return None


def _store_router_event(con: sqlite3.Connection, request_id: str,
                        event: dict, projection: dict) -> bool:
    """Insert one sanitized event projection, idempotent on natural key.

    The natural key is (request_id, kind, ts, failed_seq, next_seq,
    seq): the same ledger event re-imported never duplicates. Extra
    fields (_rc, _proof_class, _from) are folded into the stored
    columns where they belong, never as raw values.
    """
    # Fold verification extras: rc is not a stored column on events;
    # the proof outcome lives on the invocation row, so only seq,
    # route and proof_class-adjacent outcome are kept. The rc itself
    # is validated but not stored as a separate event field to keep
    # the projection minimal; the invocation row carries it.
    kind = projection["kind"]
    ts = projection.get("ts")
    failed_seq = projection.get("failed_seq")
    next_seq = projection.get("next_seq")
    seq = projection.get("seq")
    # verification_attempt keeps its proof_class in reason for
    # inspection without storing raw values.
    reason = projection.get("reason")
    if kind == "verification_attempt":
        reason = projection.get("_proof_class")
    # route_switched keeps from in requested for inspection? No:
    # from is a route, but the contract asks for scope; keep target
    # as route/target and drop from to stay minimal, except when
    # needed for scope disambiguation (stored as requested is wrong).
    # Keep minimal: from is discarded, to stays as route/target.
    values = {
        "request_id": request_id,
        "kind": kind,
        "ts": ts,
        "failed_seq": failed_seq,
        "next_seq": next_seq,
        "seq": seq,
        "route": projection.get("route"),
        "outcome": projection.get("outcome"),
        "scope": projection.get("scope"),
        "rung": projection.get("rung"),
        "target": projection.get("target"),
        "reason": reason,
        "requested": projection.get("requested"),
        "qid": projection.get("qid"),
        "failures": projection.get("failures"),
    }
    # Idempotency: the same event never inserts twice. Timestamps
    # from the ledger are floats via iso_ts; NULLs compare with IS.
    existing = con.execute(
        "SELECT id FROM router_events WHERE request_id=? AND kind=?"
        " AND COALESCE(ts, -1)=COALESCE(?, -1)"
        " AND COALESCE(failed_seq, -999999)=COALESCE(?, -999999)"
        " AND COALESCE(next_seq, -999999)=COALESCE(?, -999999)"
        " AND COALESCE(seq, -999999)=COALESCE(?, -999999)",
        (request_id, kind, ts, failed_seq, next_seq, seq)).fetchone()
    if existing is not None:
        # Fill unknown fields in place without duplicating.
        row = con.execute(
            "SELECT * FROM router_events WHERE id=?", (existing["id"],)).fetchone()
        diff = {c: v for c, v in values.items()
                if c not in ("request_id", "kind") and row[c] != v and v is not None and row[c] is None}
        if diff:
            con.execute(
                f"UPDATE router_events SET {','.join(f'{c}=?' for c in diff)} WHERE id=?",
                tuple(diff.values()) + (existing["id"],))
            return True
        # Also update when stored NULL should become a new non-NULL
        # route/outcome/scope on re-import with more evidence.
        return False
    con.execute(
        "INSERT INTO router_events(request_id, kind, ts, failed_seq,"
        " next_seq, seq, route, outcome, scope, rung, target, reason,"
        " requested, qid, failures) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (values["request_id"], values["kind"], values["ts"],
         values["failed_seq"], values["next_seq"], values["seq"],
         values["route"], values["outcome"], values["scope"],
         values["rung"], values["target"], values["reason"],
         values["requested"], values["qid"], values["failures"]))
    return True


def _validate_source_event(con: sqlite3.Connection, db_path: str,
                           event: dict, totals: dict) -> bool:
    """Fail-closed validation with whitelisted recovery projections.

    A missing or dangling request id is quarantined with ``missing_id``
    and skipped. Unknown event kinds, legacy values without retainable
    fields, and question_posted for another qid store nothing and
    return False. Whitelisted recovery, verification, route-switch,
    planner-rejection and recovery-decision question events store only
    their sanitized projection in router_events; raw payloads never
    enter the ledger.
    """
    request_id = _valid_request_id(event.get("request_id"))
    if request_id is None or not con.execute(
            "SELECT 1 FROM router_jobs WHERE request_id=?",
            (request_id,)).fetchone():
        _record_malformed(
            con, db_path, "missing_id", _shape_excerpt(event), totals)
        return False
    projection = _project_router_event(event)
    if projection is None:
        payload = _parse_json_object(event.get("payload_json"))
        _ = privacy.filter_detail(event.get("kind") or "", payload or {})
        _ = privacy.filter_target(event.get("kind"))
        return False
    return _store_router_event(con, request_id, event, projection)


def _sync_rollouts(con: sqlite3.Connection, rollout_root: str, totals: dict,
                   full: bool = False) -> bool:
    """Import router-owned Codex rollouts through the Codex adapter.

    Every rollout continues through ``_codex.import_codex_file`` with the
    full flag, so main-session classification, sanitized excerpts,
    whitelisted details and targets, fixed categories and in-place privacy
    corrections come from the core importer. No Codex privacy logic is
    duplicated here.
    """
    patterns = (
        os.path.join(rollout_root, "codex-sessions", "**", "rollout-*.jsonl"),
        os.path.join(rollout_root, "kits", "*", "sessions", "**",
                     "rollout-*.jsonl"))
    paths = set()
    for pattern in patterns:
        paths.update(glob.glob(pattern, recursive=True))
    changed = False
    seen: set[str] = set()
    for path in sorted(paths):
        try:
            canonical = os.path.realpath(path)
        except OSError:
            canonical = path
        if canonical in seen:
            continue
        seen.add(canonical)
        try:
            stats = _codex.import_codex_file(con, canonical, full=full)
        except (OSError, sqlite3.DatabaseError, UnicodeDecodeError,
                ValueError):
            totals["failed"].append(
                {"path": canonical,
                 "error": privacy.error_category("source_unreadable")})
            _record_error(con, canonical, "source_unreadable", "")
            continue
        totals["rollouts"] += 1
        totals["responses_inserted"] += stats.get("responses_inserted", 0)
        totals["events_inserted"] += stats.get("events_inserted", 0)
        totals["submissions_inserted"] += stats.get(
            "submissions_inserted", 0)
        totals["malformed"] += stats.get("malformed", 0)
        if not stats.get("unchanged"):
            changed = True
    return changed


def _router_total(usage_text, session_key: str):
    """Router-side total under the bound harness's own semantics."""
    safe_key = privacy.filter_target(session_key)
    if safe_key != session_key:
        return None
    try:
        usage = json.loads(usage_text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(usage, dict):
        return None
    if session_key.startswith("codex:"):
        # Flat Codex shape: cached and reasoning tokens are subsets,
        # so the total is input plus output only.
        inputs = usage.get("input_tokens")
        outputs = usage.get("output_tokens")
        if (isinstance(inputs, int) and not isinstance(inputs, bool)
                and isinstance(outputs, int)
                and not isinstance(outputs, bool)):
            return inputs + outputs
        return None
    if session_key.startswith("opencode:"):
        messages = usage.get("messages")
        if not isinstance(messages, list) or not messages:
            return None
        total = 0
        for message in messages:
            tokens = message.get("tokens") if isinstance(
                message, dict) else None
            value = tokens.get("total") if isinstance(
                tokens, dict) else None
            if isinstance(value, bool):
                return None
            if isinstance(value, int):
                total += value
            elif isinstance(value, float) and math.isfinite(value):
                total += value
            else:
                return None
        return total
    return None


def _reconcile_usage(con: sqlite3.Connection, db_path: str,
                     invocations: list[dict], bound: dict[str, str],
                     totals: dict) -> None:
    """Record a usage_conflict when aggregate router differs from native.

    Router totals are summed per session_key across all bound invocations,
    then compared once with the summed native response total for that
    session. One invocation is never compared against a whole shared
    session, so Codex resumes sharing a session raise no false mismatch.
    """
    router_sums: dict[str, float] = {}
    session_shapes: dict[str, set] = {}
    for inv in invocations:
        invocation_id = _valid_id_token(inv.get("invocation_id"))
        session_key = bound.get(invocation_id) if invocation_id else None
        usage_text = inv.get("usage_json")
        if not session_key or not usage_text:
            continue
        if privacy.filter_target(session_key) != session_key:
            continue
        if not (session_key.startswith("codex:")
                or session_key.startswith("opencode:")):
            continue
        router_total = _router_total(usage_text, session_key)
        if router_total is None:
            continue
        router_sums[session_key] = router_sums.get(session_key, 0) + router_total
        shape = session_shapes.setdefault(session_key, set())
        if isinstance(inv, dict):
            shape.update(str(k) for k in inv.keys())
    for session_key, router_sum in router_sums.items():
        if not con.execute("SELECT 1 FROM sessions WHERE session_key=?",
                           (session_key,)).fetchone():
            continue
        row = con.execute(
            "SELECT SUM(total_tokens) AS total FROM responses"
            " WHERE session_key=?", (session_key,)).fetchone()
        native_total = row["total"] if row else None
        if native_total is None:
            continue
        if router_sum != native_total:
            raw = json.dumps(
                {k: None for k in sorted(session_shapes.get(session_key, ()))})
            excerpt = privacy.line_excerpt(raw)[:200]
            _record_malformed(
                con, db_path, "usage_conflict", excerpt, totals)
