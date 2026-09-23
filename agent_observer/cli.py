"""CLI: agent-observer sync|sessions|capture|task|trace. Stdlib only."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import CAPTURE_CONTRACT_VERSION, EVENT_CONTRACT_VERSION, SCHEMA_VERSION
from . import adapters as _adapters
from . import db as _db
from . import identity as _identity
from . import report as _report


def _con(path: str):
    con = _db.connect(path)
    _db.init_db(con)
    return con


def _emit(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(_text(payload))


def _text(payload) -> str:
    if isinstance(payload, dict) and payload.get("_view") == "sync":
        lines = []
        for h in payload["harnesses"]:
            line = (f"{h['harness']}: {h.get('sources', 0)} sources "
                    f"({h.get('unchanged', 0)} unchanged), "
                    f"{h.get('responses_inserted', 0)} new responses, "
                    f"{h.get('events_inserted', 0)} new events, "
                    f"{h.get('submissions_inserted', 0)} new submissions, "
                    f"{h.get('malformed', 0)} malformed records kept in import_errors")
            if h.get("failed"):
                line += f", {len(h['failed'])} sources failed"
            if h.get("skipped"):
                line += f" (skipped: {h['skipped']})"
            lines.append(line)
        if payload.get("agentsmd_versions") is not None:
            lines.append(f"agentsmd versions mapped: {payload['agentsmd_versions']}")
        return "\n".join(lines)
    if isinstance(payload, dict) and payload.get("_view") == "sessions":
        lines = []
        for r in payload["sessions"]:
            lines.append(
                f"{r['session_key']}  {r.get('project_dir') or '?'}  "
                f"agentsmd={r.get('agentsmd_version') or 'unknown'}  "
                f"responses={r.get('responses', 0)} total={r.get('total_tokens', 0)}")
        return "\n".join(lines) or "no sessions"
    if isinstance(payload, dict) and payload.get("_view") == "task":
        r = payload
        lines = [
            f"task {r['task']['task_id']}: {r['task'].get('title') or ''}",
            f"  attributed responses: {r['attributed']['responses']} "
            f"total={r['attributed']['total_tokens']}",
            f"  shared joint responses: {r['shared_joint']['responses']} "
            f"total={r['shared_joint']['total_tokens']}",
            f"  unassigned in scope: {r['unassigned_in_scope']['responses']} "
            f"total={r['unassigned_in_scope']['total_tokens']}",
            f"  reconciles against scope: {r['reconciles']}",
            f"  missing assignments: {r['missing_assignments'] or 'none'}",
            f"  conflicting: {r['conflicting_assignments'] or 'none'}",
            f"  crashes counted separately: {r['crashes_counted_separately']}",
        ]
        return "\n".join(lines)
    if isinstance(payload, dict) and payload.get("_view") == "trace":
        lines = [f"events: {len(payload['events'])}",
                 f"joined call_ids: {len(payload['join']['joined_call_ids'])}",
                 f"unmatched results: {len(payload['join']['unmatched_results'])}",
                 f"unanswered calls: {len(payload['join']['unanswered_calls'])}"]
        for e in payload["events"][:50]:
            lines.append(
                f"  #{e.get('ordinal_num')} {e.get('family')} "
                f"{(e.get('name') or '')[:80]} [{e.get('status') or 'unknown'}]")
        if len(payload["events"]) > 50:
            lines.append(f"  ... ({len(payload['events']) - 50} more)")
        return "\n".join(lines)
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agent_observer",
                                 description="Observe agent work from native records.")
    ap.add_argument("--db", default=_db.default_path())
    ap.add_argument("--json", action="store_true", dest="as_json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="import native records from every harness")
    s.add_argument("--source", default=None,
                   help="import one file (requires --harness)")
    s.add_argument("--harness", action="append", default=None,
                   choices=list(_adapters.ORDER),
                   help="limit to these harnesses (repeatable)")
    s.add_argument("--root", default=None,
                   help="override the harness record location")
    s.add_argument("--full", action="store_true",
                   help="re-read sources from the start")
    s.add_argument("--agentsmd-repo", default=None,
                   help="AgentsMD repository for the version map")
    s.add_argument("--json", action="store_true", dest="as_json")

    ss = sub.add_parser("sessions", help="list or show imported sessions")
    ssub = ss.add_subparsers(dest="op", required=True)
    sl = ssub.add_parser("list")
    sl.add_argument("--harness", default=None)
    sl.add_argument("--project", default=None)
    sl.add_argument("--since", default=None, help="ISO date or epoch seconds")
    sl.add_argument("--limit", type=int, default=50)
    sl.add_argument("--json", action="store_true", dest="as_json")
    sw = ssub.add_parser("show")
    sw.add_argument("--session", required=True)
    sw.add_argument("--json", action="store_true", dest="as_json")

    c = sub.add_parser("capture", help="record workload facts")
    csub = c.add_subparsers(dest="op", required=True)
    t = csub.add_parser("create-task")
    t.add_argument("--task", required=True)
    t.add_argument("--project", default=None)
    t.add_argument("--family", default=None)
    t.add_argument("--title", default=None)
    t.add_argument("--issue", default=None)
    a = csub.add_parser("assign")
    a.add_argument("--submission", required=True)
    a.add_argument("--task", required=True)
    a.add_argument("--attempt", default=None)
    a.add_argument("--phase", default=None)
    a.add_argument("--evidence", default=None)
    a.add_argument("--shared", action="store_true")
    d = csub.add_parser("dispatch")
    d.add_argument("--submission", required=True)
    d.add_argument("--worker", required=True)
    d.add_argument("--parent-turn", default=None)
    d.add_argument("--worker-turn", default=None)
    d.add_argument("--requested-model", default=None)
    d.add_argument("--requested-effort", default=None)
    d.add_argument("--policy", default=None)
    d.add_argument("--reason", default=None)
    d.add_argument("--task-name", default=None)
    at = csub.add_parser("attempt")
    at.add_argument("--task", required=True)
    at.add_argument("--turn", required=True)
    at.add_argument("--role", required=True, choices=["parent", "worker"])
    at.add_argument("--model", default=None)
    at.add_argument("--effort", default=None)
    at.add_argument("--state", default="active",
                    choices=["complete", "active", "cancelled", "failed",
                             "quota_blocked", "crashed"])
    at.add_argument("--usable", action="store_true", default=None)
    at.add_argument("--not-usable", action="store_true")
    o = csub.add_parser("outcome")
    o.add_argument("--task", required=True)
    o.add_argument("--state", required=True,
                   choices=["complete", "active", "cancelled", "failed",
                            "quota_blocked", "crashed", "unknown"])
    o.add_argument("--candidate", default=None)
    o.add_argument("--proof", default=None)
    o.add_argument("--repairs", default=None)
    o.add_argument("--corrections", default=None)

    k = sub.add_parser("task", help="inspect tasks and usage")
    ksub = k.add_subparsers(dest="op", required=True)
    ksub.add_parser("list").add_argument("--json", action="store_true",
                                              dest="as_json")
    sh = ksub.add_parser("show")
    sh.add_argument("--task", required=True)
    sh.add_argument("--json", action="store_true", dest="as_json")

    tr = sub.add_parser("trace", help="inspect the execution timeline")
    tr.add_argument("--task", default=None)
    tr.add_argument("--turn", default=None)
    tr.add_argument("--session", default=None)
    tr.add_argument("--family", default=None)
    tr.add_argument("--limit", type=int, default=200)
    tr.add_argument("--capabilities", action="store_true")
    tr.add_argument("--json", action="store_true", dest="as_json")
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    ns = ap.parse_args(argv)
    try:
        con = _con(ns.db)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        if ns.cmd == "sync":
            return _sync(con, ns)

        if ns.cmd == "sessions":
            return _sessions(con, ns)

        if ns.cmd == "capture":
            return _capture(con, ns)

        if ns.cmd == "task":
            if ns.op == "list":
                rows = [dict(r) for r in con.execute(
                    "SELECT * FROM tasks ORDER BY task_id")]
                if ns.as_json:
                    print(json.dumps({"tasks": rows}, indent=2,
                                     sort_keys=True, default=str))
                elif not rows:
                    print("no tasks")
                else:
                    for r in rows:
                        print(f"{r['task_id']}: {r.get('title') or ''}")
                return 0
            try:
                rep = _report.task_report(con, ns.task)
            except KeyError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            rep["_view"] = "task"
            _emit(rep, ns.as_json)
            # Missing or conflicting ownership is a visible failure.
            if rep["missing_assignments"] or rep["conflicting_assignments"]:
                return 3
            return 0

        if ns.cmd == "trace":
            if ns.capabilities:
                caps = {"capabilities": _report.capabilities(),
                        "contract": {"event": EVENT_CONTRACT_VERSION}}
                if ns.as_json:
                    print(json.dumps(caps, indent=2, sort_keys=True))
                else:
                    for c in caps["capabilities"]:
                        mark = "yes" if c["supported"] else "no "
                        print(f"{mark} {c['family']}: {c['detail']}")
                return 0
            tl = _report.timeline(con, task_id=ns.task, turn_id=ns.turn,
                                 family=ns.family, limit=ns.limit,
                                 session_key=ns.session)
            tl["_view"] = "trace"
            _emit(tl, ns.as_json)
            return 0
    finally:
        con.close()
    return 2


def _capture(con, ns) -> int:
    if ns.op == "create-task":
        con.execute(
            "INSERT OR IGNORE INTO tasks(task_id, project, family, title,"
            " issue_url, created_at) VALUES(?,?,?,?,?,?)",
            (ns.task, ns.project, ns.family, ns.title, ns.issue, _db.now()))
        con.commit()
        _emit({"ok": True, "task": ns.task}, ns.as_json)
        return 0
    if ns.op == "assign":
        ns.submission = _resolve_submission(con, ns.submission)
        sub = con.execute("SELECT native_id, is_genuine FROM submissions "
                          "WHERE native_id=?", (ns.submission,)).fetchone()
        if sub is None:
            print(f"unknown submission: {ns.submission}", file=sys.stderr)
            return 2
        if not sub["is_genuine"]:
            print(f"refused: {ns.submission} is not a genuine submission",
                  file=sys.stderr)
            return 2
        if con.execute("SELECT 1 FROM tasks WHERE task_id=?",
                       (ns.task,)).fetchone() is None:
            print(f"unknown task: {ns.task}", file=sys.stderr)
            return 2
        con.execute(
            "INSERT OR IGNORE INTO assignments(submission_native_id, task_id,"
            " attempt, phase, evidence, shared, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (ns.submission, ns.task, ns.attempt, ns.phase, ns.evidence,
             1 if ns.shared else 0, _db.now()))
        con.commit()
        _emit({"ok": True, "submission": ns.submission, "task": ns.task,
               "shared": bool(ns.shared)}, ns.as_json)
        return 0
    if ns.op == "dispatch":
        ns.submission = _resolve_submission(con, ns.submission)
        if con.execute("SELECT 1 FROM submissions WHERE native_id=?",
                       (ns.submission,)).fetchone() is None:
            print(f"unknown submission: {ns.submission}", file=sys.stderr)
            return 2
        con.execute(
            "INSERT OR IGNORE INTO dispatches(owning_submission, parent_turn,"
            " worker_thread, worker_turn, requested_model, requested_effort,"
            " policy_version, reason, task_name, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ns.submission, ns.parent_turn, ns.worker, ns.worker_turn,
             ns.requested_model, ns.requested_effort, ns.policy, ns.reason,
             ns.task_name, _db.now()))
        con.commit()
        _emit({"ok": True, "worker": ns.worker}, ns.as_json)
        return 0
    if ns.op == "attempt":
        usable = None
        if ns.not_usable:
            usable = 0
        elif ns.usable:
            usable = 1
        con.execute(
            "INSERT OR IGNORE INTO attempts(task_id, turn_id, role, harness,"
            " model_observed, effort_observed, state, usable_output)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (ns.task, ns.turn, ns.role, ns.turn.split(":", 1)[0]
             if ":" in ns.turn else None, ns.model, ns.effort,
             ns.state, usable))
        con.commit()
        _emit({"ok": True, "turn": ns.turn}, ns.as_json)
        return 0
    if ns.op == "outcome":
        # Exit 0 never implies acceptance: acceptance_state stays explicit.
        con.execute(
            "INSERT INTO outcomes(task_id, candidate, proof_ref,"
            " acceptance_state, repairs, corrections, updated_at)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET"
            " candidate=excluded.candidate, proof_ref=excluded.proof_ref,"
            " acceptance_state=excluded.acceptance_state,"
            " repairs=excluded.repairs, corrections=excluded.corrections,"
            " updated_at=excluded.updated_at",
            (ns.task, ns.candidate, ns.proof, ns.state, ns.repairs,
             ns.corrections, _db.now()))
        con.commit()
        _emit({"ok": True, "task": ns.task, "state": ns.state}, ns.as_json)
        return 0
    return 2


def _resolve_submission(con, given: str) -> str:
    """Accept a harness-prefixed id or a bare native id when it is unique."""
    if con.execute("SELECT 1 FROM submissions WHERE native_id=?",
                   (given,)).fetchone():
        return given
    rows = con.execute("SELECT native_id FROM submissions WHERE native_id LIKE ?",
                       ("%:" + given,)).fetchall()
    return rows[0]["native_id"] if len(rows) == 1 else given


def _sync(con, ns) -> int:
    registry = _adapters.available()
    wanted = ns.harness or [h for h in _adapters.ORDER if h in registry]
    if ns.source and (not ns.harness or len(ns.harness) != 1):
        # A single file names its harness; default to Codex for rollouts.
        wanted = ns.harness or ["codex"]
    if ns.source and not os.path.exists(ns.source):
        print(f"source not found: {ns.source}", file=sys.stderr)
        return 2
    # The release map comes first so adapters can match embedded
    # instruction text at import time and keep only the hash.
    mapped = None
    repo = ns.agentsmd_repo or _identity.default_agentsmd_repo()
    if repo and os.path.isdir(repo):
        mapped = _identity.store_version_map(con, _identity.build_version_map(repo))
    results = []
    for name in wanted:
        module = registry.get(name)
        if module is None:
            results.append({"harness": name, "skipped": "adapter not built"})
            continue
        results.append(module.sync(con, root=ns.root, full=ns.full,
                                   source=ns.source))
    _identity.refresh_session_versions(con)
    con.commit()
    payload = {"_view": "sync", "harnesses": results,
               "agentsmd_versions": mapped,
               "contract": {"schema": SCHEMA_VERSION,
                            "event": EVENT_CONTRACT_VERSION,
                            "capture": CAPTURE_CONTRACT_VERSION}}
    _emit(payload, ns.as_json)
    return 1 if any(r.get("failed") for r in results) else 0


def _sessions(con, ns) -> int:
    if ns.op == "show":
        row = con.execute("SELECT * FROM sessions WHERE session_key=?",
                          (ns.session,)).fetchone()
        if row is None:
            print(f"unknown session: {ns.session}", file=sys.stderr)
            return 2
        payload = dict(row)
        payload["usage"] = _report.scope_totals(con, {ns.session})
        payload["events"] = {r["family"]: r["n"] for r in con.execute(
            "SELECT family, COUNT(*) n FROM events WHERE session_key=?"
            " GROUP BY family", (ns.session,))}
        payload["submissions"] = {r["kind"]: r["n"] for r in con.execute(
            "SELECT kind, COUNT(*) n FROM submissions WHERE session_key=?"
            " GROUP BY kind", (ns.session,))}
        _emit(payload, True)
        return 0
    q = ("SELECT s.*, COUNT(r.response_id) responses,"
         " COALESCE(SUM(CASE WHEN r.is_overlap=0 THEN r.total_tokens END), 0)"
         " total_tokens FROM sessions s LEFT JOIN responses r"
         " ON r.session_key=s.session_key WHERE 1=1")
    args: list = []
    if ns.harness:
        q += " AND s.harness=?"
        args.append(ns.harness)
    if ns.project:
        q += " AND s.project_dir LIKE ?"
        args.append(f"%{ns.project}%")
    if ns.since:
        from .ingest import iso_ts
        since = iso_ts(ns.since) if not ns.since.replace(".", "").isdigit() \
            else float(ns.since)
        q += " AND COALESCE(s.ended_at, s.started_at, 0) >= ?"
        args.append(since or 0)
    q += " GROUP BY s.session_key ORDER BY COALESCE(s.ended_at, s.started_at) DESC LIMIT ?"
    args.append(ns.limit)
    rows = [dict(r) for r in con.execute(q, args)]
    for r in rows:
        r.pop("identity_json", None)
    _emit({"_view": "sessions", "sessions": rows}, ns.as_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
