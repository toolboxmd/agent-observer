"""Query surface: task usage reconciliation and event timelines."""

from __future__ import annotations

import json
import sqlite3

BUCKETS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
           "output_tokens", "reasoning_output_tokens", "total_tokens")


def _sum(rows, field: str) -> int:
    return sum((r[field] or 0) for r in rows if not r["is_overlap"])


def scope_totals(con: sqlite3.Connection) -> dict:
    rows = con.execute("SELECT * FROM responses").fetchall()
    return {b: _sum(rows, b) for b in BUCKETS} | {
        "responses": sum(1 for r in rows if not r["is_overlap"]),
        "overlap_responses": sum(1 for r in rows if r["is_overlap"]),
    }


def _turn_submission(con: sqlite3.Connection) -> dict:
    """Map turn_id to the genuine submission that started it.

    One turn follows one genuine user submission; the mapping uses the
    submission turn_id recorded at import. Tool results and assistant rows
    never create submissions.
    """
    mapping = {}
    for s in con.execute(
            "SELECT native_id, turn_id FROM submissions WHERE is_genuine=1"):
        if s["turn_id"] and s["turn_id"] not in mapping:
            mapping[s["turn_id"]] = s["native_id"]
    return mapping


def task_report(con: sqlite3.Connection, task_id: str) -> dict:
    task = con.execute("SELECT * FROM tasks WHERE task_id=?",
                       (task_id,)).fetchone()
    if task is None:
        raise KeyError(f"unknown task: {task_id}")
    turn_sub = _turn_submission(con)
    assigns = con.execute(
        "SELECT * FROM assignments WHERE task_id=?", (task_id,)).fetchall()
    bound_subs = {a["submission_native_id"]: a for a in assigns}
    # Joint submissions bound to several tasks stay shared, never divided.
    joint = {}
    for a in con.execute(
            "SELECT submission_native_id, COUNT(*) n FROM assignments "
            "GROUP BY 1 HAVING n > 1"):
        rows = con.execute(
            "SELECT task_id, shared FROM assignments WHERE "
            "submission_native_id=?", (a["submission_native_id"],)).fetchall()
        joint[a["submission_native_id"]] = {
            "tasks": [r["task_id"] for r in rows],
            "all_shared": all(r["shared"] for r in rows),
        }

    responses = con.execute("SELECT * FROM responses").fetchall()
    attributed, shared, unassigned = [], [], []
    missing_submissions = []
    assigned_anywhere = {r["submission_native_id"] for r in
                         con.execute("SELECT submission_native_id "
                                     "FROM assignments").fetchall()}
    for r in responses:
        if r["is_overlap"]:
            continue
        sub = turn_sub.get(r["turn_id"])
        if sub is None:
            unassigned.append(dict(r))
        elif sub in bound_subs:
            if sub in joint:
                shared.append(dict(r))
            else:
                attributed.append(dict(r))
        else:
            unassigned.append(dict(r))
            if sub not in assigned_anywhere and sub not in missing_submissions:
                missing_submissions.append(sub)

    def total(rows):
        return {b: sum((x[b] or 0) for x in rows) for b in BUCKETS} | {
            "responses": len(rows)}

    scope = scope_totals(con)
    crashes = con.execute(
        "SELECT COUNT(*) n FROM attempts WHERE task_id=? AND state='crashed'",
        (task_id,)).fetchone()["n"]
    report = {
        "task": dict(task),
        "attributed": total(attributed),
        "shared_joint": total(shared),
        "unassigned_in_scope": total(unassigned),
        "scope": scope,
        "missing_assignments": missing_submissions,
        "conflicting_assignments": [
            {"submission": s, **j} for s, j in joint.items()
            if not j["all_shared"] and s in bound_subs],
        "joint_assignments": [
            {"submission": s, **j} for s, j in joint.items()
            if j["all_shared"] and s in bound_subs],
        "crashes_counted_separately": crashes,
        "outcome": _row_or_none(
            con.execute("SELECT * FROM outcomes WHERE task_id=?",
                        (task_id,)).fetchone()),
        "attempts": [dict(r) for r in con.execute(
            "SELECT * FROM attempts WHERE task_id=? ORDER BY turn_id",
            (task_id,))],
        "dispatches": [dict(r) for r in con.execute(
            "SELECT * FROM dispatches WHERE owning_submission IN "
            "(SELECT submission_native_id FROM assignments WHERE task_id=?)",
            (task_id,))],
    }
    # Reconciliation: attributed + shared count once per task view, but the
    # scope total is never claimed as this task total when unassigned exists.
    report["reconciles"] = (
        report["attributed"]["total_tokens"]
        + report["shared_joint"]["total_tokens"]
        + report["unassigned_in_scope"]["total_tokens"] == scope["total_tokens"]
    )
    report["complete"] = (not missing_submissions
                          and not report["conflicting_assignments"])
    return report


def timeline(con: sqlite3.Connection, task_id: str | None = None,
             turn_id: str | None = None, family: str | None = None,
             limit: int = 200) -> dict:
    turn_sub = _turn_submission(con)
    task_turns: set[str] | None = None
    if task_id is not None:
        bound = {a["submission_native_id"] for a in con.execute(
            "SELECT submission_native_id FROM assignments WHERE task_id=?",
            (task_id,))}
        task_turns = {t for t, s in turn_sub.items() if s in bound}
    q = "SELECT * FROM events WHERE 1=1"
    args: list = []
    if turn_id:
        q += " AND turn_id=?"
        args.append(turn_id)
    elif task_turns is not None:
        if not task_turns:
            return {"events": [], "note": "no bound turns; timeline empty"}
        q += f" AND turn_id IN ({','.join('?' * len(task_turns))})"
        args.extend(sorted(task_turns))
    if family:
        q += " AND family=?"
        args.append(family)
    q += " ORDER BY ordinal_num LIMIT ?"
    args.append(limit)
    rows = con.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d["detail_json"]) if d["detail_json"] else None
        except ValueError:
            d["detail"] = {"raw": d["detail_json"]}
        d.pop("detail_json", None)
        out.append(d)
    # Tool join status: calls joined to results only on equal call_id.
    calls = {e["native_id"] for e in out if e["family"] == "tool_call"}
    results = {e["native_id"] for e in out if e["family"] == "tool_result"}
    return {
        "events": out,
        "join": {
            "calls": len(calls),
            "results": len(results),
            "joined_call_ids": sorted(calls & results),
            "unmatched_results": sorted(results - calls),
            "unanswered_calls": sorted(calls - results),
        },
        "coverage_note": ("Native evidence absent stays unknown; unmatched "
                          "items are retained, never guessed into a match."),
    }


def capabilities() -> list[dict]:
    from .codex import CAPABILITIES
    return [{"family": f, "supported": s, "detail": d}
            for f, s, d in CAPABILITIES]


def _row_or_none(row) -> dict | None:
    return dict(row) if row is not None else None
