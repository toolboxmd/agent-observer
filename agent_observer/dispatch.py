"""Dispatch-mode comparison: planner dispatch versus a dispatcher thread.

A job is one planner thread and the PR (or, lacking one, the Issue) that
its children deliver. The planner's direct children are the job's
threads when their subtree links that PR or Issue in T3. The mode comes
from the tree: a ``dispatcher-`` child makes it a dispatcher job; other
role-named children (``worker-``, ``reviewer-``, ...) make it a planner
job; children without a role name leave the mode unknown.

Thread trees, creation times, turn starts and links come from T3's
``state.sqlite``, opened read-only at report time. Tokens and cost come
from the ledger. A child thread that serves several jobs is split evenly
between them. A planner turn counts toward the jobs whose children it
touched: a child reported to it, it created a child, or it messaged a
child. A turn touching several jobs is split evenly between them.

Approval comes from GitHub through the existing ``gh`` login: the first
``review/independent`` success status on any PR commit or the first
approving review. Anything unavailable stays unknown, never zero.

Only identifiers and timestamps are read into memory from T3 message
rows; the reporter thread id is parsed from a report's first line and no
message text is stored or printed.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import subprocess

from . import pricing
from .adapters import t3 as _t3
from .ingest import iso_ts

ROLES = ("worker", "dispatcher", "reviewer", "retry", "escalation")
WORKER_ROLES = ("worker", "retry", "escalation")
REPORT_RE = re.compile(r"\(thread (sub\.[A-Za-z0-9._-]{1,400})\) finished a turn\]")
REVIEW_CONTEXT = "review/independent"


def parent(thread_id: str) -> str | None:
    """The parent of ``sub.<parent>.<suffix>``; None for a root thread."""
    if not thread_id.startswith("sub."):
        return None
    head, _, _ = thread_id[len("sub."):].rpartition(".")
    return head or None


def role(thread_id: str) -> str | None:
    """The Prism role named by a child's suffix, or None."""
    if not thread_id.startswith("sub."):
        return None
    suffix = thread_id.rpartition(".")[2]
    name = suffix.split("-", 1)[0]
    return name if name in ROLES and "-" in suffix else None


def _top_child(thread_id: str, planner: str) -> str | None:
    """The planner's direct child whose subtree holds ``thread_id``."""
    current = thread_id
    while current is not None:
        up = parent(current)
        if up == planner:
            return current
        current = up
    return None


def _read_t3(path: str, planners: list[str] | None) -> dict:
    native = _t3._open_read_only(path)
    try:
        tables = _t3._tables(native)
        created: dict[str, float] = {}
        if "projection_threads" in tables:
            for row in native.execute(
                    "SELECT thread_id, created_at FROM projection_threads"):
                ts = iso_ts(row["created_at"])
                if isinstance(row["thread_id"], str) and ts is not None:
                    created[row["thread_id"]] = ts
        roots = sorted({_t3.thread_root(t) for t in created
                        if t.startswith("sub.")})
        if planners:
            roots = [r for r in roots if r in planners]
        members = {t for t in created
                   if t in roots or _t3.thread_root(t) in roots}
        links = []
        if "projection_thread_pull_requests" in tables:
            for row in native.execute(
                    "SELECT thread_id, repository, number, snapshot_json"
                    " FROM projection_thread_pull_requests"):
                if row["thread_id"] not in members:
                    continue
                try:
                    snap = json.loads(row["snapshot_json"] or "{}")
                except (ValueError, TypeError):
                    snap = {}
                state = snap.get("state") if isinstance(snap, dict) else None
                links.append({"thread_id": row["thread_id"], "kind": "pr",
                              "item": f"{row['repository']}#{row['number']}",
                              "state": state if isinstance(state, str) else None})
        if "fork_thread_issue_links" in tables:
            for row in native.execute(
                    "SELECT thread_id, repository, number"
                    " FROM fork_thread_issue_links"):
                if row["thread_id"] in members:
                    links.append({"thread_id": row["thread_id"],
                                  "kind": "issue", "state": None,
                                  "item": f"{row['repository']}#{row['number']}"})
        turns: dict[str, list] = {}
        if "orchestration_events" in tables:
            for row in native.execute(
                    "SELECT stream_id, actor_kind, occurred_at, payload_json"
                    " FROM orchestration_events WHERE event_type=?"
                    " ORDER BY sequence", (_t3.TURN_START_EVENT,)):
                if row["stream_id"] not in members:
                    continue
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except (ValueError, TypeError):
                    continue
                ts = iso_ts(row["occurred_at"])
                if ts is None or not isinstance(payload, dict):
                    continue
                turns.setdefault(row["stream_id"], []).append(
                    {"ts": ts, "actor": row["actor_kind"],
                     "message_id": payload.get("messageId"),
                     "reporter": None})
        if "projection_thread_messages" in tables:
            for thread, items in turns.items():
                ids = [t["message_id"] for t in items
                       if isinstance(t["message_id"], str)
                       and t["actor"] == "server"]
                reporters = {}
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    for row in native.execute(
                            "SELECT message_id, substr(text, 1, 1000) AS head"
                            " FROM projection_thread_messages WHERE message_id"
                            f" IN ({','.join('?' * len(chunk))})", chunk):
                        first = (row["head"] or "").split("\n", 1)[0]
                        hit = REPORT_RE.search(first)
                        if hit:
                            reporters[row["message_id"]] = hit.group(1)
                for t in items:
                    t["reporter"] = reporters.get(t["message_id"])
    finally:
        native.close()
    return {"roots": roots, "created": created, "links": links, "turns": turns}


def _jobs(state: dict) -> list[dict]:
    """One job per delivered PR, or per Issue when its children link no PR."""
    jobs = []
    for planner in state["roots"]:
        subtree_items: dict[str, dict[str, set]] = {}
        pr_state: dict[str, str | None] = {}
        for link in state["links"]:
            top = _top_child(link["thread_id"], planner)
            if top is None:
                continue
            kinds = subtree_items.setdefault(top, {"pr": set(), "issue": set()})
            kinds[link["kind"]].add(link["item"])
            if link["kind"] == "pr":
                pr_state[link["item"]] = link["state"] or pr_state.get(link["item"])
        keys: dict[str, dict] = {}
        for top, kinds in sorted(subtree_items.items()):
            delivered = kinds["pr"] or kinds["issue"]
            for item in delivered:
                job = keys.setdefault(item, {
                    "planner": planner, "job": item,
                    "kind": "pr" if kinds["pr"] else "issue",
                    "children": set(), "items": set(),
                    "pr_state": pr_state.get(item)})
                job["children"].add(top)
                job["items"] |= kinds["pr"] | kinds["issue"]
        jobs.extend(keys[k] for k in sorted(keys))
    return jobs


def _mode(job: dict) -> str:
    roles = {role(c) for c in job["children"]}
    if "dispatcher" in roles:
        return "dispatcher"
    if roles - {None}:
        return "planner"
    return "unknown"


def _turn_windows(turns: list) -> list[tuple]:
    starts = sorted(turns, key=lambda t: t["ts"])
    out = []
    for i, turn in enumerate(starts):
        end = starts[i + 1]["ts"] if i + 1 < len(starts) else float("inf")
        out.append((turn["ts"], end, turn))
    return out


def _window_at(windows: list, ts: float):
    for start, end, turn in windows:
        if start <= ts < end:
            return start, end, turn
    return None


def thread_sessions(con: sqlite3.Connection, thread_id: str) -> set:
    """Ledger session keys behind one T3 thread, rotated Claude ones too."""
    keys: set = set()
    try:
        natives = [r["native_session"] for r in con.execute(
            "SELECT native_session FROM t3_threads WHERE thread_id=?",
            (thread_id,))]
    except sqlite3.DatabaseError:
        natives = []
    for native in natives:
        for row in con.execute(
                "SELECT session_key FROM sessions WHERE (harness='claude'"
                " AND (native_id=? OR substr(native_id, 1, ?)=?))"
                " OR (harness IN ('codex','opencode') AND native_id=?)",
                (native, len(native) + 1, native + ":", native)):
            keys.add(row["session_key"])
    try:
        for row in con.execute(
                "SELECT DISTINCT s.session_key FROM submissions s"
                " JOIN t3_turn_origins o ON s.native_id='claude:' || o.message_id"
                " WHERE o.thread_id=?", (thread_id,)):
            keys.add(row["session_key"])
    except sqlite3.DatabaseError:
        pass
    return keys


def _responses(con, keys: set) -> list[dict]:
    if not keys:
        return []
    keys = sorted(keys)
    return [dict(r) for r in con.execute(
        "SELECT * FROM responses WHERE is_overlap=0 AND session_key IN"
        f" ({','.join('?' * len(keys))})", keys)]


def _share(acc: dict, rows: list, weight: float, schedule: dict) -> None:
    """Add a weighted share of responses to one job's totals."""
    for row in rows:
        acc["responses"] += 1
        tokens = row.get("total_tokens")
        if tokens is None:
            acc["unknown_tokens"] += 1
        else:
            acc["tokens"] += tokens * weight
        cost, _ = pricing.price_response(row, schedule)
        if cost is None:
            acc["unpriced"] += 1
        else:
            acc["cost"] += cost * weight


def _gh_json(args: list):
    proc = subprocess.run(["gh", *args], capture_output=True, text=True,
                          timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh failed")
    return json.loads(proc.stdout or "null")


def fetch_approval(item: str) -> dict:
    """First approving review time for ``owner/repo#N`` from GitHub.

    Returns ``{"approved_at": epoch | None, "error": str | None}``. An
    unreachable GitHub is an error (unknown), not a missing approval.
    """
    repo, _, number = item.partition("#")
    try:
        pr = _gh_json(["pr", "view", number, "-R", repo,
                       "--json", "reviews,commits"])
        times = [iso_ts(r.get("submittedAt")) for r in pr.get("reviews") or []
                 if r.get("state") == "APPROVED"]
        for commit in pr.get("commits") or []:
            sha = commit.get("oid")
            if not isinstance(sha, str):
                continue
            statuses = _gh_json(["api", f"repos/{repo}/commits/{sha}/statuses",
                                 "--paginate"])
            times += [iso_ts(s.get("created_at")) for s in statuses or []
                      if s.get("context") == REVIEW_CONTEXT
                      and s.get("state") == "success"]
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"approved_at": None, "error": str(exc)[:200]}
    times = [t for t in times if t is not None]
    return {"approved_at": min(times) if times else None, "error": None}


def compare(con: sqlite3.Connection, t3_path: str,
            planners: list[str] | None = None, jobs: list[str] | None = None,
            since: float | None = None, until: float | None = None,
            schedule: dict | None = None, approval=None) -> dict:
    approval = approval or fetch_approval
    state = _read_t3(t3_path, planners)
    schedule = schedule or pricing.default_schedule(con)
    found = _jobs(state)
    windows = {p: _turn_windows(state["turns"].get(p, []))
               for p in state["roots"]}
    created = state["created"]

    for job in found:
        start = min((created[c] for c in job["children"] if c in created),
                    default=None)
        hit = _window_at(windows[job["planner"]], start) if start else None
        job["start"] = hit[0] if hit else start
    if jobs:
        wanted = set(jobs)
        found = [j for j in found if j["job"] in wanted or j["items"] & wanted]
    found = [j for j in found if j["start"] is not None
             and (since is None or j["start"] >= since)
             and (until is None or j["start"] < until)]

    # Child subtrees shared by several selected jobs split evenly.
    owners: dict[str, list] = {}
    for job in found:
        for child in job["children"]:
            owners.setdefault(child, []).append(job)
    results = {id(j): {"responses": 0, "tokens": 0.0, "unknown_tokens": 0,
                       "cost": 0.0, "unpriced": 0, "shared_threads": 0,
                       "unmapped_threads": 0,
                       "planner_turns": 0, "shared_planner_turns": 0}
               for j in found}
    for child, owning in owners.items():
        subtree = [t for t in created if t == child
                   or _top_child(t, owning[0]["planner"]) == child]
        mapped = {t: thread_sessions(con, t) for t in subtree}
        # A thread with no ledger session (a Grok child, an unsynced
        # thread) has usage Observer cannot see: the job's usage is unknown.
        unmapped = sum(1 for keys in mapped.values() if not keys)
        rows = _responses(con, set().union(*mapped.values()))
        for job in owning:
            acc = results[id(job)]
            acc["shared_threads"] += len(owning) > 1
            acc["unmapped_threads"] += unmapped
            _share(acc, rows, 1.0 / len(owning), schedule)

    # Planner coordination turns.
    by_child = {c: owners[c] for c in owners}
    for planner in {j["planner"] for j in found}:
        planner_keys = thread_sessions(con, planner)
        prow = _responses(con, planner_keys)
        children_events: dict[tuple, set] = {}
        for start, end, turn in windows[planner]:
            touched = set()
            if turn["reporter"]:
                top = _top_child(turn["reporter"], planner)
                if top:
                    touched.add(top)
            for thread, ts in created.items():
                if parent(thread) == planner and start <= ts < end:
                    touched.add(thread)
            for thread, items in state["turns"].items():
                if parent(thread) != planner:
                    continue
                if any(start <= t["ts"] < end and t["actor"] == "server"
                       and not t["reporter"] for t in items):
                    touched.add(thread)
            owning = {id(j): j for c in touched for j in by_child.get(c, [])
                      if j["planner"] == planner}
            if owning:
                children_events[(start, end)] = owning
        for (start, end), owning in children_events.items():
            rows = [r for r in prow if r.get("ts") is not None
                    and start <= r["ts"] < end]
            for job in owning.values():
                acc = results[id(job)]
                if not planner_keys and not acc["planner_turns"]:
                    acc["unmapped_threads"] += 1
                acc["planner_turns"] += 1
                acc["shared_planner_turns"] += len(owning) > 1
                _share(acc, rows, 1.0 / len(owning), schedule)

    out_jobs = []
    for job in found:
        acc = results[id(job)]
        workers = [created[t] for t in created
                   if role(t) in WORKER_ROLES
                   and _top_child(t, job["planner"]) in job["children"]]
        first_worker = min(workers) - job["start"] if workers else None
        merged = job["pr_state"] == "merged"
        if job["kind"] == "pr":
            review = approval(job["job"])
        else:
            review = {"approved_at": None, "error": "no PR linked"}
        approved_at = review["approved_at"]
        if merged or approved_at is not None:
            succeeded = True
        elif review["error"] and job["kind"] == "pr":
            succeeded = None
        else:
            succeeded = False
        out_jobs.append({
            "planner": job["planner"], "job": job["job"],
            "mode": _mode(job), "children": sorted(job["children"]),
            "start": job["start"], "pr_state": job["pr_state"],
            "succeeded": succeeded,
            "first_worker_s": first_worker,
            "approved_at": approved_at,
            "approval_s": approved_at - job["start"]
            if approved_at is not None else None,
            "approval_error": review["error"],
            "responses": acc["responses"],
            "tokens": None if acc["unknown_tokens"] or acc["unmapped_threads"]
            or not acc["responses"] else round(acc["tokens"]),
            "cost_usd": None if acc["unpriced"] or acc["unmapped_threads"]
            or not acc["responses"] else acc["cost"],
            "unmapped_threads": acc["unmapped_threads"],
            "unknown_token_responses": acc["unknown_tokens"],
            "unpriced_responses": acc["unpriced"],
            "shared_threads": acc["shared_threads"],
            "planner_turns": acc["planner_turns"],
            "shared_planner_turns": acc["shared_planner_turns"]})
    modes = {m: _summary([j for j in out_jobs if j["mode"] == m])
             for m in ("planner", "dispatcher", "unknown")}
    return {"jobs": out_jobs, "modes": modes,
            "cost_basis": schedule.get("basis",
                                       "API list-price equivalent; not subscription spend"),
            "note": "Tokens are each harness's own total; cost is a list-price"
                    " estimate. Shared threads and planner turns split evenly"
                    " between the jobs they served."}


def _median(values: list):
    known = [v for v in values if v is not None]
    return {"value": statistics.median(known) if known else None,
            "n": len(known)}


def _mean(values: list):
    known = [v for v in values if v is not None]
    return {"value": sum(known) / len(known) if known else None,
            "n": len(known)}


def _summary(jobs: list) -> dict:
    known = [j["succeeded"] for j in jobs if j["succeeded"] is not None]
    return {"jobs": len(jobs),
            "succeeded": {"value": sum(known) if known else None,
                          "n": len(known)},
            "success_rate": {"value": sum(known) / len(known) if known else None,
                             "n": len(known)},
            "median_first_worker_s": _median([j["first_worker_s"] for j in jobs]),
            "median_approval_s": _median([j["approval_s"] for j in jobs]),
            "tokens_per_job": _mean([j["tokens"] for j in jobs]),
            "cost_per_job_usd": _mean([j["cost_usd"] for j in jobs])}


def _fmt(cell: dict, kind: str) -> str:
    value, n = cell["value"], cell["n"]
    if value is None:
        text = "unknown"
    elif kind == "s":
        text = f"{value / 60:.1f} min"
    elif kind == "rate":
        text = f"{value:.0%}"
    elif kind == "usd":
        text = f"${value:,.2f}"
    else:
        text = f"{round(value):,}"
    return f"{text} (n={n})"


def render(payload: dict) -> str:
    lines = []
    for mode in ("planner", "dispatcher", "unknown"):
        s = payload["modes"][mode]
        if mode == "unknown" and not s["jobs"]:
            continue
        lines.append(
            f"{mode} mode: {s['jobs']} jobs, succeeded "
            f"{_fmt(s['succeeded'], 'n')}, success rate "
            f"{_fmt(s['success_rate'], 'rate')}, median time to first worker "
            f"{_fmt(s['median_first_worker_s'], 's')}, median time to approving"
            f" review {_fmt(s['median_approval_s'], 's')}, tokens per job "
            f"{_fmt(s['tokens_per_job'], 'n')}, cost per job "
            f"{_fmt(s['cost_per_job_usd'], 'usd')}")
    for j in payload["jobs"]:
        times = ", ".join(
            f"{name} " + ("unknown" if j[k] is None else f"{j[k] / 60:.1f} min")
            for name, k in (("first worker", "first_worker_s"),
                            ("approving review", "approval_s")))
        tokens = "unknown" if j["tokens"] is None else f"{j['tokens']:,}"
        cost = "unknown" if j["cost_usd"] is None else f"${j['cost_usd']:,.2f}"
        success = {True: "yes", False: "no", None: "unknown"}[j["succeeded"]]
        lines.append(
            f"- {j['job']} [{j['mode']}] succeeded {success}, {times}, tokens"
            f" {tokens}, cost {cost}, planner turns {j['planner_turns']}"
            f" ({j['shared_planner_turns']} shared)")
    if not payload["jobs"]:
        lines.append("no jobs matched")
    lines.append(payload["note"])
    return "\n".join(lines)
