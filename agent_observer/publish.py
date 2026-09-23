"""GitHub summaries: one Observer-owned comment per PR or commit.

Rendering is offline and deterministic. Posting happens only through the
explicit publish command, with the GitHub CLI's existing login. A later
publish edits the same comment, found by its hidden marker, and never
touches other comments. Only aggregates leave the machine: no transcripts,
prompts, tool arguments or file contents.
"""

from __future__ import annotations

import json
import subprocess

from . import analysis, report

MARKER = "<!-- agent-observer:summary v1 -->"


def _fmt(n) -> str:
    return "unknown" if n is None else f"{int(n):,}"


def summarize(con, session_keys: set, label: str, task_id: str | None = None) -> dict:
    keys = sorted(session_keys)
    usage = report.scope_totals(con, set(keys))
    models = [dict(r) for r in con.execute(
        f"SELECT harness, model, effort, COUNT(*) responses, SUM(total_tokens) tokens"
        f" FROM responses WHERE is_overlap=0 AND session_key IN ({','.join('?' * len(keys))})"
        f" GROUP BY harness, model, effort ORDER BY tokens DESC", keys)] if keys else []
    sessions = [dict(r) for r in con.execute(
        f"SELECT session_key, harness, started_at, ended_at, agentsmd_version FROM sessions"
        f" WHERE session_key IN ({','.join('?' * len(keys))})", keys)] if keys else []
    starts = [s["started_at"] for s in sessions if s["started_at"]]
    ends = [s["ended_at"] for s in sessions if s["ended_at"]]
    counts: dict = {}
    for s in con.execute(
            f"SELECT * FROM sessions WHERE session_key IN ({','.join('?' * len(keys))})",
            keys) if keys else []:
        for incident in analysis.detect_session(con, s):
            counts[incident["detector"]] = counts.get(incident["detector"], 0) + 1
    shared = 0
    if task_id:
        rep = report.task_report(con, task_id)
        shared = rep["shared_joint"]["total_tokens"]
    return {"label": label, "sessions": len(sessions),
            "harnesses": sorted({s["harness"] for s in sessions}),
            "agentsmd_versions": sorted({s["agentsmd_version"] for s in sessions
                                         if s["agentsmd_version"]}),
            "usage": usage, "models": models,
            "span_s": (max(ends) - min(starts)) if starts and ends else None,
            "incidents": counts, "shared_tokens": shared,
            "unknown_usage_sessions": sum(1 for s in sessions if not any(
                m for m in models if m["harness"] == s["harness"]))}


def render(summary: dict) -> str:
    u = summary["usage"]
    lines = [MARKER, f"### Agent Observer: {summary['label']}", "",
             f"{summary['sessions']} session{'s' if summary['sessions'] != 1 else ''} on "
             f"{', '.join(summary['harnesses']) or 'no harness'}; "
             f"AgentsMD {', '.join(summary['agentsmd_versions']) or 'unknown'}; "
             f"span {_fmt(summary['span_s'])} s.", "",
             "| Harness | Model | Effort | Responses | Tokens |", "| --- | --- | --- | ---: | ---: |"]
    for m in summary["models"]:
        lines.append(f"| {m['harness']} | {m['model'] or 'unknown'} | {m['effort'] or 'unknown'} "
                     f"| {m['responses']} | {_fmt(m['tokens'])} |")
    lines += ["", f"Total tokens {_fmt(u['total_tokens'])} over {u['responses']} responses "
              f"(each harness's own total; cache and reasoning buckets are not added across "
              f"harnesses)."]
    if summary["shared_tokens"]:
        lines.append(f"Shared with other tasks and not divided: {_fmt(summary['shared_tokens'])} tokens.")
    if summary["incidents"]:
        lines += ["", "Diagnostics (candidates, not verdicts): " + ", ".join(
            f"{k.replace('_', ' ')} {v}" for k, v in sorted(summary["incidents"].items()))]
    lines += ["", "<sub>Local measurement from native records; usage totals are not billing. "
              "Updated in place by `agent-observer publish`.</sub>"]
    return "\n".join(lines) + "\n"


def _gh(args: list, payload: dict | None = None) -> dict | list:
    proc = subprocess.run(["gh", "api", *args] + (["--input", "-"] if payload else []),
                          input=json.dumps(payload) if payload else None,
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh api failed: {args}")
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def post(repo: str, body: str, pr: int | None = None, commit: str | None = None) -> dict:
    """Create or update the one Observer comment on a PR or a commit."""
    if (pr is None) == (commit is None):
        raise ValueError("name exactly one of a PR number or a commit SHA")
    if pr is not None:
        listing = f"repos/{repo}/issues/{pr}/comments?per_page=100"
        create = f"repos/{repo}/issues/{pr}/comments"
        edit = f"repos/{repo}/issues/comments/{{id}}"
    else:
        listing = f"repos/{repo}/commits/{commit}/comments?per_page=100"
        create = f"repos/{repo}/commits/{commit}/comments"
        edit = f"repos/{repo}/comments/{{id}}"
    existing = [c for c in _gh([listing]) if MARKER in (c.get("body") or "")]
    if existing:
        comment = _gh(["-X", "PATCH", edit.format(id=existing[0]["id"])], {"body": body})
        return {"action": "updated", "id": comment.get("id"), "url": comment.get("html_url")}
    comment = _gh(["-X", "POST", create], {"body": body})
    return {"action": "created", "id": comment.get("id"), "url": comment.get("html_url")}
