"""Muse Code native sessions (#127).

Muse writes one append-only ``session.jsonl`` per session under
``~/.local/share/muse/sessions/YYYY/MM/DD/<session id>/`` and internal
child sessions under ``<session dir>/subagent/<child id>/session.jsonl``.
Each line is one frame: ``record_type``, ``payload_type``,
``schema_version``, ``payload_schema_version``, native ``sequence``, frame
``id``, ``recorded_at`` (microseconds) and ``stream {id, kind: session}``.

One native tool call is observed several times: the committed call
(``assistant_tool_calls_committed``), a started and a terminal
``tool_batch`` effect, and the result committed to the model
(``tool_result_batch_committed``). They join only on ``call_id`` and
become one ``tool_call`` and at most one ``tool_result``; effects never
add calls. The terminal outcome gives the status, the committed text
gives the UTF-8 size; either may be missing and then stays NULL.

Because those observations span lines, a changed file is parsed whole
(complete lines only) and each event is written with its final values
through ``insert_event(reconcile=True)``. No join state is kept between
syncs.

MCP identity is a session-level rule: a name in the session's
``mcp_tool_identity_catalog`` is ``mcp`` (with server and raw tool), a
name only in the active toolset is ``builtin``, anything else (no
catalog or toolset, or conflicting catalog entries) is ``unknown``. A
name prefix is never evidence.

Usage (``model_completed``) is not imported: whether output includes
reasoning is not established, so Muse sessions are reported as
unmeasured rather than zero. Prompts, results, reasons, hooks and
memory text are never stored.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sqlite3

from .. import db
from ..identity import SessionIdentity
from ..ingest import JsonlSource, MissingNativeId, fingerprint, insert_event

HARNESS = "muse"

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".local", "share",
                            "muse", "sessions")

CAPABILITIES = [
    ("model_usage", False, "model_completed usage is not imported; output/reasoning counter semantics are unverified, so sessions read unmeasured"),
    ("tool_calls", True, "assistant_tool_calls_committed, one event per call_id"),
    ("tool_results", True, "terminal effect status and committed result UTF-8 size, joined on call_id"),
    ("mcp_identity", True, "tool_source from the session's MCP tool identity catalog and active toolset; never from a name prefix"),
    ("read_evidence", True, "read_file calls as read attempts"),
    ("skill_invocation", True, "read_skill calls"),
    ("skill_file_reads", False, "skill file paths are not recorded"),
    ("file_change", False, "edit and write calls stay tool calls; no file_change events"),
    ("compaction", False, "no compaction record observed"),
    ("lifecycle_task", True, "run start and terminal"),
    ("subagents", True, "memory reminder child links; child logs under subagent/"),
    ("human_input", False, "prompts are not imported"),
    ("instruction_identity", True, "AgentsMD version from read_file paths only"),
]

_SESSION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TARGET_ARGS = {"read_file": "path", "edit_file": "path", "write_file": "path",
                "bash": "command"}
_RUN_KINDS = frozenset({
    "started", "terminal", "model_request_configured",
    "assistant_tool_calls_committed", "tool_result_batch_committed",
    "memory_reminder_child_session_linked"})
_STATUS = {"completed": "ok", "failed": "error"}
# Consumed non-run payload types; each needs schema and payload version 1.
# Effect-started and session-opened carry nothing imported but are version
# gated like the rest.
_CONSUMED = (
    "runtime.mcp_tool_identity_catalog", "runtime.session.metadata",
    "session.opened.observed", "session.workspace_branch.observed",
    "session.end", "tool_batch.effect.started", "tool_batch.effect.terminal")


def _session_id(value) -> str | None:
    if isinstance(value, str) and _SESSION_ID_RE.fullmatch(value):
        return value
    return None


def _ts(value) -> float | None:
    """Muse ``recorded_at`` is integer microseconds since the epoch."""
    if type(value) is int and value > 0:
        return value / 1_000_000
    return None


def _str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def discover(root: str | None = None) -> list[str]:
    root = root or DEFAULT_ROOT
    main = glob.glob(os.path.join(root, "*", "*", "*", "*", "session.jsonl"))
    children = glob.glob(os.path.join(root, "*", "*", "*", "*", "subagent",
                                      "*", "session.jsonl"))
    return sorted(main + children)


def _layout_parent(path: str) -> tuple[str | None, str | None]:
    """(child id, parent id) when the file sits at <parent>/subagent/<child>/."""
    child_dir = os.path.dirname(os.path.abspath(path))
    sub_dir = os.path.dirname(child_dir)
    if os.path.basename(sub_dir) != "subagent":
        return None, None
    child = _session_id(os.path.basename(child_dir))
    parent = _session_id(os.path.basename(os.path.dirname(sub_dir)))
    if child is None or parent is None:
        return None, None
    return child, parent


class _State:
    """Everything one complete parse of a session file establishes."""

    def __init__(self):
        self.session = None
        self.catalog: dict[str, set] = {}
        self.catalog_seen = False
        self.active: set = set()
        self.toolset_seen = False
        self.calls: dict[str, dict] = {}
        self.terminals: dict[str, dict] = {}
        self.results: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.links: dict[str, dict] = {}
        self.project_dir = None
        self.branch = None
        self.model = None
        self.started_at = None
        self.ended_at = None

    def identity(self, name: str) -> dict:
        entries = self.catalog.get(name)
        if entries:
            if len(entries) == 1:
                server, tool = next(iter(entries))
                return {"tool_source": "mcp", "mcp_server": server, "mcp_tool": tool}
            return {"tool_source": "unknown"}
        if self.catalog_seen and self.toolset_seen and name in self.active:
            return {"tool_source": "builtin"}
        return {"tool_source": "unknown"}


def _parse(src: JsonlSource, totals: dict) -> _State:
    state = _State()

    def bad(ordinal, category, raw):
        src.error(ordinal, category, raw)
        totals["malformed"] += 1

    def extend(ts):
        if ts is not None:
            state.started_at = ts if state.started_at is None else min(state.started_at, ts)
            state.ended_at = ts if state.ended_at is None else max(state.ended_at, ts)

    def accept(sid, ts):
        """A consumed record passed validation: only now may it establish
        the session identity or widen the span. A rejected one does neither."""
        if state.session is None:
            state.session = sid
        extend(ts)

    for ordinal, obj, raw in src.records():
        if obj is None:
            bad(ordinal, "malformed_json", raw)
            continue
        if not isinstance(obj, dict) or obj.get("record_type") != "event":
            continue  # The retained envelope and non-event lines carry nothing consumed.
        ptype = obj.get("payload_type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        ts = _ts(obj.get("recorded_at"))
        # Discriminators are checked for type before any membership test,
        # so a malformed consumed record is quarantined, never a crash.
        run_frame = ptype == "runtime.session" and payload.get("kind") == "run"
        if run_frame and not isinstance(event.get("kind"), str):
            bad(ordinal, "schema_error", raw)
            continue
        consumed = (run_frame and event["kind"] in _RUN_KINDS) or ptype in _CONSUMED
        if not consumed:
            extend(ts)  # Unconsumed but well-formed frames still date the session.
            continue  # Deliberately unconsumed: envelope, kind=task, usage, others.
        if obj.get("schema_version") != 1 or obj.get("payload_schema_version") != 1:
            bad(ordinal, "unsupported_schema", raw)
            continue
        stream = obj.get("stream") if isinstance(obj.get("stream"), dict) else {}
        sid = _session_id(stream.get("id"))
        if sid is None:
            bad(ordinal, "missing_id", raw)
            continue
        if state.session is not None and sid != state.session:
            bad(ordinal, "identity_conflict", raw)
            continue
        seq = obj.get("sequence") if type(obj.get("sequence")) is int else None
        at = {"seq": seq, "ts": ts}
        record = payload.get("record") if isinstance(payload.get("record"), dict) else {}
        if ptype == "runtime.mcp_tool_identity_catalog":
            entries = payload.get("entries")
            if not isinstance(entries, list):
                bad(ordinal, "schema_error", raw)
                continue
            accept(sid, ts)
            state.catalog_seen = True
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name, server, tool = (_str(entry.get("canonical_id")),
                                      _str(entry.get("server_name")),
                                      _str(entry.get("raw_tool_name")))
                if name and server and tool:
                    state.catalog.setdefault(name, set()).add((server, tool))
        elif ptype == "runtime.session.metadata":
            accept(sid, ts)
            state.project_dir = state.project_dir or _str(record.get("workspace_root"))
            state.model = state.model or _str(record.get("model_id"))
        elif ptype == "session.workspace_branch.observed":
            accept(sid, ts)
            ref = record.get("reference") if isinstance(record.get("reference"), dict) else {}
            if ref.get("kind") == "branch" and _str(ref.get("name")):
                state.branch = ref["name"]
        elif ptype in ("session.end", "session.opened.observed",
                       "tool_batch.effect.started"):
            accept(sid, ts)  # Version gated only; adds no evidence beyond the span.
        elif ptype == "tool_batch.effect.terminal":
            call = _str(record.get("call_id"))
            outcome = record.get("outcome")
            if call is None:
                bad(ordinal, "missing_id", raw)
                continue
            if not isinstance(outcome, dict) or not isinstance(outcome.get("kind"), str):
                # No usable outcome: the result's status stays unknown.
                bad(ordinal, "schema_error", raw)
                continue
            accept(sid, ts)
            state.terminals[call] = {**at, "status": _STATUS.get(outcome["kind"])}
        else:
            run_id = _str(payload.get("run_id"))
            kind = event["kind"]
            if run_id is None:
                bad(ordinal, "missing_id", raw)
                continue
            at["run"] = run_id
            if kind == "started":
                frame = _str(obj.get("id"))
                if frame is None:
                    bad(ordinal, "missing_id", raw)
                    continue
                accept(sid, ts)
                state.runs.setdefault(run_id, {})["start"] = {**at, "frame": frame}
            elif kind == "terminal":
                frame = _str(obj.get("id"))
                if frame is None:
                    bad(ordinal, "missing_id", raw)
                    continue
                accept(sid, ts)
                state.runs.setdefault(run_id, {})["end"] = {
                    **at, "frame": frame, "terminal": _str(event.get("terminal"))}
            elif kind == "model_request_configured":
                accept(sid, ts)
                toolset = event.get("toolset") if isinstance(event.get("toolset"), dict) else {}
                active = toolset.get("active_tools")
                if isinstance(active, list):
                    state.toolset_seen = True
                    state.active |= {t for t in active if isinstance(t, str)}
            elif kind == "assistant_tool_calls_committed":
                items = event.get("tool_calls")
                if not isinstance(items, list):
                    bad(ordinal, "schema_error", raw)
                    continue
                # Every item is validated before the record may touch state:
                # one bad item rejects the whole batch, leaving no prefix.
                calls = []
                for item in items:
                    call = _str(item.get("call_id")) if isinstance(item, dict) else None
                    name = _str(item.get("name")) if isinstance(item, dict) else None
                    if call is None or name is None:
                        calls = None
                        break
                    args = None
                    if isinstance(item.get("args"), str):
                        try:
                            decoded = json.loads(item["args"])
                        except ValueError:
                            decoded = None
                        args = decoded if isinstance(decoded, dict) else None
                    calls.append((call, name, args))
                if calls is None:
                    bad(ordinal, "missing_id", raw)
                    continue
                accept(sid, ts)
                for call, name, args in calls:
                    state.calls.setdefault(call, {**at, "name": name, "args": args})
            elif kind == "tool_result_batch_committed":
                items = event.get("results")
                if not isinstance(items, list):
                    bad(ordinal, "schema_error", raw)
                    continue
                results = []
                for item in items:
                    call = _str(item.get("tool_call_id")) if isinstance(item, dict) else None
                    if call is None:
                        results = None
                        break
                    text = item.get("text")
                    results.append((call, len(text.encode("utf-8"))
                                    if isinstance(text, str) else None))
                if results is None:
                    bad(ordinal, "missing_id", raw)
                    continue
                accept(sid, ts)
                for call, size in results:
                    state.results[call] = {**at, "size": size}
            elif kind == "memory_reminder_child_session_linked":
                child = _session_id(event.get("child_session_id"))
                if child is None:
                    bad(ordinal, "missing_id", raw)
                    continue
                accept(sid, ts)
                state.links.setdefault(child, at)
    return state


def _write(con: sqlite3.Connection, src: JsonlSource, state: _State,
           stats: dict, parent: str | None) -> None:
    key = f"{HARNESS}:{state.session}"
    identity = SessionIdentity()

    def event(family, native_id, at, **kw):
        try:
            insert_event(con, stats, source_id=src.source_id, session_key=key,
                         family=family, native_id=native_id, ordinal=at["seq"],
                         ts=at["ts"], turn_id=f"{HARNESS}:{at['run']}"
                         if at.get("run") else None, reconcile=True, **kw)
        except MissingNativeId:
            src.error(None, "missing_id")

    for call, info in state.calls.items():
        name, args = info["name"], info["args"] or {}
        detail = state.identity(name)
        arg_key = _TARGET_ARGS.get(name)
        target = args.get(arg_key) if arg_key else None
        target = target if isinstance(target, str) else None
        event("tool_call", call, info, name=name, target=target, detail=detail,
              fingerprint=fingerprint(json.dumps(info["args"], sort_keys=True))
              if info["args"] is not None else None)
        terminal, result = state.terminals.get(call), state.results.get(call)
        status = terminal["status"] if terminal else None
        if terminal or result:
            first = min((x for x in (terminal, result) if x),
                        key=lambda x: (x["seq"] is None, x["seq"] or 0))
            event("tool_result", call, {**first, "run": info.get("run")},
                  name=name, status=status, detail=detail,
                  size_bytes=result["size"] if result else None)
        if name == "read_file" and target:
            identity.observe_path(target)
            event("read", call, info, name=name, target=target, status=status)
        if name == "read_skill":
            event("skill_invoke", call, info, name=name, target=args.get("name"))
    for run_id, run in state.runs.items():
        start, end = run.get("start"), run.get("end")
        if start:
            event("lifecycle", start["frame"], start, name="turn_started")
        if end:
            event("lifecycle", end["frame"], end, name="turn_ended",
                  status=end["terminal"])
        con.execute(
            "INSERT INTO turns(turn_id, source_id, session_key, session_id,"
            " model_observed, started_at, completed_at, duration_ms, state)"
            " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(turn_id) DO UPDATE SET"
            " model_observed=excluded.model_observed,"
            " started_at=excluded.started_at, completed_at=excluded.completed_at,"
            " duration_ms=excluded.duration_ms, state=excluded.state",
            (f"{HARNESS}:{run_id}", src.source_id, key, state.session, state.model,
             start["ts"] if start else None, end["ts"] if end else None,
             int((end["ts"] - start["ts"]) * 1000)
             if start and end and start["ts"] is not None and end["ts"] is not None else None,
             (end["terminal"] if end else "active")))
    for child, at in state.links.items():
        event("lifecycle", child, at, name="subagent_spawned")
        db.upsert_session(con, f"{HARNESS}:{child}", HARNESS, child, None,
                          parent_session_key=key, role="subagent")
    fields = dict(project_dir=state.project_dir, git_branch=state.branch,
                  started_at=state.started_at, ended_at=state.ended_at,
                  **identity.fields(con))
    if parent:
        fields.update(parent_session_key=f"{HARNESS}:{parent}", role="subagent")
    db.upsert_session(con, key, HARNESS, state.session, src.source_id, **fields)


def import_muse_file(con: sqlite3.Connection, path: str, full: bool = False) -> dict:
    stats = {"events_inserted": 0, "malformed": 0}
    src = JsonlSource(con, HARNESS, path, full=full)
    if src.unchanged:
        return {"unchanged": True}
    # A grown file still needs earlier lines: a result or catalog can follow
    # the call it completes across an append boundary.
    src.start_offset = 0
    state = _parse(src, stats)
    if state.session is None:
        src.finish()
        return stats
    layout_child, parent = _layout_parent(path)
    if layout_child is not None and layout_child != state.session:
        src.error(None, "identity_conflict")
        stats["malformed"] += 1
        parent = None
    _write(con, src, state, stats, parent)
    src.finish(session_id=state.session)
    return stats


def sync(con: sqlite3.Connection, root: str | None = None, full: bool = False,
         source: str | None = None) -> dict:
    paths = [source] if source else discover(root)
    totals = {"harness": HARNESS, "sources": 0, "unchanged": 0,
              "responses_inserted": 0, "events_inserted": 0,
              "events_updated": 0, "user_messages_inserted": 0,
              "malformed": 0, "failed": []}
    for path in paths:
        try:
            stats = import_muse_file(con, path, full=full)
            con.commit()
        except (OSError, sqlite3.DatabaseError, UnicodeDecodeError) as exc:
            con.rollback()
            totals["failed"].append({"path": path, "error": str(exc)})
            continue
        totals["sources"] += 1
        totals["unchanged"] += 1 if stats.get("unchanged") else 0
        for key in ("events_inserted", "events_updated", "malformed"):
            totals[key] += stats.get(key, 0)
    return totals
