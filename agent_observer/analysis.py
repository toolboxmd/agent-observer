"""Deterministic detectors and comparisons over the ledger.

Every detector returns incidents with the session, project, time and the
event references a person needs to check them. A detected repetition is a
candidate, not proof of waste: file changes, compactions and proof
obligations can make a repeat legitimate, and the evidence travels with the
incident so the reader can tell. Heuristic detectors say so in their label.
No model calls; the same ledger gives the same answer.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import defaultdict

TEST_COMMAND_RE = re.compile(
    r"(\bpytest\b|\bunittest\b|scripts/test\.py|\bnpm (run )?test\b|\bpnpm (run )?test\b"
    r"|\byarn test\b|\bgo test\b|\bcargo test\b|\bjest\b|\bvitest\b|\bmocha\b|\bphpunit\b"
    r"|\brspec\b|\bmake test\b|\bbun test\b|\bdeno test\b|\bctest\b|\bmvn test\b|\bgradle test\b)")
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]*\.py$|_test\.(py|go|rb|exs?)$"
    r"|\.(test|spec)\.[jt]sx?$|Test\.java$|Tests?\.cs$")
PERMISSION_RE = re.compile(
    r"\b(should i|shall i|do you want me to|want me to|would you like me to|can i|may i"
    r"|let me know if you(?:'d| would) like|should we|do you want to proceed|proceed\?)\b[^?]*\?\s*$",
    re.I | re.S)
CORRECTION_RE = re.compile(
    r"^\s*(no\b|nope\b|stop\b|wrong\b|that'?s not\b|not what i\b|don'?t\b|why did you\b"
    r"|you didn'?t\b|bro\b|bruh\b|wtf\b|i said\b|again\?|this is wrong\b)", re.I)
LARGE_OUTPUT_BYTES = 50_000
AGENTSMD_PATH = "/agentsmd/"

DETECTORS = ("repeated_read", "repeated_skill_load", "repeated_command",
             "repeated_failure", "test_edit_after_failure", "permission_seeking",
             "human_correction", "large_tool_output")
HEURISTIC = {"permission_seeking", "human_correction"}


def _detail(row) -> dict:
    try:
        return json.loads(row["detail_json"]) if row["detail_json"] else {}
    except ValueError:
        return {}


def _failed(row) -> bool:
    if row["status"] in ("error", "failed", "failure"):
        return True
    detail = _detail(row)
    for key in ("exit_code", "exitCode"):
        code = detail.get(key)
        if isinstance(code, int) and code != 0:
            return True
    return False


def _passed(row) -> bool:
    if _failed(row):
        return False
    return row["status"] in ("ok", "completed", "success", None)


def _session_filter(since=None, until=None, project=None, harness=None,
                    agentsmd_version=None, session=None):
    where = ["(s.role IS NULL OR s.role != 'subagent')"]
    args: list = []
    if session:
        where.append("s.session_key = ?")
        args.append(session)
    if since is not None:
        where.append("COALESCE(s.ended_at, s.started_at, 0) >= ?")
        args.append(since)
    if until is not None:
        where.append("COALESCE(s.started_at, s.ended_at, 0) < ?")
        args.append(until)
    if project:
        where.append("s.project_dir LIKE ?")
        args.append(f"%{project}%")
    if harness:
        where.append("s.harness = ?")
        args.append(harness)
    if agentsmd_version:
        where.append("s.agentsmd_version = ?")
        args.append(agentsmd_version)
    return " AND ".join(where), args


def sessions_in_scope(con, **filters) -> list:
    where, args = _session_filter(**filters)
    return con.execute(f"SELECT s.* FROM sessions s WHERE {where}", args).fetchall()


def _events(con, session_key: str) -> list:
    return con.execute(
        "SELECT * FROM events WHERE session_key=? ORDER BY COALESCE(ts, 0), ordinal_num, id",
        (session_key,)).fetchall()


def _incident(session, detector, ts, summary, refs, extra=None) -> dict:
    return {"detector": detector, "heuristic": detector in HEURISTIC,
            "session": session["session_key"], "harness": session["harness"],
            "project": session["project_dir"], "agentsmd_version": session["agentsmd_version"],
            "ts": ts, "summary": summary, "event_refs": refs, **(extra or {})}


def detect_session(con, session) -> list:
    """All incidents for one session, in time order."""
    events = _events(con, session["session_key"])
    incidents: list = []
    incidents += _repeated_reads(session, events)
    incidents += _repeated_skills(session, events)
    incidents += _repeated_commands(session, events)
    incidents += _test_edits_after_failure(session, events)
    incidents += _permission_seeking(session, events)
    incidents += _human_corrections(con, session, events)
    incidents += _large_outputs(session, events)
    return sorted(incidents, key=lambda i: (i["ts"] or 0))


def _boundaries(events) -> list:
    return [e["ts"] or 0 for e in events if e["family"] == "compaction"]


def _repeated_reads(session, events) -> list:
    """Same target and range read again with no edit to it and no compaction
    in between. Different ranges, an intervening change or a compaction make
    the reread explainable, so they break the run."""
    out = []
    last: dict = {}
    changed_since: dict = {}
    compactions = _boundaries(events)
    for e in events:
        if e["family"] == "file_change" and e["target"]:
            changed_since[e["target"]] = e["ts"] or 0
            continue
        if e["family"] != "read" or not e["target"]:
            continue
        detail = _detail(e)
        key = (e["target"], detail.get("start_line"), detail.get("num_lines"),
               detail.get("cmd"))
        prev = last.get(key)
        ts = e["ts"] or 0
        if prev is not None:
            edited = changed_since.get(e["target"], -1) > (prev["ts"] or 0)
            compacted = any((prev["ts"] or 0) < c <= ts for c in compactions)
            if not edited and not compacted:
                out.append(_incident(session, "repeated_read", e["ts"],
                                     f"re-read {e['target']}",
                                     [prev["id"], e["id"]], {"target": e["target"]}))
        last[key] = e
    return out


def _repeated_skills(session, events) -> list:
    out = []
    seen: dict = {}
    compactions = _boundaries(events)
    for e in events:
        if e["family"] not in ("skill_read", "skill_invoke"):
            continue
        name = (e["name"] or e["target"] or "").lower()
        if e["family"] == "skill_read":
            name = (_detail(e).get("skill") or name).lower()
        key = (e["family"], name, e["target"] if e["family"] == "skill_read" else None)
        prev = seen.get(key)
        ts = e["ts"] or 0
        if prev is not None and not any((prev["ts"] or 0) < c <= ts for c in compactions):
            out.append(_incident(session, "repeated_skill_load", e["ts"],
                                 f"loaded {name} again ({e['family']})",
                                 [prev["id"], e["id"]], {"skill": name}))
        seen[key] = e
    return out


def _command(e) -> str | None:
    if e["family"] != "tool_result":
        return None
    name = (e["name"] or "").lower()
    if name in ("bash", "exec", "shell", "run_terminal_cmd", "run_command") and e["target"]:
        return " ".join(e["target"].split())
    return None


def _repeated_commands(session, events) -> list:
    out = []
    runs: dict = defaultdict(list)
    for e in events:
        cmd = _command(e)
        if cmd:
            runs[cmd].append(e)
    for cmd, rows in runs.items():
        if len(rows) >= 3:
            out.append(_incident(session, "repeated_command", rows[-1]["ts"],
                                 f"ran {len(rows)} times: {cmd[:120]}",
                                 [r["id"] for r in rows], {"command": cmd, "count": len(rows)}))
        streak = []
        for r in rows:
            if _failed(r):
                streak.append(r)
                if len(streak) == 2:
                    out.append(_incident(session, "repeated_failure", r["ts"],
                                         f"failed again: {cmd[:120]}",
                                         [s["id"] for s in streak], {"command": cmd}))
            else:
                streak = []
    return out


def _test_edits_after_failure(session, events) -> list:
    """A failing test run, then edits to test files, then a passing run.

    This is the pattern the user named: a test updated to the new output
    instead of asking what it protects. It is a candidate: the edit may be a
    legitimate rewrite, which the event references let a person check."""
    out = []
    failing = None
    test_edits: list = []
    code_edits: list = []
    for e in events:
        cmd = _command(e)
        if cmd and TEST_COMMAND_RE.search(cmd):
            if _failed(e):
                if failing is None:
                    failing = e
                    test_edits, code_edits = [], []
            elif failing is not None:
                if test_edits:
                    out.append(_incident(
                        session, "test_edit_after_failure", e["ts"],
                        f"test files edited after a failing run: "
                        f"{', '.join(sorted({t['target'] for t in test_edits}))[:200]}",
                        [failing["id"]] + [t["id"] for t in test_edits] + [e["id"]],
                        {"test_files": sorted({t["target"] for t in test_edits}),
                         "code_files": sorted({c["target"] for c in code_edits}),
                         "only_tests_changed": not code_edits,
                         "command": cmd}))
                failing = None
                test_edits, code_edits = [], []
            continue
        if failing is not None and e["family"] == "file_change" and e["target"]:
            (test_edits if TEST_PATH_RE.search(e["target"]) else code_edits).append(e)
    return out


def _permission_seeking(session, events) -> list:
    out = []
    for e in events:
        if e["family"] != "assistant_message":
            continue
        excerpt = str(_detail(e).get("excerpt") or "").strip()
        tail = excerpt[-300:]
        if tail.endswith("?") and PERMISSION_RE.search(tail):
            out.append(_incident(session, "permission_seeking", e["ts"],
                                 f"asked: {tail[-160:]}", [e["id"]]))
    return out


def _human_corrections(con, session, events) -> list:
    out = []
    for row in con.execute(
            "SELECT native_id, ts, kind, text_excerpt FROM submissions"
            " WHERE session_key=? AND kind IN ('genuine', 'interrupt') ORDER BY ts",
            (session["session_key"],)):
        if row["kind"] == "interrupt":
            out.append(_incident(session, "human_correction", row["ts"],
                                 "interrupted the agent", [row["native_id"]],
                                 {"signal": "interrupt"}))
        elif CORRECTION_RE.search(row["text_excerpt"] or ""):
            out.append(_incident(session, "human_correction", row["ts"],
                                 f"said: {(row['text_excerpt'] or '')[:120]}",
                                 [row["native_id"]], {"signal": "correction wording"}))
    for e in events:
        if e["family"] == "lifecycle" and e["name"] == "turn_aborted" and \
                "interrupt" in str(_detail(e).get("reason") or "").lower():
            out.append(_incident(session, "human_correction", e["ts"],
                                 "interrupted the agent", [e["id"]], {"signal": "interrupt"}))
        if e["family"] == "permission" and e["status"] == "denied":
            out.append(_incident(session, "human_correction", e["ts"],
                                 f"denied {e['name']}", [e["id"]], {"signal": "denial"}))
    return out


def _large_outputs(session, events) -> list:
    return [_incident(session, "large_tool_output", e["ts"],
                      f"{e['name']} returned {e['size_bytes']} bytes", [e["id"]],
                      {"bytes": e["size_bytes"], "truncated": bool(e["truncated"])})
            for e in events
            if e["family"] == "tool_result" and ((e["size_bytes"] or 0) >= LARGE_OUTPUT_BYTES
                                                 or e["truncated"])]


def diagnose(con, detectors=None, limit=200, **filters) -> dict:
    wanted = set(detectors or DETECTORS)
    incidents = []
    sessions = sessions_in_scope(con, **filters)
    for s in sessions:
        incidents += [i for i in detect_session(con, s) if i["detector"] in wanted]
    incidents.sort(key=lambda i: (i["ts"] or 0), reverse=True)
    counts: dict = defaultdict(int)
    for i in incidents:
        counts[i["detector"]] += 1
    return {"sessions": len(sessions), "counts": dict(counts),
            "incidents": incidents[:limit], "truncated": len(incidents) > limit,
            "note": "Candidates with evidence, not verdicts. Heuristic detectors are marked."}


def _session_metrics(con, s) -> dict:
    key = s["session_key"]
    usage = con.execute(
        "SELECT COUNT(*) n, SUM(total_tokens) t, MAX(input_tokens) mi FROM responses"
        " WHERE session_key=? AND is_overlap=0", (key,)).fetchone()
    subs = {r["kind"]: r["n"] for r in con.execute(
        "SELECT kind, COUNT(*) n FROM submissions WHERE session_key=? GROUP BY kind", (key,))}
    reading = con.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(size_bytes), 0) b FROM events WHERE session_key=?"
        " AND family IN ('read', 'skill_read') AND target LIKE ?", (key, f"%{AGENTSMD_PATH}%")
    ).fetchone()
    incidents = detect_session(con, s)
    per = defaultdict(int)
    for i in incidents:
        per[i["detector"]] += 1
    elapsed = None
    if s["started_at"] and s["ended_at"]:
        elapsed = max(0.0, s["ended_at"] - s["started_at"])
    return {"responses": usage["n"] or 0, "tokens": usage["t"] or 0,
            "genuine_prompts": subs.get("genuine", 0), "interrupts": subs.get("interrupt", 0),
            "agentsmd_reads": reading["n"] or 0, "agentsmd_read_bytes": reading["b"] or 0,
            "elapsed_s": elapsed, "incidents": dict(per)}


def _stats(values) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    return {"n": len(values), "median": statistics.median(values),
            "mean": round(statistics.fmean(values), 2), "total": round(sum(values), 2)}


def compare(con, by: str = "agentsmd", **filters) -> dict:
    """Group sessions and report behavior per group with sample sizes."""
    column = {"agentsmd": "agentsmd_version", "harness": "harness",
              "project": "project_dir", "model": None}[by]
    groups: dict = defaultdict(list)
    for s in sessions_in_scope(con, **filters):
        if by == "model":
            row = con.execute(
                "SELECT model, COUNT(*) n FROM responses WHERE session_key=? AND model IS NOT NULL"
                " GROUP BY model ORDER BY n DESC LIMIT 1", (s["session_key"],)).fetchone()
            label = row["model"] if row else None
        else:
            label = s[column]
        groups[label or "unknown"].append(s)
    rows = []
    for label, members in groups.items():
        metrics = [_session_metrics(con, s) for s in members]
        human = [m for m in metrics if m["genuine_prompts"]]
        row = {"group": label, "sessions": len(members),
               "projects": len({s["project_dir"] for s in members}),
               "sessions_with_human_prompts": len(human),
               "tokens_per_session": _stats([m["tokens"] for m in metrics]),
               "elapsed_s_per_session": _stats([m["elapsed_s"] for m in metrics]),
               "genuine_prompts_per_session": _stats([m["genuine_prompts"] for m in human]),
               "interrupts": sum(m["interrupts"] for m in metrics),
               "agentsmd_read_bytes_per_session": _stats([m["agentsmd_read_bytes"] for m in metrics]),
               "first_seen": min((s["started_at"] or 0) for s in members) or None,
               "last_seen": max((s["ended_at"] or s["started_at"] or 0) for s in members) or None}
        for detector in DETECTORS:
            total = sum(m["incidents"].get(detector, 0) for m in metrics)
            row[detector] = {"incidents": total,
                             "per_session": round(total / len(members), 3) if members else None}
        rows.append(row)
    rows.sort(key=lambda r: _version_key(r["group"]) if by == "agentsmd" else r["group"])
    return {"by": by, "groups": rows,
            "note": ("Observational comparison: groups differ in period, projects and task "
                     "mix, so differences are leads to inspect, not causal effects. "
                     "Subagent sessions are excluded; every figure carries its sample size.")}


def _version_key(label: str):
    parts = label.split(".")
    if all(p.isdigit() for p in parts):
        return (0, tuple(int(p) for p in parts))
    return (1, (label,))
