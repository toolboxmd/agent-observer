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
    contracts keep their exact strings. Local paths and excerpts never
    reach here: only aggregates, identifiers and validated references are
    rendered.
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
            "timing": rep["timing"],
            "attempt_timing": rep["attempt_timing"],
            "failures": rep["failures"],
            "job_outcomes": rep["job_outcomes"],
            "recovery": rep["recovery"],
            "usage_coverage": rep["usage_coverage"],
            "native_cost": rep["native_cost"],
            "native_cost_shared": rep["native_cost_shared"],
            "estimated_cost": rep["estimated_cost"],
            "estimated_cost_shared": rep["estimated_cost_shared"],
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


def _price_evidence_lines(summary: dict) -> list[str]:
    """Render numeric rates for reported models, never arbitrary schedule metadata."""
    schedule = summary.get("price_schedule")
    if not schedule:
        return []
    lines = ["", f"Price schedule `{_safe_field(summary.get('price_schedule_id'))}`.",
             "", "<details>", "<summary>Selected rates (USD per million tokens)</summary>", "",
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
                    rate = "; ".join(f"{ttl}: {_safe_field(rate.get(ttl))}" for ttl in ("5m", "1h"))
                cells.append(_safe_field(rate))
            lines.append(f"| {_safe_field(model)} | {_safe_field(tier)} | " + " | ".join(cells) + " |")
    lines += ["", "Other output and reasoning are priced without double counting "
              "inclusive native output. Missing rates remain unknown.", "", "</details>"]
    return lines


def render(summary: dict) -> str:
    u = summary["usage"]
    label = _safe_field(summary['label'])
    harnesses = ", ".join(_safe_field(h) for h in summary['harnesses']) or "no harness"
    versions = ", ".join(_safe_field(v) for v in summary['agentsmd_versions']) or "unknown"
    span_note = ""
    if summary.get("scope_kind") == "task":
        source = summary.get("span_source") or ""
        if summary.get("span_s") is None:
            span_note = f" (task time {source}; session span {_fmt(summary.get('session_span_s'))} s, session context)"
        elif "task" in source:
            span_note = " (task turns)"
        else:
            span_note = f" ({_safe_field(source)})"
    lines = [MARKER, f"### Agent Observer: {label}", "",
             f"{summary['sessions']} session{'s' if summary['sessions'] != 1 else ''} on "
             f"{harnesses}; "
             f"AgentsMD {versions}; "
             f"span {_fmt(summary['span_s'])} s{span_note}.", ""]
    if summary.get("scope_kind") == "session":
        lines += [f"_{_safe_field(summary.get('session_note') or 'Session scope only.')} "
                  f"Finished processes never imply an accepted task outcome._", ""]
    sems = {m.get("semantics") for m in summary["models"]}
    if len(sems) > 1:
        lines += ["| Harness | Model | Effort | Semantics | Responses | Tokens |",
                  "| --- | --- | --- | --- | ---: | ---: |"]
        for m in summary["models"]:
            lines.append(
                f"| {_safe_field(m['harness'])} | {_safe_field(m['model'] or 'unknown')} | {_safe_field(m['effort'] or 'unknown')} "
                f"| {_safe_field(m.get('semantics') or 'unknown')} "
                f"| {m['responses']} | {_fmt_tokens(m['tokens'], m.get('unknown_tokens') or 0)} |")
    else:
        lines += ["| Harness | Model | Effort | Responses | Tokens |",
                  "| --- | --- | --- | ---: | ---: |"]
        for m in summary["models"]:
            lines.append(
                f"| {_safe_field(m['harness'])} | {_safe_field(m['model'] or 'unknown')} | {_safe_field(m['effort'] or 'unknown')} "
                f"| {m['responses']} | {_fmt_tokens(m['tokens'], m.get('unknown_tokens') or 0)} |")
    if summary["models"]:
        lines += ["", "Generated tokens (reasoning and other output are disjoint):",
                  "| Model / harness | Effort | Reasoning | Other output | Reasoning share | Split coverage |",
                  "| --- | --- | ---: | ---: | ---: | ---: |"]
        for m in summary["models"]:
            g = m.get("generation") or {}
            share = g.get("reasoning_share")
            share_text = f"{share:.1%}" if share is not None else "unknown"
            lines.append(f"| {_safe_field(m['model'])} / {_safe_field(m['harness'])} | {_safe_field(m['effort'])} "
                         f"| {_fmt_tokens(g.get('reasoning_tokens'), g.get('unknown_responses', 0))} "
                         f"| {_fmt_tokens(g.get('other_output_tokens'), g.get('unknown_responses', 0))} "
                         f"| {share_text} | {g.get('measured_responses', 0)}/{m['responses']} |")
        lines.append("Other output includes code, tool calls and replies. Reasoning share uses generated tokens, not input/cache tokens.")
        lines += ["", "Attributed token buckets by model "
                  "(native semantics, never added across semantics):"]
        for m in summary["models"]:
            lines.append(
                f"- {_safe_field(m['harness'])}/{_safe_field(m['model'] or 'unknown')}"
                f" ({_safe_field(m['effort'] or 'unknown')}, "
                f"{_safe_field(m.get('semantics') or 'unknown')}): "
                f"{_fmt_bucket_row(m)}; harness total "
                f"{_fmt_tokens(m['tokens'], m.get('unknown_tokens') or 0)} "
                f"over {m['responses']} responses.")
    if summary.get("phases"):
        lines += ["", "Usage by work phase (explicit ownership; mixed or missing phases stay visible):",
                  "| Phase | Model / harness | Responses | Input | Cache read | Cache write | Output (native) | Reasoning | Total (native) | Priced subtotal |",
                  "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for phase in summary["phases"]:
            costs = {(m['harness'], m['model'], m['effort'], m['semantics']): m
                     for m in phase['estimated_cost']['by_model']}
            for m in phase["models"]:
                c = costs.get((m['harness'],m['model'],m['effort'],m['semantics']), {})
                unknown = m.get('unknown_counts') or {}
                buckets = " | ".join(_fmt_tokens(m.get(k), unknown.get(k, 0)) for k in report.BUCKETS)
                cost = _fmt_cost(c.get('estimated_cost_usd_partial') if c.get('priced_responses') else None)
                lines.append(f"| {_safe_field(phase['phase'])} | {_safe_field(m['model'])} / {_safe_field(m['harness'])} "
                             f"({_safe_field(m['effort'])}) | {m['responses']} | {buckets} | {cost} |")
        lines.append("Native semantics match the model rows above. Inclusive output already contains reasoning; do not add it again. Phase costs cover attributed responses only.")
        lines += ["", "Recorded activity by phase (observations, not separate token bills):",
                  "| Phase | Tool calls | MCP results | Reads | File changes | Failed tool results |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for phase in summary['phases']:
            a = phase['activity']
            counts = " | ".join(str(a.get(k, 0)) for k in
                ('tool_calls','mcp_results','reads','file_changes','failed_tool_results'))
            lines.append(f"| {_safe_field(phase['phase'])} | {counts} |")
        lines.append("Tool/MCP counters overlap and coverage varies by harness. Their input/output token cost cannot be separated from reused model context without stronger native attribution.")
        context = summary.get('activity_session_context') or {}
        if any(context.values()):
            lines.append("Session-context activity without task/phase ownership (may include unrelated work): "
                         + ", ".join(f"{k.replace('_', ' ')} {v}" for k,v in context.items()) + ".")
    if "total_tokens" in u:
        lines += ["", f"Total tokens {_fmt_tokens(u['total_tokens'], (u.get('unknown_counts') or {}).get('total_tokens', 0))} over {u['responses']} responses "
                  f"(each harness's own total; cache and reasoning buckets are not added across "
                  f"harnesses)."]
    else:
        # Mixed counter semantics: no top-level total exists and none is
        # invented; each semantics keeps its own total.
        parts = [f"{_safe_field(sem)} {_fmt_tokens(b.get('total_tokens'), (b.get('unknown_counts') or {}).get('total_tokens', 0))}"
                 for sem, b in sorted((u.get("by_semantics") or {}).items())]
        lines += ["", f"Tokens by counter semantics over {u['responses']} responses "
                  f"(never added across semantics): {'; '.join(parts) or 'unknown'}."]
    if summary.get("shared_models"):
        lines += ["", "Shared model rows (joint usage, kept whole and never "
                  "divided into the attributed headline):"]
        for m in summary["shared_models"]:
            lines.append(
                f"- {_safe_field(m['harness'])}/{_safe_field(m['model'] or 'unknown')}"
                f" ({_safe_field(m['effort'] or 'unknown')}, "
                f"{_safe_field(m.get('semantics') or 'unknown')}): "
                f"{_fmt_bucket_row(m)}; harness total "
                f"{_fmt_tokens(m['tokens'], m.get('unknown_tokens') or 0)} "
                f"over {m['responses']} responses.")
    if summary.get("shared_by_semantics"):
        parts = [f"{_safe_field(sem)} {_fmt_tokens(b.get('total_tokens'), (b.get('unknown_counts') or {}).get('total_tokens', 0))}"
                 for sem, b in sorted(summary["shared_by_semantics"].items())]
        lines.append("Shared with other tasks and not divided"
                     f" (by semantics, never added): {'; '.join(parts) or 'unknown'}.")
    elif summary["shared_tokens"] or summary.get("shared_tokens_unknown"):
        lines.append(f"Shared with other tasks and not divided: {_fmt_tokens(summary['shared_tokens'], summary.get('shared_tokens_unknown') or 0)} tokens.")
    unassigned = summary.get("unassigned_in_scope")
    if isinstance(unassigned, dict) and unassigned.get("responses"):
        lines.append(
            f"Unassigned in scope (never task cost): "
            f"{unassigned.get('responses')} responses.")
    if summary.get("scope_kind") == "task":
        est = summary.get("estimated_cost") or {}
        if est.get("schedule_source"):
            lines += ["", f"Estimated list-price cost ({_safe_field(est.get('schedule_source'))} "
                      f"as of {_safe_field(est.get('schedule_as_of'))}): "
                      f"partial {_fmt_cost(est.get('estimated_cost_usd_partial'))} "
                      f"over {est.get('priced_responses', 0)}/{est.get('responses', 0)} priced responses."]
            if est.get("estimated_cost_usd_total") is not None:
                lines.append(f"Complete total {_fmt_cost(est.get('estimated_cost_usd_total'))} "
                             f"(all {est.get('responses', 0)} responses priced).")
            else:
                reasons = ", ".join(
                    f"{_safe_field(k)} {v}" for k, v in
                    sorted((est.get("unpriced_reasons") or {}).items())) or "unpriced usage"
                lines.append(f"No complete total: unpriced responses remain ({reasons}); "
                             f"unknown stays unknown, never zero.")
            lines.append(_safe_field(est.get("basis") or "Standard API list-price equivalent, not subscription spend."))
            lines += ["", "| Model / harness | Effort | Priced responses | Known subtotal | Complete estimate | Source |",
                      "| --- | --- | ---: | ---: | ---: | --- |"]
            for model in est.get("by_model", []):
                lines.append(
                    f"| {_safe_field(model.get('model'))} / {_safe_field(model.get('harness'))} "
                    f"| {_safe_field(model.get('effort'))} "
                    f"| {model['priced_responses']}/{model['responses']} "
                    f"| {_fmt_cost(model['estimated_cost_usd_partial'] if model['priced_responses'] else None)} "
                    f"| {_fmt_cost(model['estimated_cost_usd'])} "
                    f"| {_safe_field(model.get('source_url'))} |")
            for basis in sorted({m['basis'] for m in est.get('by_model', []) if m.get('basis')}):
                lines.append(_safe_field(basis))
            lines += _price_evidence_lines(summary)
        else:
            lines += ["", "Estimated list-price cost: unknown "
                      "(no sourced price schedule supplied; unpriced models stay unknown)."]
        if est.get("coverage_note"):
            lines.append(_safe_field(est["coverage_note"]))
        native = summary.get("native_cost") or {}
        if native.get("total_usd") is not None:
            lines.append(f"Native harness-reported cost: {_fmt_cost(native.get('total_usd'))} "
                         f"({native.get('known_responses', 0)} known, "
                         f"{native.get('unknown_responses', 0)} unknown responses; "
                         f"separate from the list-price estimate).")
        else:
            lines.append("Native harness-reported cost: complete total unknown; "
                         f"known subtotal {_fmt_cost(native.get('known_subtotal_usd') if native.get('known_responses') else None)} "
                         f"over {native.get('known_responses', 0)} reported responses "
                         "(separate from the list-price estimate).")
        lines.append("_Usage totals are not billing. Subscription spending is "
                     "separate and is never posted as spend._")
        lines += ["", f"Snapshot `{_safe_field(summary.get('snapshot_id'))}` at source cutoff "
                  f"{_fmt(summary.get('source_cutoff'))}; measured "
                  f"{len(summary.get('scope_sessions') or [])} session(s), "
                  f"{u.get('responses', 0)} attributed + "
                  f"{(summary.get('shared_models') and sum(m['responses'] for m in summary['shared_models'])) or 0} shared + "
                  f"{(unassigned or {}).get('responses', 0)} unassigned responses."]
        acceptance = summary.get("acceptance_state") or "unknown"
        lines.append(f"Acceptance: {_safe_field(acceptance)} "
                     f"(finished processes never imply acceptance).")
        outcome = summary.get("outcome") or {}
        for key, title in (("candidate", "Candidate"), ("proof_ref", "Proof"),
                           ("repairs", "Repairs"), ("corrections", "Corrections")):
            ref = _safe_ref(outcome.get(key))
            if ref is not None:
                lines.append(f"{title}: {ref}")
            elif outcome.get(key):
                lines.append(f"{title}: [withheld: free-form reference not published]")
        if summary.get("missing_assignments") or summary.get("conflicting_assignments"):
            lines.append(
                "Coverage: missing "
                f"{len(summary.get('missing_assignments') or [])}, conflicting "
                f"{len(summary.get('conflicting_assignments') or [])}; "
                f"reconciles {summary.get('reconciles')}, complete {summary.get('complete')}.")
        else:
            lines.append(
                f"Coverage: reconciles {summary.get('reconciles')}, "
                f"complete {summary.get('complete')}; "
                f"crashes counted separately {summary.get('crashes_counted_separately')}.")
        if summary.get("has_active_work"):
            lines.append("Active work remains visible; arithmetic reconciliation "
                         "alone is not completion.")
        timing = summary.get("timing") or {}
        if timing:
            lines.append(
                f"Completion: {_safe_field(timing.get('completion_elapsed_s'))} s "
                f"({_safe_field(timing.get('completion_label'))}); "
                f"partial execution span {_safe_field(timing.get('partial_execution_span_s'))} s "
                f"({_safe_field(timing.get('partial_execution_span_source'))}).")
            if timing.get("missing"):
                lines.append(
                    f"Timing missing: {_safe_field(', '.join(timing['missing']))}.")
        attempt_time = summary.get("attempt_timing") or {}
        if attempt_time:
            lines.append(
                f"Executions reconciled: {_safe_field(attempt_time.get('reconciled_execution_count'))} "
                f"from {_safe_field(attempt_time.get('raw_attempt_count'))} attempts "
                f"({_safe_field(attempt_time.get('duplicate_router_native_groups'))} router/native duplicates; "
                "parallel durations never become task elapsed).")
            for sess in attempt_time.get("per_session", []):
                lines.append(
                    f"Session {_safe_field(sess.get('session_key') or 'unknown')}: "
                    f"{_safe_field(sess.get('attempts'))} attempts, union span "
                    f"{_safe_field(sess.get('union_span_s'))} s "
                    "(merged covered duration of explicit attempt windows; gaps excluded).")
        failures = summary.get("failures") or {}
        if failures:
            by_class = ", ".join(
                f"{_safe_field(k)} {v}" for k, v in sorted((failures.get("by_class") or {}).items())) or "none"
            lines.append(
                f"Failures: {_safe_field(failures.get('failed_attempts'))}/"
                f"{_safe_field(failures.get('production_attempts'))} failed/production attempts "
                f"(by class: {by_class}; {_safe_field(failures.get('denominator_note'))}).")
            for rec in summary.get("recovery", []):
                lines.append(
                    f"Recovery {_safe_field(rec.get('failed_turn'))} -> "
                    f"{_safe_field(rec.get('next_attempt_turn') or 'none')}: "
                    f"{_safe_field(rec.get('failure_to_next_start_s'))} s to next start; "
                    f"{_safe_field(rec.get('recovery_outcome'))}.")
        coverage_u = summary.get("usage_coverage") or {}
        if coverage_u:
            lines.append(
                f"Usage source coverage: router {_safe_field(coverage_u.get('router_with_usage'))}/"
                f"{_safe_field(coverage_u.get('router_attempts'))} with usage; "
                f"null with reconciled native {_safe_field(coverage_u.get('null_with_reconciled_native'))}; "
                f"{_safe_field(coverage_u.get('note'))}.")
        gaps = summary.get("coverage") or {}
        for key, label in (("missing_sessions", "Missing native sessions"),
                           ("sessions_without_usage", "Sessions without native usage"),
                           ("unbound_worker_sessions", "Unbound worker sessions"),
                           ("unbound_dispatches", "Dispatches without worker ownership"),
                           ("conflicting_sessions", "Conflicting session ownership")):
            if gaps.get(key):
                lines.append(f"{label}: {len(gaps[key])}; task consumption remains incomplete.")
    if summary["incidents"]:
        lines += ["", "Diagnostics, task turns only (candidates, not verdicts): " + ", ".join(
            f"{k.replace('_', ' ')} {v}" for k, v in sorted(summary["incidents"].items()))]
    if summary.get("session_context_incidents"):
        lines.append("Diagnostics, session context (not task-only): " + ", ".join(
            f"{k.replace('_', ' ')} {v}" for k, v in
            sorted(summary["session_context_incidents"].items())))
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
