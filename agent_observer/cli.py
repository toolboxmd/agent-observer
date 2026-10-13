"""CLI: agent-observer sync|sessions|capture|task|claims|thread-misuse|delegation-modes|trace|event|interventions|intervention|verdict|recurrence|session-attr. Stdlib only."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys

from . import CAPTURE_CONTRACT_VERSION, EVENT_CONTRACT_VERSION, SCHEMA_VERSION
from . import adapters as _adapters
from . import analysis as _analysis
from . import db as _db
from . import identity as _identity
from . import interventions as _interventions
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


def _fmt_tokens(value, unknown: int = 0) -> str:
    """Human rendering of a counter that may be unknown.

    Fully unknown renders as unknown; a partial sum renders as an
    explicitly labeled lower bound, never as an exact figure.
    """
    if value is None:
        return "unknown"
    text = f"{int(value):,}"
    if unknown:
        return f">={text} (lower bound)"
    return text


def _bucket_unknown(bucket: dict, field: str = "total_tokens") -> int:
    return (bucket.get("unknown_counts") or {}).get(field, 0)


def _fmt_section_total(section: dict) -> str:
    """A task section total that stays honest when semantics are mixed.

    Sections over mixed counter semantics carry no top-level raw total;
    the text view names that instead of inventing a sum.
    """
    if "total_tokens" in section:
        return _fmt_tokens(section.get("total_tokens"),
                           _bucket_unknown(section))
    if "by_semantics" in section:
        return "mixed semantics (see --json)"
    return "unknown"


def _fmt_session_total(session: dict) -> str:
    """One session row total that never mixes counter semantics.

    Sessions spanning more than one semantics expose no combined total;
    the text view names that instead of inventing a sum. Homogeneous
    sessions keep the historical total rendering.
    """
    if "total_tokens" in session:
        return _fmt_tokens(session.get("total_tokens"),
                            session.get("unknown_tokens") or 0)
    if "by_semantics" in session:
        return "mixed semantics (see --json)"
    return "unknown"


def _text(payload) -> str:
    if isinstance(payload, dict) and payload.get("_view") == "sync":
        lines = []
        for h in payload["harnesses"]:
            line = (f"{h['harness']}: {h.get('sources', 0)} sources "
                    f"({h.get('unchanged', 0)} unchanged), "
                    f"{h.get('responses_inserted', 0)} new responses, "
                    f"{h.get('events_inserted', 0)} new events, "
                    f"{h.get('user_messages_inserted', 0)} new user messages, "
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
                f"responses={r.get('responses', 0)} "
                f"total={_fmt_session_total(r)}")
        return "\n".join(lines) or "no sessions"
    if isinstance(payload, dict) and payload.get("_view") == "task":
        r = payload
        lines = [
            f"task {r['task']['task_id']}: {r['task'].get('title') or ''}",
            f"  estimated cost: {(r.get('total_cost') or {}).get('text') or 'unknown'}"
            + (f" (includes work shared with {len(r['total_cost']['shared_with_tasks'])}"
               f" other task{'' if len(r['total_cost']['shared_with_tasks']) == 1 else 's'},"
               " counted whole; sums across tasks overlap)"
               if (r.get('total_cost') or {}).get('shared_responses') else ""),
            f"  attributed responses: {r['attributed']['responses']} "
            f"total={_fmt_section_total(r['attributed'])}",
            f"  shared joint responses: {r['shared_joint']['responses']} "
            f"total={_fmt_section_total(r['shared_joint'])}",
            f"  unassigned in scope: {r['unassigned_in_scope']['responses']} "
            f"total={_fmt_section_total(r['unassigned_in_scope'])}",
            f"  reconciles against scope: {r['reconciles']}",
            f"  missing assignments: {r['missing_assignments'] or 'none'}",
            f"  unbound usage: {r.get('unbound_usage') or 'none'}",
            f"  conflicting: {r['conflicting_assignments'] or 'none'}",
            f"  crashes counted separately: {r['crashes_counted_separately']}",
            f"  acceptance: {r.get('acceptance_state') or 'unknown'} "
            f"(finished processes never imply acceptance)",
            f"  snapshot: {r.get('snapshot_id') or 'unknown'} "
            f"cutoff={r.get('source_cutoff')}",
            f"  task time: {r.get('time', {}).get('task_elapsed_s')} "
            f"({r.get('time', {}).get('task_elapsed_source')}); "
            f"session span {r.get('time', {}).get('session_span_s')} "
            f"({r.get('time', {}).get('session_span_label')})",
        ]
        est = r.get("estimated_cost") or {}
        if est.get("schedule_source"):
            lines.append(
                f"  estimated cost partial: {est.get('estimated_cost_usd_partial')} "
                f"over {est.get('priced_responses')}/{est.get('responses')} priced; "
                f"total={est.get('estimated_cost_usd_total')}")
        else:
            lines.append("  estimated cost: unknown (no sourced schedule)")
        for model in r.get("models", []):
            buckets = ", ".join(f"{key}={model.get(key)}" for key in _report.BUCKETS)
            lines.append(f"  {model['harness']}/{model.get('model') or 'unknown'} "
                         f"effort={model.get('effort') or 'unknown'} "
                         f"[{model['semantics']}]: {buckets}")
        for phase in r.get("phases", []):
            lines.append(f"  phase {phase['phase']}: responses={phase['usage']['responses']} "
                         f"total={_fmt_section_total(phase['usage'])}; "
                         f"activity={phase['activity']}")
            for model in phase['models']:
                g=model['generation']
                lines.append(f"    {model['harness']}/{model['model']}: "
                             f"reasoning={g['reasoning_tokens']}, other_output={g['other_output_tokens']}, "
                             f"reasoning_share={g['reasoning_share']}, unknown_splits={g['unknown_responses']}")
        for model in est.get("by_model", []):
            lines.append(f"  price {model.get('model') or 'unknown'} "
                         f"effort={model.get('effort') or 'unknown'}: "
                         f"known subtotal USD {model['estimated_cost_usd_partial']}; "
                         f"complete estimate={model['estimated_cost_usd']}; "
                         f"priced {model['priced_responses']}/{model['responses']}")
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
    # The global flag owns the default: every subparser-level --json uses
    # SUPPRESS so it never overrides a global --json with its own default.
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
    s.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)

    ss = sub.add_parser("sessions", help="list or show imported sessions")
    ssub = ss.add_subparsers(dest="op", required=True)
    sl = ssub.add_parser("list")
    sl.add_argument("--harness", default=None)
    sl.add_argument("--project", default=None)
    sl.add_argument("--since", default=None, help="ISO date or epoch seconds")
    sl.add_argument("--limit", type=int, default=50)
    sl.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    sw = ssub.add_parser("show")
    sw.add_argument("--session", required=True)
    sw.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)

    c = sub.add_parser("capture", help="record workload facts")
    c.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    csub = c.add_subparsers(dest="op", required=True)
    t = csub.add_parser("create-task")
    t.add_argument("--task", required=True)
    t.add_argument("--project", default=None)
    t.add_argument("--family", default=None)
    t.add_argument("--title", default=None)
    t.add_argument("--issue", default=None)
    t.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    a = csub.add_parser("assign")
    a.add_argument("--user-message", "--submission", dest="user_message",
                   required=True)
    a.add_argument("--task", required=True)
    a.add_argument("--task-turn", "--attempt", dest="task_turn", default=None)
    a.add_argument("--phase", default=None)
    a.add_argument("--evidence", default=None)
    a.add_argument("--shared", action="store_true")
    a.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    whole = csub.add_parser("assign-session", help="bind a provably dedicated session")
    whole.add_argument("--task", required=True)
    whole.add_argument("--session", required=True)
    whole.add_argument("--exclusive", action="store_true", required=True)
    whole.add_argument("--evidence", required=True)
    whole.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    d = csub.add_parser("delegation", aliases=["dispatch"],
                        help="record a parent delegating to a child thread")
    d.add_argument("--user-message", "--submission", dest="user_message",
                   required=True)
    d.add_argument("--child-thread", "--worker", dest="child_thread",
                   required=True)
    d.add_argument("--parent-turn", default=None)
    d.add_argument("--child-turn", "--worker-turn", dest="child_turn",
                   default=None)
    d.add_argument("--requested-model", default=None)
    d.add_argument("--requested-effort", default=None)
    d.add_argument("--policy", default=None)
    d.add_argument("--reason", default=None)
    d.add_argument("--task-name", default=None)
    d.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    at = csub.add_parser("task-turn", aliases=["attempt"],
                         help="record one observed turn toward a task")
    at.add_argument("--task", required=True)
    at.add_argument("--turn", required=True)
    # `worker` is the schema-2 name for `child`, accepted for one release.
    at.add_argument("--role", required=True,
                    choices=["parent", "child", "worker"])
    at.add_argument("--model", default=None)
    at.add_argument("--effort", default=None)
    at.add_argument("--phase", choices=list(_report.PHASES)+["mixed"], default=None)
    at.add_argument("--state", default="active",
                    choices=["complete", "active", "cancelled", "failed",
                             "quota_blocked", "crashed"])
    at.add_argument("--usable", action="store_true", default=None)
    at.add_argument("--not-usable", action="store_true")
    at.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    o = csub.add_parser("outcome")
    o.add_argument("--task", required=True)
    o.add_argument("--state", required=True,
                   choices=["complete", "active", "cancelled", "failed",
                            "quota_blocked", "crashed", "unknown"])
    o.add_argument("--candidate", default=None)
    o.add_argument("--proof", default=None)
    o.add_argument("--repairs", default=None)
    o.add_argument("--corrections", default=None)
    o.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)

    k = sub.add_parser("task", help="inspect tasks and usage")
    ksub = k.add_subparsers(dest="op", required=True)
    ksub.add_parser("list").add_argument("--json", action="store_true",
                                              dest="as_json",
                                              default=argparse.SUPPRESS)
    sh = ksub.add_parser("show")
    sh.add_argument("--task", required=True)
    sh.add_argument("--prices", default=None,
                    help="offline JSON price schedule for list-price estimates")
    sh.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)

    def _scope(parser):
        parser.add_argument("--since", default=None, help="ISO date or epoch seconds")
        parser.add_argument("--until", default=None, help="ISO date or epoch seconds")
        parser.add_argument("--project", default=None)
        parser.add_argument("--harness", default=None)
        parser.add_argument("--agentsmd-version", default=None)
        parser.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)

    dg = sub.add_parser("diagnose", help="find repeated work and behavior incidents")
    dg.add_argument("--detector", action="append", default=None,
                    choices=list(_analysis.DETECTORS))
    dg.add_argument("--session", default=None)
    dg.add_argument("--limit", type=int, default=50)
    _scope(dg)

    cp = sub.add_parser("compare", help="compare behavior across groups")
    cp.add_argument("--by", default="agentsmd",
                    choices=["agentsmd", "model", "harness", "project"])
    _scope(cp)

    pb = sub.add_parser("publish", help="render or post one summary comment on GitHub")
    target = pb.add_mutually_exclusive_group(required=True)
    target.add_argument("--task", default=None)
    target.add_argument("--session", action="append", default=None)
    pb.add_argument("--repo", default=None, help="owner/name")
    pb.add_argument("--pr", type=int, default=None)
    pb.add_argument("--commit", default=None)
    pb.add_argument("--prices", default=None,
                    help="offline JSON price schedule for list-price estimates")
    pb.add_argument("--dry-run", action="store_true", help="print the comment, post nothing")
    pb.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)

    hl = sub.add_parser("health", help="find live sessions Observer cannot see")
    hl.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)

    cl = sub.add_parser("claims", help="check a Claude Code session's final report against its tool calls")
    cl.add_argument("--session", required=True, help="session key, e.g. claude:<id>")
    cl.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)

    tm = sub.add_parser("thread-misuse",
                        help="list deep Chromeria spawn chains and cross-thread messages")
    tm.add_argument("--since", default=None, help="ISO date or epoch seconds")
    tm.add_argument("--until", default=None, help="ISO date or epoch seconds")
    tm.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)

    dm = sub.add_parser("delegation-modes", aliases=["dispatch-modes"],
                        help="compare planner delegation with a dispatcher thread per job")
    dm.add_argument("--planner", action="append", default=None,
                    help="planner T3 thread id (repeatable; default every planner)")
    dm.add_argument("--job", action="append", default=None,
                    help="owner/repo#N PR or Issue the job delivers (repeatable)")
    dm.add_argument("--since", default=None, help="job start, ISO date or epoch seconds")
    dm.add_argument("--until", default=None, help="job start, ISO date or epoch seconds")
    dm.add_argument("--t3-state", default=None, help="T3 state database (default ~/.t3 chromeria-v2.sqlite, else state.sqlite)")
    dm.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)

    ev = sub.add_parser("event", help="show the full content behind ledger events")
    evsub = ev.add_subparsers(dest="op", required=True)
    es = evsub.add_parser("show", help="read tool input, result and assistant text from the native log")
    es.add_argument("ids", nargs="*", type=int, help="event ids (the id column of trace --json)")
    es.add_argument("--ids-from", default=None, metavar="FILE",
                    help="read event ids, one per line, from FILE or - for stdin")
    es.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    esr = evsub.add_parser("search", help="find events and user messages by words, read-only")
    esr.add_argument("words", nargs="+", help="every word must appear, case-insensitively")
    esr.add_argument("--since", default=None, help="ISO date or epoch seconds")
    esr.add_argument("--until", default=None, help="ISO date or epoch seconds")
    esr.add_argument("--harness", default=None)
    esr.add_argument("--project", default=None, help="project directory contains this")
    esr.add_argument("--limit", type=int, default=50)
    esr.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    def _intervention_scope(parser):
        parser.add_argument("--since", default=None, help="ISO date or epoch seconds")
        parser.add_argument("--until", default=None, help="ISO date or epoch seconds")
        parser.add_argument("--harness", default=None)
        parser.add_argument("--project", default=None)
        parser.add_argument("--attr", action="append", default=None,
                            metavar="NAME=VALUE",
                            help="only sessions with this attribute (repeatable)")
        parser.add_argument("--json", action="store_true", dest="as_json",
                            default=argparse.SUPPRESS)

    mo = sub.add_parser("interventions", aliases=["moments"],
                        help="list interventions (user messages, interrupts, rejections) for a judge")
    _intervention_scope(mo)
    mo.add_argument("--created-by", "--origin", dest="created_by",
                    action="append", default=None,
                    choices=list(_interventions.CREATED_BY) + ["all", "human"],
                    help="who acted (repeatable; default user and agent;"
                         " --origin and human are the old names)")
    mo.add_argument("--kind", action="append", default=None,
                    choices=list(_interventions.KINDS))
    mo.add_argument("--unjudged", default=None, metavar="BEHAVIOR",
                    help="only interventions without a verdict for BEHAVIOR")
    mo.add_argument("--judge", default=None,
                    help="with --unjudged: only verdicts by this judge count")
    mo.add_argument("--limit", type=int, default=None)
    mm = sub.add_parser("intervention", aliases=["moment"],
                        help="read interventions in context from the native logs")
    mmsub = mm.add_subparsers(dest="op", required=True)
    ms = mmsub.add_parser("show", help="full text, agent activity before and after, nearby prompts")
    ms.add_argument("ids", nargs="*", help="intervention ids from interventions --json")
    ms.add_argument("--ids-from", default=None, metavar="FILE",
                    help="read intervention ids, one per line, from FILE or - for stdin")
    ms.add_argument("--turns-before", type=_nonnegative, default=_interventions.DEFAULT_TURNS_BEFORE,
                    help="whole turns before the intervention (0 = the intervention alone)")
    ms.add_argument("--turns-after", type=_nonnegative, default=_interventions.DEFAULT_TURNS_AFTER,
                    help="whole turns after the intervention, the first being its own")
    ms.add_argument("--items", type=_nonnegative, default=_interventions.DEFAULT_ITEMS,
                    help="at most this many agent messages and tool calls per turn")
    ms.add_argument("--chars", type=_nonnegative, default=_interventions.DEFAULT_CHARS,
                    help="cut each agent message, tool input and output to about this many characters")
    ms.add_argument("--text-chars", type=_nonnegative, default=_interventions.DEFAULT_TEXT_CHARS,
                    help="cut the person's own text (intervention, previous and next prompt) to about this many characters")
    ms.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    vd = sub.add_parser("verdict", help="store and list judge verdicts on interventions")
    vdsub = vd.add_subparsers(dest="op", required=True)
    va = vdsub.add_parser("add", help="store JSON-line verdicts; the batch is all or nothing")
    va.add_argument("--file", default="-", help="JSON lines file, or - for stdin")
    va.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    vl = vdsub.add_parser("list", help="list stored verdicts with their interventions")
    vl.add_argument("--behavior", required=True)
    vl.add_argument("--judge", default=None)
    vl.add_argument("--verdict", default=None, choices=["yes", "no", "unclear"])
    vl.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    vg = vdsub.add_parser("groups", help="list existing groups and their problem lines")
    vg.add_argument("--behavior", required=True)
    vg.add_argument("--judge", default=None)
    vg.add_argument("--created-by", "--origin", dest="created_by",
                    action="append", default=None,
                    choices=list(_interventions.CREATED_BY) + ["all", "human"],
                    help="who acted (repeatable; default user and agent;"
                         " --origin and human are the old names)")
    vg.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    rc = sub.add_parser("recurrence", help="report problems that come back, from judge verdicts")
    rc.add_argument("--behavior", required=True)
    rc.add_argument("--judge", default=None)
    rc.add_argument("--created-by", "--origin", dest="created_by",
                    action="append", default=None,
                    choices=list(_interventions.CREATED_BY) + ["all", "human"],
                    help="who acted (repeatable; default user and agent;"
                         " --origin and human are the old names)")
    _intervention_scope(rc)
    sa = sub.add_parser("session-attr", help="store session attributes used as report filters")
    sasub = sa.add_subparsers(dest="op", required=True)
    saa = sasub.add_parser("add", help="store JSON lines {session, name, value}")
    saa.add_argument("--file", default="-", help="JSON lines file, or - for stdin")
    saa.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    ex = sub.add_parser("export", help="print this machine's aggregate fleet export (JSON)")
    ex.add_argument("--machine", default=None,
                    help="machine name (default $AGENT_OBSERVER_MACHINE or the short hostname)")
    fl = sub.add_parser("fleet", help="combine aggregate evidence across machines")
    flsub = fl.add_subparsers(dest="op", required=True)
    fp = flsub.add_parser("pull", help="fetch other machines' exports over SSH")
    fp.add_argument("hosts", nargs="+", help="SSH host names")
    fp.add_argument("--remote-command", default=None,
                    help="command printing the remote export (default: agent-observer export)")
    fp.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    fr = flsub.add_parser("report", help="combine this machine's export with pulled ones")
    fr.add_argument("--expect", default=None,
                    help="comma-separated machines that must be present")
    fr.add_argument("--task", default=None, help="only this task")
    fr.add_argument("--machine", default=None, help="this machine's name")
    fr.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    tr = sub.add_parser("trace", help="inspect the execution timeline")
    tr.add_argument("--task", default=None)
    tr.add_argument("--turn", default=None)
    tr.add_argument("--session", default=None)
    tr.add_argument("--family", default=None)
    tr.add_argument("--limit", type=int, default=200)
    tr.add_argument("--capabilities", action="store_true")
    tr.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    return ap


# Only these commands write the ledger; every other command opens it
# read-only, so lookups never take the write lock or create a ledger (#73).
WRITERS = ("sync", "capture", "session-attr")


def _writes(ns) -> bool:
    return ns.cmd in WRITERS or (ns.cmd, getattr(ns, "op", None)) == (
        "verdict", "add")


# Schema-2 command names, accepted for one release (#65).
CMD_ALIASES = {"moments": "interventions", "moment": "intervention",
               "dispatch-modes": "delegation-modes"}
OP_ALIASES = {"dispatch": "delegation", "attempt": "task-turn"}


def main(argv=None) -> int:
    ap = build_parser()
    ns = ap.parse_args(argv)
    ns.cmd = CMD_ALIASES.get(ns.cmd, ns.cmd)
    if getattr(ns, "op", None) in OP_ALIASES:
        ns.op = OP_ALIASES[ns.op]
    if ns.cmd == "fleet" and ns.op == "pull":
        return _fleet_pull(ns)
    if ns.cmd == "health":
        return _health_check(ns)
    if ns.cmd == "trace" and ns.capabilities:
        return _capabilities(ns)
    read_only = not _writes(ns)
    try:
        con = _db.connect_read_only(ns.db) if read_only else _con(ns.db)
    except (RuntimeError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        if read_only:
            print("Reports and lookups require an existing ledger; run agent-observer sync first.", file=sys.stderr)
            if os.path.exists(ns.db):
                print("If a read-only sandbox prevents access, consume a coordinator-exported task JSON instead.", file=sys.stderr)
        return 2
    try:
        if ns.cmd == "sync":
            return _sync(con, ns)

        if ns.cmd == "sessions":
            return _sessions(con, ns)

        if ns.cmd in ("diagnose", "compare"):
            return _analyze(con, ns)

        if ns.cmd == "publish":
            return _publish(con, ns)

        if ns.cmd in ("interventions", "intervention", "verdict", "recurrence",
                      "session-attr"):
            return _judge(con, ns)

        if ns.cmd == "capture":
            return _capture(con, ns)

        if ns.cmd == "thread-misuse":
            from . import misuse as _misuse
            result = _misuse.report(con, since=_when(ns.since),
                                    until=_when(ns.until))
            if ns.as_json:
                _emit(result, True)
            else:
                print(_misuse.render(result))
            return 0 if result["imported"] else 2

        if ns.cmd == "claims":
            from . import claims as _claims
            try:
                path = _claims.transcript_path(con, ns.session)
                result = _claims.audit(_claims.read_transcript(path))
            except _claims.AuditError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            result["session"] = ns.session
            if ns.as_json:
                _emit(result, True)
            else:
                print(_claims.render(result))
            return 0

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
                schedule = None
                if getattr(ns, "prices", None):
                    from . import pricing as _pricing
                    try:
                        schedule = _pricing.load_schedule(ns.prices)
                    except (OSError, ValueError) as exc:
                        print(f"invalid price schedule: {exc}", file=sys.stderr)
                        return 2
                rep = _report.task_report(con, ns.task, schedule=schedule)
            except KeyError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            rep["_view"] = "task"
            _emit(rep, ns.as_json)
            # Missing, conflicting or wholly unbound ownership is visible.
            if rep["missing_assignments"] or rep["conflicting_assignments"] \
                    or rep.get("unbound_usage"):
                return 3
            return 0

        if ns.cmd == "delegation-modes":
            from . import dispatch as _dispatch
            from .adapters import t3 as _t3
            path = _t3.state_path(source=ns.t3_state)
            if not os.path.exists(path):
                print(f"no T3 state at {path}", file=sys.stderr)
                return 2
            payload = _dispatch.compare(
                con, path, planners=ns.planner, jobs=ns.job,
                since=_when(ns.since), until=_when(ns.until))
            if ns.as_json:
                _emit(payload, True)
            else:
                print(_dispatch.render(payload))
            return 0

        if ns.cmd == "event" and ns.op == "search":
            return _event_search(con, ns)
        if ns.cmd == "event":
            return _event_show(con, ns)

        if ns.cmd == "export":
            from . import fleet as _fleet
            try:
                doc = _fleet.export(con, machine=ns.machine)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(json.dumps(doc, sort_keys=True))
            return 0

        if ns.cmd == "fleet":
            return _fleet_report(con, ns)

        if ns.cmd == "trace":
            tl = _report.timeline(con, task_id=ns.task, turn_id=ns.turn,
                                 family=ns.family, limit=ns.limit,
                                 session_key=ns.session)
            tl["_view"] = "trace"
            _emit(tl, ns.as_json)
            return 0
    except sqlite3.OperationalError as exc:
        if ns.cmd not in ("task", "publish"):
            raise
        print(f"Cannot read ledger: {exc}. Run agent-observer sync with the current version first.", file=sys.stderr)
        return 2
    finally:
        con.close()
    return 2


def _health_check(ns) -> int:
    from . import health as _health
    result = _health.check()
    if ns.as_json:
        _emit(result, True)
    else:
        print(f"checked {result['checked']['claude_sessions']} Claude sessions and "
              f"{result['checked']['processes']} agent processes: "
              f"{len(result['findings'])} invisible")
        for f in result["findings"]:
            print(f"- {f['host']} pid {f['pid']} in {f['cwd']}: {f['problem']}. Fix: {f['fix']}")
    return 4 if result["findings"] else 0


def _capabilities(ns) -> int:
    caps = {"capabilities": _report.capabilities(),
            "contract": {"event": EVENT_CONTRACT_VERSION}}
    if ns.as_json:
        print(json.dumps(caps, indent=2, sort_keys=True))
    else:
        for c in caps["capabilities"]:
            mark = "yes" if c["supported"] else "no "
            print(f"{mark} {c['family']}: {c['detail']}")
    return 0


def _fleet_pull(ns) -> int:
    from . import fleet as _fleet
    directory = _fleet.store_dir(ns.db)
    results, failed = [], 0
    for host in ns.hosts:
        try:
            results.append(_fleet.pull(
                host, directory, ns.remote_command or _fleet.REMOTE_COMMAND))
        except (_fleet.PullError, OSError, subprocess.SubprocessError) as exc:
            failed += 1
            results.append({"host": host, "error": str(exc)})
    if ns.as_json:
        print(json.dumps({"_view": "fleet-pull", "pulled": results}, indent=2))
    else:
        for r in results:
            print(f"{r['host']}: failed: {r['error']}" if "error" in r else
                  f"{r['host']}: stored {r['machine']} ({r['sessions']}"
                  f" sessions, {r['tasks']} tasks, exported {r['exported_at']})"
                  + (f"; warning: {r['warning']}" if r.get("warning") else ""))
    return 1 if failed else 0


def _fleet_report(con, ns) -> int:
    from . import fleet as _fleet
    try:
        local = _fleet.export(con, machine=ns.machine)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    stored = _fleet.stored(_fleet.store_dir(ns.db))
    docs = [local] + [d for d in stored if d["machine"] != local["machine"]]
    expect = [m.strip() for m in (ns.expect or "").split(",") if m.strip()]
    result = _fleet.combine(docs, expect=expect)
    # A stored export under this machine's own name is never combined;
    # say so instead of dropping it silently.
    result["skipped_stored"] = [d["machine"] for d in stored
                                if d["machine"] == local["machine"]]
    if ns.task:
        result["tasks"] = [t for t in result["tasks"] if t["task_id"] == ns.task]
    if ns.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(_fleet.render(result))
    # Missing machines and doubly reported sessions are visible failures.
    return 3 if result["missing"] or result["duplicate_sessions"] else 0


def _event_search(con, ns) -> int:
    from . import fleet as _fleet
    from . import search as _search
    try:
        result = _search.search(
            con, ns.words, since=_when(ns.since), until=_when(ns.until),
            harness=ns.harness, project=ns.project, limit=ns.limit,
            machine=_fleet.machine_name())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if ns.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(_search.render(result))
    return 0


def _event_show(con, ns) -> int:
    from . import lookup as _lookup
    ids = list(ns.ids)
    if ns.ids_from:
        try:
            stream = sys.stdin if ns.ids_from == "-" else open(ns.ids_from, encoding="utf-8")
        except OSError as exc:
            print(f"cannot read event ids: {exc}", file=sys.stderr)
            return 2
        try:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    ids.append(int(line))
                except ValueError:
                    print(f"not an event id: {line}", file=sys.stderr)
                    return 2
        finally:
            if stream is not sys.stdin:
                stream.close()
    if not ids:
        print("give event ids or --ids-from", file=sys.stderr)
        return 2
    items = _lookup.show(con, ids)
    if ns.as_json:
        print(json.dumps(items, indent=2, ensure_ascii=False, default=str))
    else:
        print(_lookup.render(items))
    # Any event without content is visible in the exit status.
    return 0 if all(i["status"] == "ok" for i in items) else 3


def _capture(con, ns) -> int:
    if ns.op == "assign-session":
        con.execute("BEGIN IMMEDIATE")
        if not con.execute("SELECT 1 FROM tasks WHERE task_id=?", (ns.task,)).fetchone():
            print(f"unknown task: {ns.task}", file=sys.stderr)
            return 2
        if not con.execute("SELECT 1 FROM sessions WHERE session_key=?", (ns.session,)).fetchone():
            print(f"unknown session: {ns.session}", file=sys.stderr)
            return 2
        other = con.execute(
            "SELECT task_id FROM session_assignments WHERE session_key=? AND task_id<>?"
            " UNION SELECT a.task_id FROM assignments a JOIN user_messages s"
            " ON s.native_id=a.user_message_id WHERE s.session_key=? AND a.task_id<>?",
            (ns.session, ns.task, ns.session, ns.task)).fetchone()
        if other or not ns.evidence.strip():
            print("ownership conflict or missing evidence for exclusive session", file=sys.stderr)
            return 2
        con.execute(
            "INSERT OR IGNORE INTO session_assignments(session_key,task_id,evidence,created_at)"
            " VALUES(?,?,?,?)", (ns.session, ns.task, ns.evidence, _db.now()))
        con.commit()
        _emit({"ok": True, "session": ns.session, "task": ns.task}, ns.as_json)
        return 0
    if ns.op == "create-task":
        con.execute(
            "INSERT OR IGNORE INTO tasks(task_id, project, family, title,"
            " issue_url, created_at) VALUES(?,?,?,?,?,?)",
            (ns.task, ns.project, ns.family, ns.title, ns.issue, _db.now()))
        con.commit()
        _emit({"ok": True, "task": ns.task}, ns.as_json)
        return 0
    if ns.op == "assign":
        con.execute("BEGIN IMMEDIATE")
        ns.user_message = _resolve_submission(con, ns.user_message)
        sub = con.execute("SELECT native_id, is_genuine FROM user_messages "
                          "WHERE native_id=?", (ns.user_message,)).fetchone()
        if sub is None:
            print(f"unknown user message: {ns.user_message}", file=sys.stderr)
            return 2
        if not sub["is_genuine"]:
            print(f"refused: {ns.user_message} is not a genuine user message",
                  file=sys.stderr)
            return 2
        if con.execute(
                "SELECT 1 FROM session_assignments w JOIN user_messages s"
                " ON s.session_key=w.session_key WHERE s.native_id=? AND w.task_id<>?",
                (ns.user_message, ns.task)).fetchone():
            print("ownership conflict: session already bound to another task", file=sys.stderr)
            return 2
        if con.execute("SELECT 1 FROM tasks WHERE task_id=?",
                       (ns.task,)).fetchone() is None:
            print(f"unknown task: {ns.task}", file=sys.stderr)
            return 2
        con.execute(
            "INSERT OR IGNORE INTO assignments(user_message_id, task_id,"
            " task_turn, phase, evidence, shared, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (ns.user_message, ns.task, ns.task_turn, ns.phase, ns.evidence,
             1 if ns.shared else 0, _db.now()))
        con.commit()
        _emit({"ok": True, "user_message": ns.user_message, "task": ns.task,
               "shared": bool(ns.shared)}, ns.as_json)
        return 0
    if ns.op == "delegation":
        ns.user_message = _resolve_submission(con, ns.user_message)
        if con.execute("SELECT 1 FROM user_messages WHERE native_id=?",
                       (ns.user_message,)).fetchone() is None:
            print(f"unknown user message: {ns.user_message}", file=sys.stderr)
            return 2
        con.execute(
            "INSERT OR IGNORE INTO delegations(owning_user_message, parent_turn,"
            " child_thread, child_turn, requested_model, requested_effort,"
            " policy_version, reason, task_name, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ns.user_message, ns.parent_turn, ns.child_thread, ns.child_turn,
             ns.requested_model, ns.requested_effort, ns.policy, ns.reason,
             ns.task_name, _db.now()))
        con.commit()
        _emit({"ok": True, "child_thread": ns.child_thread}, ns.as_json)
        return 0
    if ns.op == "task-turn":
        if ns.role == "worker":
            ns.role = "child"
        usable = None
        if ns.not_usable:
            usable = 0
        elif ns.usable:
            usable = 1
        con.execute(
            "INSERT OR IGNORE INTO task_turns(task_id, turn_id, role, harness,"
            " model_observed, effort_observed, state, usable_output, stage)"
            " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id, turn_id) DO UPDATE SET"
            " state=excluded.state, role=excluded.role,"
            " stage=COALESCE(excluded.stage,task_turns.stage),"
            " model_observed=COALESCE(excluded.model_observed,task_turns.model_observed),"
            " effort_observed=COALESCE(excluded.effort_observed,task_turns.effort_observed),"
            " usable_output=COALESCE(excluded.usable_output,task_turns.usable_output)",
            (ns.task, ns.turn, ns.role, ns.turn.split(":", 1)[0]
             if ":" in ns.turn else None, ns.model, ns.effort,
             ns.state, usable, ns.phase))
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
    if con.execute("SELECT 1 FROM user_messages WHERE native_id=?",
                   (given,)).fetchone():
        return given
    rows = con.execute("SELECT native_id FROM user_messages WHERE native_id LIKE ?",
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
    if "t3" in wanted and len(wanted) > 1 and "t3" in registry:
        # T3 attribution reads first so transcript imports classify
        # prompts as they land, but sessions imported later in this same
        # run did not exist for that first pass. One idempotent refresh
        # closes the loop so a single sync converges; its counters fold
        # into the first T3 row.
        refresh = registry["t3"].sync(con, root=ns.root, full=ns.full,
                                      source=ns.source)
        merged = False
        for row in results:
            if row.get("harness") != "t3" or merged:
                continue
            merged = True
            for key, value in refresh.items():
                if key == "failed":
                    row["failed"] = list(row.get("failed") or []) + value
                elif key in ("sources", "unchanged", "threads",
                             "turns_mapped", "links", "malformed",
                             "v2_threads", "v2_runs", "prompt_origins"):
                    # Mirror sizes, not deltas: the refresh re-reads the
                    # same state, so the larger reading wins.
                    row[key] = max(row.get(key) or 0, value or 0) \
                        if isinstance(value, int) else value
                elif isinstance(value, int):
                    row[key] = (row.get(key) or 0) + value
                elif key not in row:
                    row[key] = value
    _identity.refresh_session_versions(con)
    con.commit()
    payload = {"_view": "sync", "harnesses": results,
               "agentsmd_versions": mapped,
               "contract": {"schema": SCHEMA_VERSION,
                            "event": EVENT_CONTRACT_VERSION,
                            "capture": CAPTURE_CONTRACT_VERSION}}
    _emit(payload, ns.as_json)
    return 1 if any(r.get("failed") for r in results) else 0


def _when(value):
    if value is None:
        return None
    from .ingest import iso_ts
    return float(value) if str(value).replace(".", "").isdigit() else iso_ts(value)


def _nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def _json_lines(path: str) -> list:
    """Records from a JSON lines file or stdin; a bad line stays a
    non-object so validation names its line number."""
    fh = sys.stdin if path == "-" else open(path, encoding="utf-8")
    try:
        records = []
        for raw in fh:
            if not raw.strip():
                continue
            try:
                records.append(json.loads(raw))
            except ValueError:
                records.append(None)
        return records
    finally:
        if fh is not sys.stdin:
            fh.close()


def _attrs(values) -> dict:
    out = {}
    for value in values or []:
        name, sep, val = value.partition("=")
        if not sep or not name:
            raise ValueError(f"--attr needs NAME=VALUE, got {value!r}")
        out[name] = val
    return out


def _judge(con, ns) -> int:
    from . import verdicts as _verdicts
    as_json = getattr(ns, "as_json", False)
    try:
        # `human` is the schema-2 name for `user` (#65).
        chosen = ["user" if c == "human" else c
                  for c in getattr(ns, "created_by", None) or []]
        created_by = None if "all" in chosen \
            else tuple(chosen or _interventions.COUNTED)
        if ns.cmd == "interventions":
            if ns.judge and not ns.unjudged:
                raise ValueError("--judge needs --unjudged BEHAVIOR")
            payload = _interventions.select(
                con, since=_when(ns.since), until=_when(ns.until),
                harness=ns.harness, project=ns.project, created_by=created_by,
                kinds=ns.kind, unjudged=ns.unjudged, attrs=_attrs(ns.attr),
                limit=ns.limit, judge=ns.judge)
            if as_json:
                _emit(payload, True)
            else:
                counts = payload["counts"]
                print(f"{counts['selected']} interventions; in scope by created_by: "
                      + ", ".join(f"{k} {v}" for k, v in counts["by_created_by"].items())
                      + "; policy denials: "
                      + ", ".join(f"{k} {v}" for k, v in payload["policy_denials"].items()))
                for m in payload["interventions"]:
                    print(f"{m['id']}  {m['kind']} {m['created_by']} {m['at']} {m['project'] or ''}")
            return 0
        if ns.cmd == "intervention":
            ids = list(ns.ids)
            if ns.ids_from:
                fh = sys.stdin if ns.ids_from == "-" else open(ns.ids_from, encoding="utf-8")
                ids += [line.strip() for line in fh if line.strip()]
            if not ids:
                print("no intervention ids given", file=sys.stderr)
                return 2
            items = _interventions.show(con, ids, turns_before=ns.turns_before,
                                  turns_after=ns.turns_after, items=ns.items,
                                  chars=ns.chars, text_chars=ns.text_chars)
            if as_json:
                _emit({"interventions": items}, True)
            else:
                print(_interventions.render(items))
            return 3 if any(i["status"] != "ok" for i in items) else 0
        if ns.cmd == "verdict" and ns.op == "add":
            result = _verdicts.add(con, _json_lines(ns.file))
            _emit(result, True) if as_json else print(
                f"stored {result['stored']}, updated {result['updated']}")
            return 0
        if ns.cmd == "verdict" and ns.op == "list":
            listed = _verdicts.listing(con, ns.behavior, judge=ns.judge,
                                       verdict=ns.verdict)
            if as_json:
                _emit({"verdicts": listed}, True)
            else:
                for v in listed:
                    print(f"{v['intervention']}  {v['verdict']} {v['group'] or '-'}"
                          f" {v['problem'] or ''}")
            return 0
        if ns.cmd == "verdict":
            groups = _verdicts.groups(con, ns.behavior, judge=ns.judge,
                                      created_by=created_by)
            if as_json:
                _emit({"groups": groups}, True)
            else:
                for g in groups:
                    print(f"{g['group']} ({g['judge']}, {g['occurrences']}x): {g['problem']}")
            return 0
        if ns.cmd == "recurrence":
            report = _verdicts.recurrence(
                con, ns.behavior, judge=ns.judge, since=_when(ns.since),
                until=_when(ns.until), harness=ns.harness, project=ns.project,
                attrs=_attrs(ns.attr), created_by=created_by)
            _emit(report, True) if as_json else print(_verdicts.render(report))
            return 0
        result = _verdicts.add_session_attributes(con, _json_lines(ns.file))
        _emit(result, True) if as_json else print(
            f"stored {result['stored']}, updated {result['updated']}")
        return 0
    except _verdicts.VerdictError as exc:
        if as_json:
            _emit({"error": str(exc), "errors": exc.errors}, True)
        else:
            print(str(exc), file=sys.stderr)
            for e in exc.errors:
                print(f"line {e['line']}: {e['error']}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _analyze(con, ns) -> int:
    filters = {"since": _when(ns.since), "until": _when(ns.until),
               "project": ns.project, "harness": ns.harness,
               "agentsmd_version": ns.agentsmd_version}
    if ns.cmd == "diagnose":
        payload = _analysis.diagnose(con, detectors=ns.detector, limit=ns.limit,
                                     session=ns.session, **filters)
        if ns.as_json:
            _emit(payload, True)
            return 0
        print(f"{payload['sessions']} sessions; incidents: "
              + (", ".join(f"{k} {v}" for k, v in sorted(payload["counts"].items())) or "none"))
        for i in payload["incidents"]:
            mark = " (heuristic)" if i["heuristic"] else ""
            print(f"- {i['detector']}{mark} {i['session']} "
                  f"[{os.path.basename(i['project'] or '?')}] {i['summary']}")
        print(payload["note"])
        return 0
    payload = _analysis.compare(con, by=ns.by, **filters)
    if ns.as_json:
        _emit(payload, True)
        return 0
    for g in payload["groups"]:
        tokens = g["tokens_per_session"]
        prompts = g["genuine_prompts_per_session"]
        if "by_semantics" in tokens:
            median = "mixed semantics (see --json)"
        else:
            median = tokens.get("median", "-")
            median = "-" if median is None else f"{median:,}" if isinstance(median, (int, float)) else median
            if g.get("unknown_token_responses"):
                median = f">={median} (lower bound)" if median != "-" else "unknown"
        line = (f"{g['group']}: {g['sessions']} sessions, {g['projects']} projects, "
                f"median tokens {median}, "
                f"prompts/session {prompts.get('median', '-')} (n={prompts['n']}), "
                f"interrupts {g['interrupts']}")
        extras = ", ".join(f"{d} {g[d]['incidents']}" for d in _analysis.DETECTORS
                           if g[d]["incidents"])
        print(line + (f"; {extras}" if extras else ""))
    print(payload["note"])
    return 0


def _publish(con, ns) -> int:
    from . import publish as _publish_mod
    if ns.task:
        try:
            keys = _report.task_sessions(con, ns.task)
        except KeyError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        label = f"task {ns.task}"
    else:
        keys = set(ns.session)
        label = f"{len(keys)} session(s)"
    if not keys:
        print("nothing to publish: no sessions in scope", file=sys.stderr)
        return 2
    schedule = None
    if getattr(ns, "prices", None):
        from . import pricing as _pricing
        try:
            schedule = _pricing.load_schedule(ns.prices)
        except (OSError, ValueError) as exc:
            print(f"invalid price schedule: {exc}", file=sys.stderr)
            return 2
    try:
        summary = _publish_mod.summarize(con, keys, label, task_id=ns.task,
                                         schedule=schedule)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    body = _publish_mod.render(
        summary, "pr" if ns.pr is not None else "commit" if ns.commit else None)
    if ns.dry_run:
        if ns.as_json:
            # Structured dry run: the same summary data plus an explicit
            # target and the rendered body, as parseable JSON.
            _emit({"dry_run": True,
                   "target": {"task": ns.task,
                              "sessions": sorted(keys),
                              "repo": ns.repo, "pr": ns.pr,
                              "commit": ns.commit},
                   "summary": summary, "body": body}, True)
        else:
            print(body)
        return 0
    if not ns.repo or (ns.pr is None) == (ns.commit is None):
        print("publish needs --repo and exactly one of --pr or --commit", file=sys.stderr)
        return 2
    try:
        result = _publish_mod.post(ns.repo, body, pr=ns.pr, commit=ns.commit)
    except (RuntimeError, ValueError) as exc:
        print(f"publish failed: {exc}", file=sys.stderr)
        return 1
    _emit(result, ns.as_json)
    return 0


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
        payload["user_messages"] = {r["kind"]: r["n"] for r in con.execute(
            "SELECT kind, COUNT(*) n FROM user_messages WHERE session_key=?"
            " GROUP BY kind", (ns.session,))}
        _emit(payload, True)
        return 0
    q = ("SELECT s.* FROM sessions s WHERE 1=1")
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
    q += " ORDER BY COALESCE(s.ended_at, s.started_at) DESC LIMIT ?"
    args.append(ns.limit)
    rows = [dict(r) for r in con.execute(q, args)]
    for r in rows:
        r.pop("identity_json", None)
        usage = _report.scope_totals(con, {r["session_key"]})
        r["responses"] = usage.get("responses", 0)
        if "total_tokens" in usage:
            r["total_tokens"] = usage.get("total_tokens")
            r["unknown_tokens"] = (usage.get("unknown_counts") or {}).get(
                "total_tokens", 0)
        elif "by_semantics" in usage:
            # Mixed counter semantics: no combined total exists.
            r.pop("total_tokens", None)
            r.pop("unknown_tokens", None)
            r["by_semantics"] = usage["by_semantics"]
        else:
            r["total_tokens"] = usage.get("total_tokens")
    _emit({"_view": "sessions", "sessions": rows}, ns.as_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
