"""Model Router ledger adapter: router jobs into the workload ledger.

Reads Model Router's ``jobs.db`` read-only (SQLite URI ``mode=ro``) and
copies jobs, invocations and readings into the Observer ``router_*``
tables, then maps that ledger onto the existing workload tables (tasks,
attempts, outcomes, session_assignments). Router-owned Codex rollouts
under the state directory are imported through the Codex adapter so
native usage and events land under the same session keys.

Counter rule: the router's ``usage_json`` is ledger evidence kept on the
attempt row only. It is never added to ``responses`` and never summed
with native counters; a cross-check against the native total for the
bound session is recorded in ``import_errors`` when the two disagree.

Stdlib only. Never writes the router database or rollout files.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import subprocess
import urllib.parse

from .. import db
from ..ingest import iso_ts
from . import codex as _codex

HARNESS = "router"
ROUTER_SCHEMA_VERSION = 2
DEFAULT_ROOT = os.path.expanduser("~/.local/state/model-router")

CAPABILITIES = [
    ("router_ledger", True, "router jobs, invocations and readings copied read-only; usage_json stays evidence on attempts, never native responses"),
    ("tool_calls", False, "no router tool-call import; native rollouts carry tool evidence"),
    ("tool_results", False, "no router tool-result import; native rollouts carry tool evidence"),
    ("read_evidence", False, "no file-read import from the router ledger"),
    ("lifecycle_task", True, "invocation terminal_class maps onto attempt state; router job status stays outcome evidence only"),
    ("human_input", False, "no submissions are synthesized from router rows"),
    ("instruction_identity", False, "no instruction identity is inferred from router rows"),
    ("workload_binding", True, "jobs map to tasks and invocations to attempts; worker sessions bind through session_assignments"),
]

_DIRECTION_RE = re.compile(
    r"<<<AGENTSMD_PROJECT_DIRECTION_V1>>>.*?<<<END_AGENTSMD_PROJECT_DIRECTION_V1>>>",
    re.S)

def discover(root: str | None = None) -> list[str]:
    db_path, _ = _resolve_paths(root, None)
    if db_path and os.path.isfile(db_path):
        return [db_path]
    return []


def sync(con: sqlite3.Connection, root: str | None = None,
         full: bool = False, source: str | None = None) -> dict:
    totals = {"harness": HARNESS, "sources": 0, "unchanged": 0,
              "responses_inserted": 0, "events_inserted": 0,
              "submissions_inserted": 0, "malformed": 0, "failed": [],
              "jobs": 0, "invocations": 0, "bindings": 0, "readings": 0,
              "rollouts": 0}
    db_path, rollout_root = _resolve_paths(root, source)
    ledger_changed = False
    pending_reconcile = None
    if db_path is None or not os.path.isfile(db_path):
        totals["failed"].append({
            "path": db_path or source or "",
            "error": "jobs_db_missing"})
    else:
        try:
            changed, invocations, bound = _sync_ledger(con, db_path, totals)
        except sqlite3.DatabaseError:
            totals["failed"].append({
                "path": db_path, "error": "jobs_db_unreadable"})
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


def _open_ro(path: str) -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro"
    src = sqlite3.connect(uri, uri=True)
    src.row_factory = sqlite3.Row
    src.execute("PRAGMA query_only=ON")
    return src


def _record_error(con: sqlite3.Connection, source_path: str, error: str,
                  excerpt: str = "") -> None:
    """Quarantine a sanitized category plus row shape, never row content.

    Re-imports of the same evidence are no-ops so errors never accumulate.
    """
    error, excerpt = error[:200], excerpt[:200]
    if con.execute(
            "SELECT 1 FROM import_errors WHERE harness=? AND source_path=?"
            " AND error=? AND COALESCE(line_excerpt, '')=COALESCE(?, '')",
            (HARNESS, source_path, error, excerpt)).fetchone():
        return False
    con.execute(
        "INSERT INTO import_errors(harness, source_path, ordinal_num, error,"
        " line_excerpt, created_at) VALUES(?,?,?,?,?,?)",
        (HARNESS, source_path, None, error, excerpt, db.now()))
    return True


def _record_malformed(con: sqlite3.Connection, source_path: str, error: str,
                      excerpt: str, totals: dict) -> None:
    """Count a quarantine only when its row is newly inserted."""
    if _record_error(con, source_path, error, excerpt):
        totals["malformed"] += 1


def _shape(table: str, columns: tuple | list, native_id) -> str:
    """Row shape for quarantine excerpts: table, native id, column names.

    The native id follows the table so both survive the 200-character
    excerpt cap even for wide source tables.
    """
    return f"{table} id={native_id} ({','.join(columns)})"


def _sync_ledger(con: sqlite3.Connection, db_path: str, totals: dict
                 ) -> tuple:
    """Validate the guard, copy the ledger, map workload.

    Returns (changed, invocations, bound) so the caller can reconcile
    usage after the router-owned rollouts are imported.
    """
    src = _open_ro(db_path)
    try:
        guard = _check_schema_version(src)
        if guard is not None:
            error, table, id_col, native_id = guard
            totals["failed"].append({"path": db_path, "error": error})
            _record_error(
                con, db_path, error,
                f"{table} id={native_id} columns({id_col},schema_version)")
            return False, [], {}
        changed = False
        jobs = [dict(r) for r in src.execute("SELECT * FROM jobs")]
        invocations = [dict(r) for r in
                       src.execute("SELECT * FROM invocations")]
        readings = [dict(r) for r in src.execute("SELECT * FROM readings")]
        for job in jobs:
            if _import_job(con, db_path, job, totals):
                changed = True
        totals["jobs"] = len(jobs)
        bound: dict[str, str] = {}
        for inv in invocations:
            if _import_invocation(con, db_path, inv, totals):
                changed = True
            key = _binding_key(con, inv)
            if key is not None:
                bound[inv["invocation_id"]] = key
        totals["invocations"] = len(invocations)
        totals["bindings"] = len(bound)
        for reading in readings:
            if _import_reading(con, reading):
                changed = True
        totals["readings"] = len(readings)
        return changed, invocations, bound
    finally:
        src.close()


def _check_schema_version(src: sqlite3.Connection) -> tuple | None:
    """Refuse the ledger unless invocations and events are all version 2.

    Returns (error, table, id column, native id) so the quarantine row
    carries a category plus the row's shape, never row content. The full
    source column list cannot fit the 200-character excerpt cap, so the
    shape names the guard-relevant source columns.
    """
    for table, id_col in (("invocations", "invocation_id"), ("events", "id")):
        cols = {r["name"] for r in
                src.execute(f"PRAGMA table_info({table})").fetchall()}
        if "schema_version" not in cols:
            return (f"schema_guard: {table} has no schema_version",
                    table, id_col, None)
        bad = src.execute(
            f"SELECT {id_col}, schema_version FROM {table}"
            f" WHERE schema_version IS NOT {ROUTER_SCHEMA_VERSION}"
            " LIMIT 1").fetchone()
        if bad is not None:
            return (f"schema_guard: {table} {bad[0]}"
                    f" schema_version={bad[1]}",
                    table, id_col, bad[0])
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
        _record_malformed(
            con, db_path, f"malformed_{column}",
            _shape("invocations", ("invocation_id", column),
                   inv.get("invocation_id")), totals)


def _block_class(block_reason) -> str | None:
    """Only the class token before the first colon; detail never stored."""
    if not block_reason:
        return None
    head = str(block_reason).split(":", 1)[0].strip()
    if not head:
        return None
    return head.split()[0]


def _task_title(goal) -> str | None:
    if not isinstance(goal, str) or not goal.strip():
        return None
    clean = _DIRECTION_RE.sub("", goal).strip()
    if not clean:
        return None
    return clean[:120]


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
    task = _parse_json_object(job.get("task_json"))
    if job.get("task_json") and task is None:
        _record_malformed(
            con, db_path, "malformed_task_json",
            _shape("jobs", ("request_id", "task_json"),
                   job.get("request_id")), totals)
    issue = task.get("issue") if task else None
    goal = task.get("goal") if task else None
    if not isinstance(issue, str):
        issue = None
    values = {
        "status": job.get("status"),
        "lane": job.get("lane"),
        "job_kind": job.get("job_kind"),
        "replay_of": job.get("replay_of"),
        "workspace": job.get("workspace"),
        "issue": issue,
        "planner_session_id": job.get("planner_session_id"),
        "planner_model": job.get("planner_model"),
        "planner_harness": job.get("planner_harness"),
        "base_commit": job.get("base_commit"),
        "head_commit": job.get("head_commit"),
        "block_reason": _block_class(job.get("block_reason")),
        "created_at": iso_ts(job.get("created_at")),
        "updated_at": iso_ts(job.get("updated_at")),
    }
    changed = _upsert_changed(con, "router_jobs",
                              {"request_id": job["request_id"]}, values)
    changed = _upsert_task(con, job, issue, goal) or changed
    changed = _upsert_outcome(con, job.get("status"),
                              f"router:{job['request_id']}") or changed
    return changed


def _upsert_task(con: sqlite3.Connection, job: dict, issue,
                 goal) -> bool:
    task_id = f"router:{job['request_id']}"
    project = _project_from_workspace(job.get("workspace"))
    title = _task_title(goal)
    row = con.execute(
        "SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO tasks(task_id, project, family, title, issue_url,"
            " origin, created_at) VALUES(?,?,?,?,?,?,?)",
            (task_id, project, None, title, issue, "router",
             iso_ts(job.get("created_at")) or db.now()))
        return True
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


def _upsert_outcome(con: sqlite3.Connection, status, task_id: str) -> bool:
    """Every job holds an unknown outcome; human acceptance is never set."""
    evidence = f"router_status:{status}" if status else None
    row = con.execute(
        "SELECT * FROM outcomes WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO outcomes(task_id, candidate, proof_ref,"
            " acceptance_state, repairs, corrections, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (task_id, None, None, "unknown", evidence, None, db.now()))
        return True
    if row["acceptance_state"] != "unknown":
        # A human-recorded outcome and its fields survive re-import.
        return False
    if row["repairs"] == evidence:
        return False
    con.execute(
        "UPDATE outcomes SET repairs=?, updated_at=? WHERE task_id=?",
        (evidence, db.now(), task_id))
    return True


def _attempt_state(terminal_class) -> str:
    if terminal_class is None:
        return "active"
    if terminal_class == "completed":
        return "complete"
    if terminal_class in ("cancelled", "crashed"):
        return terminal_class
    if terminal_class == "quota":
        return "quota_blocked"
    return "failed"


def _import_invocation(con: sqlite3.Connection, db_path: str, inv: dict,
                       totals: dict) -> bool:
    for column in ("usage_json", "native_ids_json"):
        _valid_json_object(con, db_path, inv, column, totals)
    values = {
        "request_id": inv.get("request_id"),
        "kind": inv.get("kind"),
        "stage": inv.get("stage"),
        "requested_route": inv.get("requested_route"),
        "policy_version": inv.get("policy_version"),
        "reason": inv.get("reason"),
        "terminal_class": inv.get("terminal_class"),
        "harness_version": inv.get("harness_version"),
        "observed_model": inv.get("observed_model"),
        "observed_variant": inv.get("observed_variant"),
        "elapsed_secs": inv.get("elapsed_secs"),
        "usage_json": inv.get("usage_json"),
        "native_ids_json": inv.get("native_ids_json"),
        "session_id": inv.get("session_id"),
        "session_kind": inv.get("session_kind"),
        "started_at": iso_ts(inv.get("started_at")),
        "ended_at": iso_ts(inv.get("ended_at")),
        "kit": inv.get("kit"),
        "kit_hash": inv.get("kit_hash"),
        "direction_supply": inv.get("direction_supply"),
        "direction_hash": inv.get("direction_hash"),
        "skills_json": inv.get("skills_json"),
        "tools_json": inv.get("tools_json"),
        "schema_version": inv.get("schema_version"),
    }
    changed = _upsert_changed(con, "router_invocations",
                              {"invocation_id": inv["invocation_id"]},
                              values)
    task_id = f"router:{inv.get('request_id')}"
    turn_id = f"router:{inv.get('invocation_id')}"
    attempt = {
        "role": inv.get("kind"),
        "harness": HARNESS,
        "session_key": _session_key_for(inv),
        "stage": inv.get("stage"),
        "route_requested": inv.get("requested_route"),
        "policy_version": inv.get("policy_version"),
        "reason": inv.get("reason"),
        "model_observed": inv.get("observed_model"),
        "effort_observed": inv.get("observed_variant"),
        "started_at": iso_ts(inv.get("started_at")),
        "ended_at": iso_ts(inv.get("ended_at")),
        "elapsed_s": inv.get("elapsed_secs"),
        "state": _attempt_state(inv.get("terminal_class")),
        "terminal_class": inv.get("terminal_class"),
        "usage_json": inv.get("usage_json"),
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
    return thread if isinstance(thread, str) and thread else None


def _session_key_for(inv: dict) -> str | None:
    """Native session key for the worker session, never the planner's."""
    kind = inv.get("session_kind")
    if kind == "opencode_session_id":
        sid = inv.get("session_id")
        return f"opencode:{sid}" if sid else None
    if kind == "codex_task_id":
        native = _thread_id(inv.get("native_ids_json")) or inv.get(
            "session_id")
        return f"codex:{native}" if native else None
    if kind == "grok_session_id":
        sid = inv.get("session_id")
        return f"grok:{sid}" if sid else None
    return None


def _binding_key(con: sqlite3.Connection, inv: dict) -> str | None:
    """Bind the worker session; the shared planner session stays unbound."""
    key = _session_key_for(inv)
    if key is None:
        return None
    task_id = f"router:{inv.get('request_id')}"
    evidence = f"router:{inv.get('request_id')}:{inv.get('invocation_id')}"
    con.execute(
        "INSERT OR IGNORE INTO session_assignments(session_key, task_id,"
        " evidence, created_at) VALUES(?,?,?,?)",
        (key, task_id, evidence, db.now()))
    return key


def _import_reading(con: sqlite3.Connection, reading: dict) -> bool:
    key = {"pool": reading.get("pool"), "model": reading.get("model"),
           "window": reading.get("window"),
           "observed_at": reading.get("observed_at")}
    values = {"used": reading.get("used"),
              "limit_value": reading.get("limit_value"),
              "reset_at": reading.get("reset_at"),
              "source": reading.get("source")}
    return _upsert_changed(con, "router_readings", key, values)


def _sync_rollouts(con: sqlite3.Connection, rollout_root: str, totals: dict,
                   full: bool = False) -> bool:
    """Import router-owned Codex rollouts through the Codex adapter."""
    patterns = (
        os.path.join(rollout_root, "codex-sessions", "**", "rollout-*.jsonl"),
        os.path.join(rollout_root, "kits", "*", "sessions", "**",
                     "rollout-*.jsonl"))
    paths = set()
    for pattern in patterns:
        paths.update(glob.glob(pattern, recursive=True))
    changed = False
    for path in sorted(paths):
        try:
            stats = _codex.import_codex_file(con, path, full=full)
        except (OSError, sqlite3.DatabaseError, UnicodeDecodeError,
                ValueError):
            totals["failed"].append(
                {"path": path, "error": "rollout_import_failed"})
            _record_error(con, path, "rollout_import_failed",
                          f"codex rollout file {os.path.basename(path)}")
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
            if (not isinstance(value, (int, float))
                    or isinstance(value, bool)):
                return None
            total += value
        return total
    return None


def _reconcile_usage(con: sqlite3.Connection, db_path: str,
                     invocations: list[dict], bound: dict[str, str],
                     totals: dict) -> None:
    """Record a usage_mismatch when the router total differs from native."""
    for inv in invocations:
        invocation_id = inv.get("invocation_id")
        session_key = bound.get(invocation_id)
        usage_text = inv.get("usage_json")
        if not session_key or not usage_text:
            continue
        if not (session_key.startswith("codex:")
                or session_key.startswith("opencode:")):
            continue
        if not con.execute("SELECT 1 FROM sessions WHERE session_key=?",
                           (session_key,)).fetchone():
            continue
        router_total = _router_total(usage_text, session_key)
        if router_total is None:
            continue
        row = con.execute(
            "SELECT SUM(total_tokens) AS total FROM responses"
            " WHERE session_key=?", (session_key,)).fetchone()
        native_total = row["total"] if row else None
        if native_total is None:
            continue
        if router_total != native_total:
            _record_malformed(
                con, db_path,
                f"usage_mismatch: router={router_total}"
                f" native={native_total} session={session_key}",
                _shape("invocations", ("invocation_id", "usage_json"),
                       invocation_id), totals)
