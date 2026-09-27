"""GitHub summaries: one Observer-owned comment per PR or commit.

Rendering is offline and deterministic. Posting happens only through the
explicit publish command, with the GitHub CLI's existing login. A later
publish edits the same comment, found by its hidden marker, and never
touches other comments. Only aggregates leave the machine: no transcripts,
prompts, tool arguments or file contents.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone

from . import analysis, report

MARKER = "<!-- agent-observer:summary v1 -->"

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{6,40}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_GITHUB_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    r"(/((issues|pull|commit)/[A-Za-z0-9_.-]+))?/?$")


def validate_target(repo: str, pr: int | None = None,
                    commit: str | None = None) -> None:
    """Validate an explicit publication target, fail closed."""
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        raise ValueError(f"invalid repo owner/name: {repo!r}")
    if pr is not None and (not isinstance(pr, int) or isinstance(pr, bool)
                           or pr <= 0):
        raise ValueError(f"invalid PR number: {pr!r}")
    if commit is not None and (not isinstance(commit, str)
                               or not _COMMIT_RE.match(commit)):
        raise ValueError(f"invalid commit identifier: {commit!r}")


def _safe_field(value) -> str:
    """Escape dynamic text for the Markdown body.

    Control characters are dropped; table pipes, backticks and HTML
    angle brackets/entities are escaped so model, task and target fields
    cannot break the table or inject markup. Newlines collapse to spaces.
    Underscores and other emphasis marks in closed-vocabulary identifiers
    (counter semantics, detector names) are preserved so existing aggregate
    contracts keep their exact strings. Price sources are routed through
    _safe_source first: only aggregates, identifiers and validated
    references are rendered, and local file paths never reach the body.
    """
    if value is None:
        return "unknown"
    text = str(value)
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t"
                   or (ord(ch) >= 32 and ord(ch) != 127))
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("|", "\\|").replace("`", "\\`")
    text = " ".join(text.split())
    return text[:200] or "unknown"


def _safe_source(value) -> str:
    """A publishable price-source label; local paths never leave the machine.

    Public http(s) schedule and model sources render unchanged
    (Markdown-escaped). Every other value, a file:// URI or a raw path such
    as the bundled fallback, renders as a stable label naming the kind with
    the path withheld, so a requested publish cannot expose a
    home-directory path. The T3 table is recognized by its fixed filename;
    every other local source stays a generic local schedule.
    """
    if value is None or value == "":
        return "unknown"
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        return _safe_field(value)
    if isinstance(value, str) and value.endswith("usage-model-rates.json"):
        return "T3 local rate table (path withheld)"
    return "local schedule (path withheld)"


def _safe_ref(value) -> str | None:
    """A publishable candidate/proof reference, or None when withheld.

    Only public GitHub URLs, owner/name identifiers, numeric PRs, hex
    commit SHAs and fixed-format hashes are rendered. Free-form repair,
    correction, proof, candidate or prompt text is never published.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if _GITHUB_URL_RE.match(text) or _REPO_RE.match(text):
        return _safe_field(text)
    if text.isdigit():
        return _safe_field(text)
    if _COMMIT_RE.match(text) or _SHA_RE.match(text):
        return _safe_field(text)
    return None


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


def summarize(con, session_keys: set, label: str, task_id: str | None = None,
              schedule: dict | None = None) -> dict:
    rep = report.task_report(con, task_id, schedule=schedule) if task_id else None
    if rep is not None:
        session_keys = set(rep["scope_sessions"])
    keys = sorted(session_keys)
    usage = rep["attributed"] if rep is not None else report.scope_totals(con, set(keys))
    models = (rep["models"] if rep is not None else
              report.model_usage(report._responses(con, keys)))
    sessions = [dict(r) for r in con.execute(
        f"SELECT session_key, harness, started_at, ended_at, agentsmd_version FROM sessions"
        f" WHERE session_key IN ({','.join('?' * len(keys))})", keys)] if keys else []
    starts = [s["started_at"] for s in sessions if s["started_at"]]
    ends = [s["ended_at"] for s in sessions if s["ended_at"]]
    session_span = (max(ends) - min(starts)) if starts and ends else None
    if rep is not None:
        counts = dict(rep["diagnostics"]["task_scoped_counts"])
        context_counts = dict(rep["diagnostics"]["session_context_counts"])
        span = rep["time"]["task_elapsed_s"]
        span_source = rep["time"]["task_elapsed_source"]
    else:
        counts = {}
        for s in con.execute(
                f"SELECT * FROM sessions WHERE session_key IN ({','.join('?' * len(keys))})",
                keys) if keys else []:
            for incident in analysis.detect_session(con, s):
                counts[incident["detector"]] = counts.get(incident["detector"], 0) + 1
        context_counts = {}
        span = session_span
        span_source = "session context"
    shared = None
    shared_unknown = 0
    shared_by_semantics = None
    shared_models: list = []
    if rep is not None:
        shared = rep["shared_joint"].get("total_tokens")
        unknown_counts = rep["shared_joint"].get("unknown_counts") or {}
        shared_unknown = unknown_counts.get("total_tokens", 0)
        if "by_semantics" in rep["shared_joint"]:
            shared_by_semantics = rep["shared_joint"]["by_semantics"]
            # Mixed shared scope has no combined total; unknown counts add.
            shared = None
            for sem_totals in shared_by_semantics.values():
                shared_unknown += (sem_totals.get("unknown_counts") or {}).get(
                    "total_tokens", 0)
        shared_models = rep["shared_models"]
    out: dict = {"label": label, "sessions": len(sessions),
            "harnesses": sorted({s["harness"] for s in sessions}),
            "agentsmd_versions": sorted({s["agentsmd_version"] for s in sessions
                                         if s["agentsmd_version"]}),
            "usage": usage, "models": models,
            "span_s": span,
            "span_source": span_source,
            "session_span_s": session_span,
            "incidents": counts, "session_context_incidents": context_counts,
            "shared_tokens": shared,
            "shared_tokens_unknown": shared_unknown,
            "shared_by_semantics": shared_by_semantics,
            "shared_models": shared_models,
            "unknown_usage_sessions": sum(1 for s in sessions if not any(
                m for m in models if m["harness"] == s["harness"]))}
    if rep is not None:
        out.update({
            "scope_kind": "task",
            "task_id": task_id,
            "scope_sessions": rep["scope_sessions"],
            "unassigned_in_scope": rep["unassigned_in_scope"],
            "scope": rep["scope"],
            "phases": rep["phases"],
            "activity_session_context": rep["activity_session_context"],
            "outcome": rep["outcome"],
            "acceptance_state": rep["acceptance_state"],
            "attempts": rep["attempts"],
            "dispatches": rep["dispatches"],
            "active_attempts": rep["active_attempts"],
            "has_active_work": rep["has_active_work"],
            "missing_assignments": rep["missing_assignments"],
            "conflicting_assignments": rep["conflicting_assignments"],
            "joint_assignments": rep["joint_assignments"],
            "crashes_counted_separately": rep["crashes_counted_separately"],
            "reconciles": rep["reconciles"],
            "complete": rep["complete"],
            "coverage": rep["coverage"],
            "snapshot_id": rep["snapshot_id"],
            "price_schedule_id": rep["price_schedule_id"],
            "price_schedule": rep["price_schedule"],
            "source_cutoff": rep["source_cutoff"],
            "measured": rep["measured"],
            "diagnostics": rep["diagnostics"],
            "time": rep["time"],
            "native_cost": rep["native_cost"],
            "native_cost_shared": rep["native_cost_shared"],
            "estimated_cost": rep["estimated_cost"],
            "estimated_cost_shared": rep["estimated_cost_shared"],
            "total_cost": rep["total_cost"],
            "subscription_note": rep["subscription_note"],
        })
    else:
        out.update({
            "scope_kind": "session",
            "scope_sessions": keys,
            "session_note": "Session scope only; not a complete task outcome.",
            "acceptance_state": None,
        })
    return out


def _fmt_cost(value) -> str:
    if value is None:
        return "unknown"
    return f"${value:,.6f}"


def _fmt_bucket_row(model: dict) -> str:
    """One model row's native buckets with semantics, never summed."""
    parts = []
    for bucket, short in (("input_tokens", "input"),
                          ("cached_input_tokens", "cache-read"),
                          ("cache_write_input_tokens", "cache-write"),
                          ("output_tokens", "output"),
                          ("reasoning_output_tokens", "reasoning")):
        unknown = (model.get("unknown_counts") or {}).get(bucket, 0)
        parts.append(f"{short} {_fmt_tokens(model.get(bucket), unknown)}")
    return "; ".join(parts)


def _fmt_rate(rate) -> str:
    """A rate without float noise (0.19999999999999998 renders as 0.2)."""
    if isinstance(rate, (int, float)) and not isinstance(rate, bool):
        return f"{rate:.6g}"
    return _safe_field(rate)


def _price_evidence_lines(summary: dict) -> list[str]:
    """Render numeric rates for reported models, never arbitrary schedule metadata."""
    schedule = summary.get("price_schedule")
    if not schedule:
        return []
    lines = ["", "Selected rates (USD per million tokens). "
             "These rates value the report at the selected schedule date; "
             "they do not establish historical prices or subscription spending.", "",
             "| Model | Input tier | Input | Cache read | Cache write | Other output | Reasoning |",
             "| --- | --- | ---: | ---: | --- | ---: | ---: |"]
    models = {m.get("model") for m in summary.get("models", []) + summary.get("shared_models", [])}
    for model in sorted(models, key=lambda value: value or ""):
        entry = (schedule.get("models") or {}).get(model) or {}
        threshold = entry.get("long_context_threshold")
        tiers = [(f"<= {threshold}" if threshold is not None else "all", entry.get("rates") or {})]
        if threshold is not None:
            tiers.append((f"> {threshold}", entry.get("long_context_rates") or {}))
        for tier, rates in tiers:
            cells = []
            for bucket in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                           "output_tokens", "reasoning_output_tokens"):
                rate = rates.get(bucket)
                if isinstance(rate, dict):
                    rate = "; ".join(f"{ttl}: {_fmt_rate(rate.get(ttl))}" for ttl in ("5m", "1h"))
                cells.append(_fmt_rate(rate))
            lines.append(f"| {_safe_field(model)} | {_safe_field(tier)} | " + " | ".join(cells) + " |")
    lines += ["", "Other output and reasoning are priced without double counting "
              "inclusive native output. Missing rates remain unknown."]
    return lines


FLAG_PHRASES = {
    "repeated_read": ("repeated read", "repeated reads"),
    "repeated_skill_load": ("repeated skill load", "repeated skill loads"),
    "repeated_command": ("repeated command", "repeated commands"),
    "repeated_failure": ("repeated failure", "repeated failures"),
    "test_edit_after_failure": ("test edited after a failure", "tests edited after a failure"),
    "permission_seeking": ("permission request", "permission requests"),
    "human_correction": ("human correction", "human corrections"),
    "large_tool_output": ("large tool output", "large tool outputs"),
}


def _counted(n: int, singular: str, plural: str) -> str:
    return f"{n:,} {singular if n == 1 else plural}"


def _incident(detector: str, n: int) -> str:
    fallback = _safe_field(detector.replace("_", " "))
    return _counted(n, *FLAG_PHRASES.get(detector, (fallback, fallback)))


def _fmt_duration(seconds) -> str | None:
    if seconds is None:
        return None
    seconds = float(seconds)
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _model_cell(m: dict) -> str:
    detail = _safe_field(m.get("harness"))
    if m.get("effort") and m.get("effort") != "unknown":
        detail += f", {_safe_field(m['effort'])}"
    return f"{_safe_field(m.get('model') or 'unknown')} ({detail})"


def _model_cost(m: dict, estimate: dict) -> str:
    key = (m.get("harness"), m.get("model"), m.get("effort"), m.get("semantics"))
    for row in (estimate or {}).get("by_model", []):
        if (row.get("harness"), row.get("model"), row.get("effort"),
                row.get("semantics")) == key:
            return report._money({
                "estimated_cost_usd_total": row.get("estimated_cost_usd"),
                "estimated_cost_usd_partial": row.get("estimated_cost_usd_partial"),
                "priced_responses": row.get("priced_responses")})
    return "unknown"


def _flags(summary: dict) -> list[str]:
    """Short counted phrases from existing diagnostics and coverage gaps."""
    flags = [_incident(k, n) for k, n in sorted(summary.get("incidents", {}).items()) if n]
    if summary.get("scope_kind") != "task":
        return flags
    gaps = summary.get("coverage") or {}
    unpriced = ((summary.get("total_cost") or {}).get("estimate") or {}).get(
        "unpriced_responses", 0)
    for count, singular, plural in (
            (len(summary.get("missing_assignments") or []),
             "prompt without an owner", "prompts without an owner"),
            (len(summary.get("conflicting_assignments") or []),
             "conflicting ownership binding", "conflicting ownership bindings"),
            (len(gaps.get("unbound_usage") or []),
             "session with usage bound to no task", "sessions with usage bound to no task"),
            (len(gaps.get("missing_sessions") or []),
             "missing session record", "missing session records"),
            (len(gaps.get("sessions_without_usage") or []),
             "session without usage records", "sessions without usage records"),
            (len(gaps.get("unbound_worker_sessions") or []),
             "worker session without an owner", "worker sessions without an owner"),
            (len(gaps.get("unbound_dispatches") or []),
             "dispatch without a worker", "dispatches without a worker"),
            (summary.get("crashes_counted_separately") or 0, "crashed run", "crashed runs"),
            ((summary.get("unassigned_in_scope") or {}).get("responses", 0),
             "unassigned response", "unassigned responses"),
            (unpriced or 0, "unpriced response", "unpriced responses")):
        if count:
            flags.append(_counted(count, singular, plural))
    if summary.get("has_active_work"):
        flags.append("work still active")
    if summary.get("reconciles") is False:
        flags.append("totals do not reconcile")
    return flags


def _yes(value) -> str:
    return "yes" if value else "no"


def render(summary: dict, target: str | None = None) -> str:
    """The published comment: a glanceable summary, evidence collapsed.

    Visible: title, one headline (cost, responses, sessions, wall time), a
    per-model table and one flags line. Snapshot, prices, coverage, counter
    semantics and reconciliation sit in one collapsed details block.
    """
    u = summary["usage"]
    task = summary.get("scope_kind") == "task"
    title = {"pr": "Agent work on this PR",
             "commit": "Agent work on this commit"}.get(target, "Agent work")
    total = summary.get("total_cost") or {}
    # A task's own and shared usage render as one set of rows: shared
    # usage is counted whole here and never split.
    models = total.get("models", []) if task else summary["models"]
    sessions = summary["sessions"]
    responses = total.get("responses", 0) if task else u.get("responses", 0)
    wall = _fmt_duration(summary.get("span_s"))
    if wall and "task" not in (summary.get("span_source") or ""):
        wall += " wall time (whole sessions)"
    elif wall:
        wall += " wall time"
    else:
        span = _fmt_duration(summary.get("session_span_s"))
        wall = f"{span} wall time (whole sessions)" if span else "wall time unknown"
    parts = []
    if task:
        parts.append(f"**Estimated cost {_safe_field(total.get('text') or 'unknown')}**")
    parts += [_counted(responses, "response", "responses"),
              _counted(sessions, "session", "sessions"), wall]
    lines = [MARKER, f"### {title}", "", " · ".join(parts), ""]
    if not task:
        lines += [f"_{_safe_field(summary.get('session_note') or 'Session scope only.')} "
                  f"Finished processes never imply an accepted task outcome._", ""]
    cost_col = task
    lines += ["| Model | Responses | Tokens |" + (" Estimated cost |" if cost_col else ""),
              "| --- | ---: | ---: |" + (" ---: |" if cost_col else "")]
    for m in models:
        row = (f"| {_model_cell(m)} | {m['responses']:,} "
               f"| {_fmt_tokens(m['tokens'], m.get('unknown_tokens') or 0)} |")
        if cost_col:
            row += f" {_model_cost(m, total.get('estimate'))} |"
        lines.append(row)
    flags = _flags(summary)
    lines += ["", ("Flags: " + " · ".join(flags)) if flags else "No flags", ""]
    lines += ["<details>", "<summary>Details: snapshot, prices, coverage, counters</summary>", ""]
    harnesses = ", ".join(_safe_field(h) for h in summary['harnesses']) or "no harness"
    versions = ", ".join(_safe_field(v) for v in summary['agentsmd_versions']) or "unknown"
    if task:
        acceptance = summary.get("acceptance_state") or "unknown"
        lines.append(f"- Task `{_safe_field(summary.get('task_id'))}`: outcome "
                     f"{_safe_field(acceptance)} (recorded acceptance only; a finished "
                     f"process never implies it).")
        outcome = summary.get("outcome") or {}
        for key, name in (("candidate", "Candidate"), ("proof_ref", "Proof"),
                          ("repairs", "Repairs"), ("corrections", "Corrections")):
            ref = _safe_ref(outcome.get(key))
            if ref is not None:
                lines.append(f"- {name}: {ref}")
            elif outcome.get(key):
                lines.append(f"- {name}: [withheld: free-form reference not published]")
        cutoff = summary.get("source_cutoff")
        when = (datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                if isinstance(cutoff, (int, float)) else "unknown")
        lines.append(f"- Snapshot `{_safe_field(summary.get('snapshot_id'))}`, "
                     f"records up to {when}.")
    lines.append(f"- {sessions} session{'s' if sessions != 1 else ''} on {harnesses}; "
                 f"AgentsMD {versions}.")
    if task:
        n_other = len(total.get("shared_with_tasks") or [])
        if total.get("shared_responses"):
            lines.append(f"- The total includes work shared with {n_other} other "
                         f"task{'s' if n_other != 1 else ''}, counted whole in each, "
                         f"so sums across PRs overlap.")
        unassigned = (summary.get("unassigned_in_scope") or {}).get("responses", 0)
        if unassigned:
            lines.append(f"- {unassigned:,} responses in these sessions belong to no "
                         f"task and are not counted.")
        lines.append(f"- Totals reconcile with the measured sessions: "
                     f"{_yes(summary.get('reconciles'))}. Evidence complete: "
                     f"{_yes(summary.get('complete'))}.")
        if summary.get("has_active_work"):
            lines.append("- Active work remains visible; reconciled totals alone are "
                         "not completion.")
        est = summary.get("estimated_cost") or {}
        if est.get("schedule_source"):
            lines.append(f"- Prices: list-price estimate from "
                         f"{_safe_source(est.get('schedule_source'))} as of "
                         f"{_safe_field(est.get('schedule_as_of'))}, schedule "
                         f"`{_safe_field(summary.get('price_schedule_id'))}`. "
                         f"Unknown prices stay unknown, never zero.")
            # Per-model bases are more specific; the schedule basis covers the rest.
            by_model = (total.get("estimate") or {}).get("by_model", [])
            bases = {_safe_field(m["basis"]) for m in by_model if m.get("basis")}
            bases = bases or {_safe_field(est.get("basis") or "Standard API list-price equivalent, not subscription spend.")}
            for basis in sorted(bases):
                lines.append(f"  - {basis}")
            sources = {_safe_source(m.get("source_url")) for m in by_model}
            if sources:
                lines.append(f"  - Model price sources: {', '.join(sorted(sources))}.")
            for reason, count in sorted(
                    ((total.get("estimate") or {}).get("unpriced_reasons") or {}).items()):
                lines.append(f"  - Unpriced: {_safe_field(reason)} ({count}).")
        else:
            lines.append("- Prices: no sourced price schedule, so the estimate is unknown.")
        if est.get("coverage_note"):
            lines.append(f"- {_safe_field(est['coverage_note'])}.")
        natives = [summary.get("native_cost") or {}, summary.get("native_cost_shared") or {}]
        known = sum(n.get("known_responses", 0) or 0 for n in natives)
        if not known:
            lines.append("- Harness-reported cost: none reported.")
        else:
            subtotal = sum(n.get("known_subtotal_usd") or 0.0 for n in natives
                           if n.get("known_responses"))
            unknown = sum(n.get("unknown_responses", 0) or 0 for n in natives)
            lines.append(f"- Harness-reported cost (separate from the estimate): "
                         f"{_fmt_cost(subtotal)} over {known:,} responses"
                         + (f"; {unknown:,} responses report none." if unknown else "."))
        lines.append("- Usage totals are not billing. Subscription spending is "
                     "separate and is never posted as spend.")
        lines.append(f"- Crashed runs are counted separately: "
                     f"{summary.get('crashes_counted_separately') or 0}.")
    if summary.get("session_context_incidents"):
        lines.append("- Diagnostics from whole sessions, possibly other work: " + ", ".join(
            _incident(k, n) for k, n in
            sorted(summary["session_context_incidents"].items())) + ".")
    lines += ["", "Token counters by model (native counter semantics; never added "
              "across semantics):"]
    for m in models:
        lines.append(
            f"- {_model_cell(m)}, `{_safe_field(m.get('semantics') or 'unknown')}`: "
            f"{_fmt_bucket_row(m)}; harness total "
            f"{_fmt_tokens(m['tokens'], m.get('unknown_tokens') or 0)}.")
    if models:
        lines += ["", "Generated tokens (reasoning and other output are disjoint; other "
                  "output includes code, tool calls and replies):", "",
                  "| Model | Reasoning | Other output | Reasoning share | Split known for |",
                  "| --- | ---: | ---: | ---: | ---: |"]
        for m in models:
            g = m.get("generation") or {}
            share = g.get("reasoning_share")
            lines.append(f"| {_model_cell(m)} "
                         f"| {_fmt_tokens(g.get('reasoning_tokens'), g.get('unknown_responses', 0))} "
                         f"| {_fmt_tokens(g.get('other_output_tokens'), g.get('unknown_responses', 0))} "
                         f"| {f'{share:.1%}' if share is not None else 'unknown'} "
                         f"| {g.get('measured_responses', 0):,} of {m['responses']:,} |")
    if summary.get("phases"):
        lines += ["", "Usage by work phase (this task's own responses):", "",
                  "| Phase | Model | Responses | Input | Cache read | Cache write | Output | Reasoning | Total | Priced part |",
                  "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for phase in summary["phases"]:
            costs = {(m['harness'], m['model'], m['effort'], m['semantics']): m
                     for m in phase['estimated_cost']['by_model']}
            for m in phase["models"]:
                c = costs.get((m['harness'], m['model'], m['effort'], m['semantics']), {})
                unknown = m.get('unknown_counts') or {}
                buckets = " | ".join(_fmt_tokens(m.get(k), unknown.get(k, 0)) for k in report.BUCKETS)
                cost = _fmt_cost(c.get('estimated_cost_usd_partial') if c.get('priced_responses') else None)
                lines.append(f"| {_safe_field(phase['phase'])} | {_model_cell(m)} "
                             f"| {m['responses']} | {buckets} | {cost} |")
        lines += ["", "Recorded activity by phase (observations, not separate token bills):", "",
                  "| Phase | Tool calls | MCP results | Reads | File changes | Failed tool results |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for phase in summary['phases']:
            a = phase['activity']
            counts = " | ".join(str(a.get(k, 0)) for k in
                                ('tool_calls', 'mcp_results', 'reads', 'file_changes', 'failed_tool_results'))
            lines.append(f"| {_safe_field(phase['phase'])} | {counts} |")
    lines += _price_evidence_lines(summary)
    lines += ["", "</details>", "",
              "<sub>Local measurement from native records; usage totals are not billing. "
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
    validate_target(repo, pr=pr, commit=commit)
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
