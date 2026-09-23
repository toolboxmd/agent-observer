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


def _changed_paths(event) -> set:
    """Every path a file_change event touches.

    Native Codex FileChange records carry several paths in
    detail["paths"]; older records carry only target. Both participate,
    so later reads of any changed path are explainable and every changed
    path counts in test-edit classification.
    """
    paths = set()
    if event["target"]:
        paths.add(event["target"])
    detail = _detail(event)
    sub = detail.get("paths")
    if isinstance(sub, dict):
        for path in sub.keys():
            if path:
                paths.add(str(path))
    elif isinstance(sub, (list, tuple)):
        for path in sub:
            if path:
                paths.add(str(path))
    return paths


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
        if e["family"] == "file_change":
            for path in _changed_paths(e):
                changed_since[path] = e["ts"] or 0
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


def _skill_key_touched(key, path: str) -> bool:
    """Whether a file change to path resets one skill's seen state.

    key is (family, name, target). A change resets the skill when it
    touches the skill's loaded file (exact or suffix match either way,
    since adapters store relative and absolute spellings) or a file
    under the skill's installed directory.
    """
    family, name, target = key
    changed = (path or "").lower().replace("\\", "/")
    if not changed:
        return False
    if target:
        loaded = str(target).lower().replace("\\", "/")
        if changed == loaded or changed.endswith("/" + loaded) \
                or loaded.endswith("/" + changed):
            return True
    if name:
        skill = str(name).lower()
        if f"skills/{skill}/" in changed or changed.endswith(f"skills/{skill}") \
                or changed.endswith(f"/{skill}/SKILL.md".lower()) \
                or changed == f"{skill}/SKILL.md".lower():
            return True
        if family == "skill_invoke" and (changed == skill
                                         or changed.endswith("/" + skill)):
            return True
    return False


def _repeated_skills(session, events) -> list:
    """A skill loaded again with no edit to its files and no compaction
    in between. Editing the skill's file resets that skill's seen state,
    so a reload after the edit is a counterexample, not a candidate."""
    out = []
    seen: dict = {}
    compactions = _boundaries(events)
    for e in events:
        if e["family"] == "file_change":
            for path in _changed_paths(e):
                for key in [k for k in seen if _skill_key_touched(k, path)]:
                    del seen[key]
            continue
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
    test_files: set = set()
    code_files: set = set()
    for e in events:
        cmd = _command(e)
        if cmd and TEST_COMMAND_RE.search(cmd):
            if _failed(e):
                if failing is None:
                    failing = e
                    test_edits, code_edits = [], []
                    test_files, code_files = set(), set()
            elif failing is not None:
                if test_edits:
                    out.append(_incident(
                        session, "test_edit_after_failure", e["ts"],
                        f"test files edited after a failing run: "
                        f"{', '.join(sorted(test_files))[:200]}",
                        [failing["id"]] + [t["id"] for t in test_edits] + [e["id"]],
                        {"test_files": sorted(test_files),
                         "code_files": sorted(code_files),
                         "only_tests_changed": not code_edits,
                         "command": cmd}))
                failing = None
                test_edits, code_edits = [], []
                test_files, code_files = set(), set()
            continue
        if failing is not None and e["family"] == "file_change":
            paths = _changed_paths(e)
            touched_tests = sorted(p for p in paths if TEST_PATH_RE.search(p))
            touched_code = sorted(p for p in paths if not TEST_PATH_RE.search(p))
            if touched_tests:
                test_edits.append(e)
                test_files.update(touched_tests)
            if touched_code:
                code_edits.append(e)
                code_files.update(touched_code)
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
        "SELECT COUNT(*) n, SUM(total_tokens) t,"
        " SUM(CASE WHEN total_tokens IS NULL THEN 1 ELSE 0 END) u,"
        " MAX(input_tokens) mi FROM responses"
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
    # Contract rule 5: an unknown total stays None, never zero; the count
    # of responses without totals marks the lower bound.
    return {"responses": usage["n"] or 0, "tokens": usage["t"],
            "tokens_unknown": usage["u"] or 0,
            "genuine_prompts": subs.get("genuine", 0), "interrupts": subs.get("interrupt", 0),
            "agentsmd_reads": reading["n"] or 0, "agentsmd_read_bytes": reading["b"] or 0,
            "elapsed_s": elapsed, "incidents": dict(per)}


def _turn_models(con, session_key: str) -> dict:
    """Map turn_id to the model that produced it.

    A turn's model comes from its responses; the turns table's observed
    model is the fallback. A turn with responses from several models
    keeps the majority one so incidents still attribute deterministically.
    """
    votes: dict = defaultdict(lambda: defaultdict(int))
    for r in con.execute(
            "SELECT turn_id, model FROM responses WHERE session_key=?"
            " AND turn_id IS NOT NULL AND model IS NOT NULL AND is_overlap=0",
            (session_key,)):
        votes[r["turn_id"]][r["model"]] += 1
    mapping = {turn: max(counts, key=counts.get) for turn, counts in votes.items()}
    for t in con.execute(
            "SELECT turn_id, model_observed FROM turns WHERE session_key=?"
            " AND turn_id IS NOT NULL AND model_observed IS NOT NULL",
            (session_key,)):
        mapping.setdefault(t["turn_id"], t["model_observed"])
    return mapping


def _incident_turns(con, session_key: str, incident: dict) -> set:
    """Turns an incident's evidence points at.

    Event refs are integer event ids; submission refs are native ids, and
    anything else is tried as a native event id within the session.
    Refs that resolve nowhere contribute no turn.
    """
    turns = set()
    for ref in incident.get("event_refs") or []:
        turn = None
        if isinstance(ref, int):
            row = con.execute(
                "SELECT turn_id FROM events WHERE id=? AND session_key=?",
                (ref, session_key)).fetchone()
            turn = row["turn_id"] if row else None
        else:
            row = con.execute(
                "SELECT turn_id FROM submissions WHERE native_id=?",
                (ref,)).fetchone()
            turn = row["turn_id"] if row else None
            if turn is None:
                row = con.execute(
                    "SELECT turn_id FROM events WHERE native_id=?"
                    " AND session_key=?", (ref, session_key)).fetchone()
                turn = row["turn_id"] if row else None
        if turn:
            turns.add(turn)
    return turns


def _incident_model(con, turn_models: dict, session_key: str, incident: dict) -> str:
    """The model of the turn where the incident occurred.

    An incident whose evidence resolves to exactly one modeled turn takes
    that turn's model; anything else (no turn, an unmodeled turn, or
    evidence spanning models) lands in 'mixed' rather than guessed.
    """
    models = {turn_models[t] for t in _incident_turns(con, session_key, incident)
              if t in turn_models}
    return next(iter(models)) if len(models) == 1 else "mixed"


def _model_session_tokens(con, session_key: str, model: str) -> tuple:
    """Known total_tokens sum and unknown count for one model's responses."""
    row = con.execute(
        "SELECT SUM(total_tokens) t,"
        " SUM(CASE WHEN total_tokens IS NULL THEN 1 ELSE 0 END) u"
        " FROM responses WHERE session_key=? AND model=? AND is_overlap=0",
        (session_key, model)).fetchone()
    return row["t"], row["u"] or 0


def _stats(values) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    return {"n": len(values), "median": statistics.median(values),
            "mean": round(statistics.fmean(values), 2), "total": round(sum(values), 2)}


def compare(con, by: str = "agentsmd", **filters) -> dict:
    """Group sessions and report behavior per group with sample sizes."""
    if by == "model":
        return _compare_by_model(con, **filters)
    column = {"agentsmd": "agentsmd_version", "harness": "harness",
              "project": "project_dir"}[by]
    groups: dict = defaultdict(list)
    for s in sessions_in_scope(con, **filters):
        groups[s[column] or "unknown"].append(s)
    rows = []
    for label, members in groups.items():
        metrics = [_session_metrics(con, s) for s in members]
        human = [m for m in metrics if m["genuine_prompts"]]
        row = {"group": label, "sessions": len(members),
               "projects": len({s["project_dir"] for s in members}),
               "sessions_with_human_prompts": len(human),
               "tokens_per_session": _stats([m["tokens"] for m in metrics]),
               "unknown_token_responses": sum(m["tokens_unknown"] for m in metrics),
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


def _compare_by_model(con, **filters) -> dict:
    """Group by response model, not by session majority.

    Tokens attribute per response to that response's model, and a session
    counts under every model it used, so group session counts overlap.
    Incidents attribute to the model of the turn where they occurred; an
    incident whose turn or model is unknown lands in 'mixed', never
    guessed into a model. Prompts stay session-level: they describe the
    sessions in the group, not the model.
    """
    sessions = sessions_in_scope(con, **filters)
    members: dict = defaultdict(list)  # model -> sessions using it
    session_models: dict = {}
    for s in sessions:
        models = sorted(r["model"] for r in con.execute(
            "SELECT DISTINCT model FROM responses WHERE session_key=?"
            " AND model IS NOT NULL", (s["session_key"],)))
        session_models[s["session_key"]] = models
        for model in models:
            members[model].append(s)
    # Incidents per session, attributed once to a turn model or 'mixed'.
    attributed: dict = defaultdict(lambda: defaultdict(int))  # model -> detector -> n
    mixed_sessions: set = set()
    model_tokens: dict = {}  # (session_key, model) -> (known_sum_or_None, unknown_n)
    for s in sessions:
        key = s["session_key"]
        turn_models = _turn_models(con, key)
        labels: dict = defaultdict(int)
        for incident in detect_session(con, s):
            label = _incident_model(con, turn_models, key, incident)
            labels[label] += 1
            attributed[label][incident["detector"]] += 1
        if labels.get("mixed"):
            mixed_sessions.add(key)
        for model in session_models[key]:
            model_tokens[(key, model)] = _model_session_tokens(con, key, model)
    rows = []
    for model, model_sessions in members.items():
        metrics = [_session_metrics(con, s) for s in model_sessions]
        human = [m for m in metrics if m["genuine_prompts"]]
        per_session = [model_tokens[(s["session_key"], model)][0]
                       for s in model_sessions]
        unknown = sum(model_tokens[(s["session_key"], model)][1]
                      for s in model_sessions)
        row = {"group": model, "sessions": len(model_sessions),
               "projects": len({s["project_dir"] for s in model_sessions}),
               "sessions_with_human_prompts": len(human),
               "tokens_per_session": _stats(per_session),
               "unknown_token_responses": unknown,
               "elapsed_s_per_session": _stats([m["elapsed_s"] for m in metrics]),
               "genuine_prompts_per_session": _stats([m["genuine_prompts"] for m in human]),
               "interrupts": sum(m["interrupts"] for m in metrics),
               "agentsmd_read_bytes_per_session": _stats([m["agentsmd_read_bytes"] for m in metrics]),
               "first_seen": min((s["started_at"] or 0) for s in model_sessions) or None,
               "last_seen": max((s["ended_at"] or s["started_at"] or 0)
                                 for s in model_sessions) or None}
        for detector in DETECTORS:
            total = attributed[model][detector]
            row[detector] = {"incidents": total,
                             "per_session": round(total / len(model_sessions), 3)
                             if model_sessions else None}
        rows.append(row)
    if mixed_sessions or attributed["mixed"]:
        mixed_list = [s for s in sessions if s["session_key"] in mixed_sessions]
        metrics = [_session_metrics(con, s) for s in mixed_list]
        human = [m for m in metrics if m["genuine_prompts"]]
        row = {"group": "mixed", "sessions": len(mixed_list),
               "projects": len({s["project_dir"] for s in mixed_list}),
               "sessions_with_human_prompts": len(human),
               "tokens_per_session": {"n": 0},
               "unknown_token_responses": 0,
               "elapsed_s_per_session": _stats([m["elapsed_s"] for m in metrics]),
               "genuine_prompts_per_session": _stats([m["genuine_prompts"] for m in human]),
               "interrupts": sum(m["interrupts"] for m in metrics),
               "agentsmd_read_bytes_per_session": _stats([m["agentsmd_read_bytes"] for m in metrics]),
               "first_seen": min((s["started_at"] or 0) for s in mixed_list) or None
               if mixed_list else None,
               "last_seen": max((s["ended_at"] or s["started_at"] or 0)
                                 for s in mixed_list) or None if mixed_list else None}
        for detector in DETECTORS:
            total = attributed["mixed"][detector]
            row[detector] = {"incidents": total,
                             "per_session": round(total / len(mixed_list), 3)
                             if mixed_list else None}
        rows.append(row)
    rows.sort(key=lambda r: r["group"])
    return {"by": "model", "groups": rows,
            "note": ("Observational comparison: groups differ in period, projects and task "
                     "mix, so differences are leads to inspect, not causal effects. "
                     "Subagent sessions are excluded; every figure carries its sample size. "
                     "A session counts under every model it used, tokens attribute "
                     "per response, and incidents with an unknown turn model land "
                     "in 'mixed'.")}


def _version_key(label: str):
    parts = label.split(".")
    if all(p.isdigit() for p in parts):
        return (0, tuple(int(p) for p in parts))
    return (1, (label,))
