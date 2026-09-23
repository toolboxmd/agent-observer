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


def _fmt_tokens(value, unknown: int = 0) -> str:
    """A counter that stays honest when native evidence is missing.

    Fully unknown renders as unknown; a partial sum renders as an
    explicitly labeled lower bound, never as an exact figure.
    """
    if value is None:
        return "unknown"
    text = f"{int(value):,}"
    if unknown:
        return f">={text} (lower bound)"
    return text


def summarize(con, session_keys: set, label: str, task_id: str | None = None) -> dict:
    keys = sorted(session_keys)
    usage = report.scope_totals(con, set(keys))
    models = [dict(r) for r in con.execute(
        f"SELECT harness, model, effort, COUNT(*) responses, SUM(total_tokens) tokens,"
        f" SUM(CASE WHEN total_tokens IS NULL THEN 1 ELSE 0 END) unknown_tokens"
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
    shared_unknown = 0
    if task_id:
        rep = report.task_report(con, task_id)
        shared = rep["shared_joint"]["total_tokens"]
        shared_unknown = (rep["shared_joint"].get("unknown_counts") or {}).get(
            "total_tokens", 0)
    return {"label": label, "sessions": len(sessions),
            "harnesses": sorted({s["harness"] for s in sessions}),
            "agentsmd_versions": sorted({s["agentsmd_version"] for s in sessions
                                         if s["agentsmd_version"]}),
            "usage": usage, "models": models,
            "span_s": (max(ends) - min(starts)) if starts and ends else None,
            "incidents": counts, "shared_tokens": shared,
            "shared_tokens_unknown": shared_unknown,
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
                     f"| {m['responses']} | {_fmt_tokens(m['tokens'], m.get('unknown_tokens') or 0)} |")
    lines += ["", f"Total tokens {_fmt_tokens(u['total_tokens'], (u.get('unknown_counts') or {}).get('total_tokens', 0))} over {u['responses']} responses "
              f"(each harness's own total; cache and reasoning buckets are not added across "
              f"harnesses)."]
    if summary["shared_tokens"] or summary.get("shared_tokens_unknown"):
        lines.append(f"Shared with other tasks and not divided: {_fmt_tokens(summary['shared_tokens'], summary.get('shared_tokens_unknown') or 0)} tokens.")
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


def _authenticated_login() -> str:
    """The GitHub user Observer posts as, via the existing gh login."""
    me = _gh(["user"])
    login = me.get("login") if isinstance(me, dict) else None
    if not login:
        raise RuntimeError("gh api user returned no login; refusing to touch comments")
    return login


def _comment_author(comment: dict):
    """Author login of a comment, for issue and commit comments alike."""
    for key in ("user", "author"):
        author = comment.get(key)
        if isinstance(author, dict) and author.get("login"):
            return author["login"]
    return None


def _comment_time(comment: dict) -> tuple:
    return (comment.get("created_at") or "", comment.get("updated_at") or "",
            comment.get("id") or 0)


def _list_all_comments(listing: str) -> list:
    """Every comment on the listing, across all pages.

    The first page is fetched at the given listing URL; later pages
    append page numbers. Collection stops at the first short or empty
    page, with no page cap, so a marker at any position is found.
    """
    comments: list = []
    page = 1
    while True:
        url = listing if page == 1 else f"{listing}&page={page}"
        batch = _gh([url])
        if not isinstance(batch, list):
            break
        comments.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return comments


def post(repo: str, body: str, pr: int | None = None, commit: str | None = None) -> dict:
    """Create or update the Observer-owned comment on a PR or a commit.

    Only a marker comment authored by the authenticated gh user is ever
    patched: a foreign or spoofed marker is left alone and a new comment
    is created beside it. The full comment list is paginated before
    markers are selected; when several owned markers exist, the newest
    is updated and the duplicates are reported.
    """
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
    me = _authenticated_login()
    comments = _list_all_comments(listing)
    markers = [c for c in comments if MARKER in (c.get("body") or "")]
    owned = sorted((c for c in markers if _comment_author(c) == me),
                   key=_comment_time)
    if owned:
        target = owned[-1]
        comment = _gh(["-X", "PATCH", edit.format(id=target["id"])], {"body": body})
        result = {"action": "updated", "id": comment.get("id"),
                  "url": comment.get("html_url")}
        if len(owned) > 1:
            result["duplicates"] = len(owned) - 1
            result["duplicate_ids"] = [c.get("id") for c in owned[:-1]]
        return result
    comment = _gh(["-X", "POST", create], {"body": body})
    result = {"action": "created", "id": comment.get("id"),
              "url": comment.get("html_url")}
    foreign = [c.get("id") for c in markers if _comment_author(c) != me]
    if foreign:
        result["foreign_markers_left_alone"] = foreign
    return result
