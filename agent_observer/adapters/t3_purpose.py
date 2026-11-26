"""Local mirror of Chromeria's v1 executionPurpose and nativeExecution contracts.

Inputs are literal provider identities, before harness-specific ledger namespacing.
Sessions describe participation only. Segment and inventory rows describe native
evidence even when no input boundary or reconciled final usage exists.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
import re
import sqlite3
from typing import NamedTuple

from .. import db


# Separate tables keep an expected attempt with a missing turn, an unresolved
# segment, and an independently counted main-agent turn distinguishable.
TABLES = {
    "runs": """run_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, task_id TEXT,
        scope TEXT NOT NULL, reason TEXT, host TEXT, repository TEXT, kind TEXT, number INTEGER,
        provenance_json TEXT, status TEXT, version INTEGER, active_attempt_id TEXT, start_state TEXT""",
    "sessions": """run_id TEXT NOT NULL, session_key TEXT NOT NULL,
        PRIMARY KEY(run_id,session_key)""",
    "inputs": """run_id TEXT NOT NULL, session_key TEXT NOT NULL, native_id TEXT NOT NULL,
        kind TEXT NOT NULL, validation TEXT NOT NULL, attempt_id TEXT, provider_turn_id TEXT,
        segment_id TEXT""",
    "grok_calls": """session_key TEXT NOT NULL, call_id TEXT NOT NULL, provider_turn_id TEXT NOT NULL,
        PRIMARY KEY(session_key,call_id,provider_turn_id)""",
    "deliveries": """task_id TEXT NOT NULL, thread_id TEXT NOT NULL, host TEXT NOT NULL,
        repository TEXT NOT NULL, number INTEGER NOT NULL, url TEXT NOT NULL, declared_at TEXT NOT NULL,
        PRIMARY KEY(task_id,thread_id,url,declared_at)""",
    "attempts": """attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, provider_thread_id TEXT,
        provider_turn_id TEXT, status TEXT, session_key TEXT, provider_session_id TEXT, start_state TEXT""",
    "turns": """provider_turn_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, run_id TEXT NOT NULL,
        session_key TEXT, status TEXT, usage_json TEXT, context_usage_json TEXT,
        native_response_ids_json TEXT, usage_closure_json TEXT""",
    "capabilities": """provider_session_id TEXT PRIMARY KEY, driver TEXT, native_inputs TEXT,
        child_segments TEXT, background_inventory TEXT, native_usage_finality TEXT""",
    "segments": """owner_id TEXT NOT NULL, thread_id TEXT NOT NULL, segment_id TEXT NOT NULL,
        subagent_id TEXT, run_id TEXT, task_id TEXT, kind TEXT, driver TEXT, native_session_id TEXT,
        native_operation_id TEXT, native_child_id TEXT, source_json TEXT, boundary_json TEXT,
        status TEXT, usage_finality TEXT, notice_ids_json TEXT, wake_dispositions_json TEXT,
        provenance_json TEXT, observed_at TEXT, PRIMARY KEY(thread_id,owner_id,segment_id)""",
    "child_turns": """run_id TEXT NOT NULL, segment_id TEXT NOT NULL, provider_turn_id TEXT NOT NULL,
        session_key TEXT NOT NULL, status TEXT, usage_json TEXT, context_usage_json TEXT,
        PRIMARY KEY(run_id,segment_id,provider_turn_id)""",
    "workflow_members": """thread_id TEXT NOT NULL, subagent_id TEXT NOT NULL, member_index INTEGER NOT NULL,
        attempt INTEGER, agent_id TEXT, child_thread_id TEXT, state TEXT, total_tokens INTEGER""",
    "participants": """run_id TEXT, thread_id TEXT NOT NULL, kind TEXT NOT NULL, participant_id TEXT,
        child_thread_id TEXT, child_run_id TEXT, environment_id TEXT, status TEXT, group_id TEXT, availability TEXT, declaration_json TEXT""",
}
# Columns added after the first release of a mirror table. A ledger written by
# an earlier version gains them on its next refresh; readers treat a missing
# column as no statement.
ADDED = {"runs": ("start_state",), "attempts": ("start_state",),
         "turns": ("native_response_ids_json", "usage_closure_json")}
PROJECTION = "orchestration_v2_projection_"
ID = re.compile(r"[a-zA-Z0-9._:%/@+~=,-]{1,8192}", re.ASCII)
HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]+)?", re.I | re.ASCII)
REPO = re.compile(r"[a-z0-9_.-]+/[a-z0-9_.-]+", re.I | re.ASCII)
MAX_INT = 2 ** 53 - 1
DRIVERS = {"codex": "codex", "claudeAgent": "claude", "opencode": "opencode",
           "opencode2": "opencode", "grok": "grok", "muse": "muse"}
INPUT_TYPES = {"codex": ("turn", "codex_turn_started"),
               "claudeAgent": ("message", "claude_prompt_echo"),
               "opencode": ("message", "opencode_admitted_message")}
STATES = ("pending", "preparing", "queued", "starting", "running", "waiting", "completed",
          "failed", "cancelled", "superseded", "interrupted", "rolled_back")
INVALID = ("invalid", "invalid_purpose", None, None)
WITNESSES = {"claudeAgent": "process_ledger", "codex": "event_stream"}
REASON = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)


def _id(value):
    return value if isinstance(value, str) and ID.fullmatch(value) else None


def _number(value):
    return type(value) is int and 0 <= value <= MAX_INT


def _v1(value):
    return type(value.get("version")) is int and value["version"] == 1


def _list(value):
    return value if isinstance(value, list) else []


def _obj(value):
    return value if isinstance(value, dict) else {}


def _enum(value, choices):
    return value if isinstance(value, str) and value in choices else None


def _date(value):
    if isinstance(value, str) and len(value) <= 40 and "T" in value:
        try:
            if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None:
                return value
        except ValueError:
            pass
    return None


def _start(payload):
    """A run or attempt start: explicit null means it never started; absent proves nothing."""
    if "startedAt" not in payload:
        return None
    if payload["startedAt"] is None:
        return "not_started"
    return "started" if _date(payload["startedAt"]) else None


def _closure(value, driver):
    """Parse a v1 usage closure strictly. Anything else is no statement (None)."""
    c = _obj(value)
    if not _v1(c):
        return None
    if (c.get("state") == "main_agent_only" and c.get("witness") == WITNESSES.get(driver)
            and _number(c.get("uncountedCompactions"))):
        return _json(dict(version=1, state="main_agent_only", witness=c["witness"],
                          uncountedCompactions=c["uncountedCompactions"]))
    if (c.get("state") == "open" and driver in WITNESSES and isinstance(c.get("reason"), str)
            and REASON.fullmatch(c["reason"])):
        return _json(dict(version=1, state="open", reason=c["reason"]))
    return None


def _response_ids(value, driver):
    """Claude's listed main-agent response ids; malformed lists are no statement."""
    if driver != "claudeAgent" or not isinstance(value, list) or not all(_id(i) for i in value):
        return None
    return _json(sorted(set(value)))


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")) if value is not None else None


def _reference(value):
    ref = _obj(value)
    host, repo = ref.get("host"), ref.get("repository")
    n, kind = ref.get("number"), ref.get("kind")
    if (isinstance(host, str) and HOST.fullmatch(host) and len(host) <= 253
            and isinstance(repo, str) and REPO.fullmatch(repo) and len(repo) <= 200
            and kind in ("issue", "pr") and _number(n) and n > 0):
        return dict(host=host.lower(), repository=repo.lower(), kind=kind, number=n)


def task_id(con, ref):
    host, repo = ref["host"].lower(), ref["repository"].lower()
    coordinate = repo if host == "github.com" else f"{host}/{repo}/{ref['kind']}"
    canonical = f"{coordinate}#{ref['number']}"
    if con.execute("SELECT 1 FROM tasks WHERE task_id=?", (canonical,)).fetchone():
        return canonical
    # Existing GitHub links used the repository's original capitalization.
    # Keep the original ledger key (including its assignments and outcomes).
    prior = con.execute("SELECT task_id FROM tasks WHERE task_id=? COLLATE NOCASE LIMIT 2",
                        (canonical,)).fetchall()
    return prior[0][0] if len(prior) == 1 else canonical


def _url(ref):
    path = "issues" if ref["kind"] == "issue" else "pull"
    return f"https://{ref['host']}/{ref['repository']}/{path}/{ref['number']}"


def _task(con, ref):
    key = task_id(con, ref)
    con.execute("INSERT OR IGNORE INTO tasks(task_id,project,title,issue_url,origin,created_at)"
                " VALUES(?,?,?,?, 't3',?)", (key, ref["repository"], key, _url(ref), db.now()))
    return key


def _provenance(value):
    value = _obj(value)
    kind = value.get("type")
    if kind in ("declared", "legacy"):
        return {"type": kind}
    if kind == "selection" and _number(value.get("revision")):
        return {"type": kind, "revision": value["revision"]}
    if kind in ("native_operation", "delegated_completion"):
        ids = value.get("operationIds")
        if isinstance(ids, list) and all(_id(i) for i in ids):
            return {"type": kind, "operationIds": ids}
    if kind == "pull_request_watch":
        ref = _reference(value.get("reference"))
        date = _date(value.get("startedAt"))
        if ref and date:
            return {"type": kind, "reference": ref, "startedAt": date}
    fields = {"parent_run": ("threadId", "runId"), "source_run": ("threadId", "runId"),
              "target_run": ("threadId", "runId"), "delegated_launch": ("threadId", "runId", "taskId"),
              "remote_launch": ("threadId", "runId", "environmentId", "launchId"),
              "schedule": ("scheduledTaskId",)}
    if isinstance(kind, str) and kind in fields and all(_id(value.get(k)) for k in fields[kind]):
        return {"type": kind, **{k: value[k] for k in fields[kind]}}


def _purpose(value):
    p = _obj(value)
    provenance = _provenance(p.get("provenance"))
    if not _v1(p) or provenance is None:
        return INVALID
    scope = _obj(p.get("scope"))
    if scope.get("type") == "single":
        ref = _reference(scope.get("reference"))
        if ref:
            return "single", None, ref, provenance
    if scope.get("type") == "unscoped" and scope.get("reason") in (
            "deliberate", "legacy_no_intent", "mixed", "unknown_cause"):
        return "unscoped", scope["reason"], None, provenance
    return INVALID


def _rows(native, tables, name, where="", args=()):
    if PROJECTION + name not in tables:
        return
    for row in native.execute("SELECT * FROM " + PROJECTION + name + where, args):
        row = dict(row)
        try:
            payload = json.loads(row.pop("payload_json", None))
        except (ValueError, TypeError, RecursionError):
            payload = None
        yield row, _obj(payload)


class Snapshot(NamedTuple):
    con: sqlite3.Connection
    path: str

    def insert(self, table, *values):
        values = (self.path, *values)
        self.con.execute(f"INSERT INTO t3_source_{table} VALUES("
                         + ",".join("?" for _ in values) + ")", values)

    def task(self, ref):
        return _task(self.con, ref)


def _participant(snapshot, run, thread, kind, participant, *, child=None, child_run=None, remote=None,
                 status=None, group=None, declaration=None):
    remote = _obj(remote)
    scope, reason, ref, provenance = _purpose(declaration)
    declared = None
    if declaration is not None:
        declared = dict(scope=scope, reason=reason, reference=ref, provenance=provenance)
        if ref:
            snapshot.task(ref)
    snapshot.insert("participants", run, thread, kind, _id(participant), _id(remote.get("threadId") or child),
            _id(child_run or remote.get("runId")), _id(remote.get("environmentId")), _enum(status, STATES),
            _id(group), _enum(remote.get("availability"), ("current", "stale", "unsupported", "lost")),
            _json(declared))


class Provider(NamedTuple):
    thread: str
    driver: str
    instance: str | None
    session: str | None
    native_session: str | None


class Segment(NamedTuple):
    subagent: str | None
    driver: str
    native_session: str
    operation: str
    kind: str
    payload: dict
    source: dict
    purpose: tuple
    boundary: dict


def _matches(row, payload, *pairs):
    return all(column in row and key in payload and row[column] == payload[key]
               for column, key in pairs)


def _strong(value, driver):
    ref = _obj(value)
    if ref.get("strength") == "strong" and ref.get("driver") == driver:
        return _id(ref.get("nativeId"))


def _deliveries(snapshot, native, tables):
    aware = set()
    for row, p in _rows(native, tables, "threads"):
        thread = _id(row.get("thread_id"))
        if not thread or p.get("id") != thread:
            continue
        if "purposeSelection" in p or p.get("purposeDeliveries"):
            aware.add(thread)
        for d in map(_obj, _list(p.get("purposeDeliveries"))):
            purpose, pr = _reference(d.get("purpose")), _reference(d.get("pullRequest"))
            at = _date(d.get("declaredAt"))
            if _v1(d) and purpose and pr and pr["kind"] == "pr" and at:
                snapshot.insert("deliveries", snapshot.task(purpose), thread, pr["host"],
                        pr["repository"], pr["number"], _url(pr), at)
    return aware


def _runs(snapshot, native, tables):
    runs = {}
    for row, p in _rows(native, tables, "runs"):
        run, thread = _id(row.get("run_id")), _id(row.get("thread_id"))
        if not run or not thread or (p and "purpose" not in p):
            continue  # A readable pre-v1 run keeps its existing legacy treatment.
        purpose = _purpose(p.get("purpose")) if _matches(
            row, p, ("run_id", "id"), ("thread_id", "threadId")) else INVALID
        scope, reason, ref, provenance = purpose
        key = snapshot.task(ref) if ref else None
        coords = ref or {}
        status = _enum(row.get("status"), STATES)
        version = _obj(p.get("purpose")).get("version")
        snapshot.insert("runs", run, thread, key, scope, reason, coords.get("host"),
                coords.get("repository"), coords.get("kind"), coords.get("number"),
                _json(provenance), status, version if _number(version) else None,
                _id(p.get("activeAttemptId")), _start(p))
        runs[run] = (thread, purpose)
        delivery = _obj(_obj(p.get("delegatedCompletion")).get("delivery"))
        for task in _list(delivery.get("taskIds")):
            if _id(task):
                _participant(snapshot, run, thread, "completion_member", task)
        for cause in map(_obj, _list(delivery.get("purposeCauses"))):
            if _id(cause.get("taskId")) and _id(cause.get("runId")):
                _participant(snapshot, run, thread, "completion_cause", cause["taskId"],
                             child_run=cause["runId"], declaration=cause.get("purpose"))
        for work in map(_obj, _list(p.get("restartCancelledBackgroundWork"))):
            category = _enum(work.get("kind"), ("subagent", "shell", "monitor", "task"))
            if category:
                _participant(snapshot, run, thread, "cancelled_" + category, work.get("id"), status="cancelled")
    return runs


def _usage(value, context=False):
    p = _obj(value)
    if context:
        if not _number(p.get("usedTokens")):
            return None
        result = {"usedTokens": p["usedTokens"]}
        fields = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "maxTokens")
    else:
        status = _enum(p.get("usageStatus"), ("complete", "partial", "unavailable"))
        if (not status or p.get("usageScope") != "main_agent" or type(p.get("hasSubagents")) is not bool
                or (status == "complete" and not all(_number(p.get(k)) for k in ("inputTokens", "outputTokens")))):
            return None
        result = dict(usageScope="main_agent", usageStatus=status, hasSubagents=p["hasSubagents"])
        if "localCommand" in p:
            if type(p["localCommand"]) is not bool:
                return None
            result["localCommand"] = p["localCommand"]
        if "otherUsage" in p:
            other = _obj(p["otherUsage"])
            if not all(_number(other.get(k)) for k in ("inputTokens", "outputTokens")):
                return None
            result["otherUsage"] = {k: other[k] for k in ("inputTokens", "outputTokens")}
        if "models" in p:
            if not isinstance(p["models"], list) or not all(_id(m) for m in p["models"]):
                return None
            result["models"] = p["models"]
        fields = ("inputTokens", "outputTokens", "cachedInputTokens", "cacheCreationTokens", "reasoningTokens")
    for k in fields:
        if k in p and not (context and k == "maxTokens" and p[k] is None):
            if not _number(p[k]):
                return None
            result[k] = p[k]
    return _json(result)


def _capabilities(snapshot, native, tables):
    for row, p in _rows(native, tables, "provider_sessions"):
        session = _id(row.get("provider_session_id"))
        cap = _obj(_obj(p.get("capabilities")).get("purposeEvidence"))
        if session and session == p.get("id") and _v1(cap):
            snapshot.insert("capabilities", session, _enum(p.get("driver"), DRIVERS),
                    _enum(cap.get("nativeInputs"), ("native_turn", "input_echo", "admitted_message", "unsupported")),
                    _enum(cap.get("childSegments"), ("initial_turn_only", "initial_input_only", "causes_only", "unsupported")),
                    _enum(cap.get("backgroundInventory"), ("observed_causes", "unsupported")),
                    _enum(cap.get("nativeUsageFinality"), ("main_agent_only", "unavailable")))


def _providers(snapshot, native, tables, runs, segments):
    providers = {}
    for row, p in _rows(native, tables, "provider_threads"):
        pt = _id(row.get("provider_thread_id"))
        thread, driver = _id(row.get("thread_id")), _enum(p.get("driver"), DRIVERS)
        if (not pt or not thread or not driver or not _matches(row, p, ("provider_thread_id", "id"),
                ("thread_id", "appThreadId")) or row.get("driver", driver) != driver
                or row.get("provider_instance_id", p.get("providerInstanceId")) != p.get("providerInstanceId")):
            continue
        providers[pt] = Provider(thread, driver, _id(p.get("providerInstanceId")), _id(p.get("providerSessionId")),
                         _strong(p.get("nativeThreadRef"), driver))
        for segment in map(_obj, _list(p.get("nativeOperations"))):
            segments.append((thread, pt, None, driver, segment))
        for task in map(_obj, _list(p.get("pendingBackgroundTasks"))):
            run = _id(task.get("runId"))
            if run in runs and runs[run][0] == thread and _id(task.get("taskId")):
                _participant(snapshot, run, thread, "pending_background", task["taskId"],
                             child=task.get("childThreadId"), remote={"environmentId": task.get("childEnvironmentId")})
    return providers


def _subagents(snapshot, native, tables, runs, segments):
    children = {}
    for row, p in _rows(native, tables, "subagents"):
        child = _id(row.get("subagent_id"))
        thread, origin = _id(row.get("thread_id")), _enum(p.get("origin"), ("app_owned", "provider_native"))
        driver = _enum(p.get("driver"), DRIVERS)
        if (not child or not thread or not origin or not _matches(row, p, ("subagent_id", "id"),
                ("thread_id", "threadId"), ("origin", "origin"), ("run_id", "runId"))
                or row.get("driver", driver) != driver
                or row.get("child_thread_id") != p.get("childThreadId")):
            continue
        run = _id(row["run_id"])
        if run in runs and runs[run][0] == thread:
            remote = _obj(p.get("remoteChild"))
            _participant(snapshot, run, thread, origin, child, child=p.get("childThreadId"), remote=remote,
                         status=p.get("status"), group=p.get("runGroupId"), declaration=p.get("purpose"))
            completion = _obj(p.get("completionSource"))
            if _id(completion.get("runId")):
                _participant(snapshot, run, thread, "child_completion", child, child=p.get("childThreadId"),
                             remote=remote, child_run=completion["runId"], declaration=completion.get("purpose"))
        for member in map(_obj, _list(_obj(p.get("workflow")).get("agents"))):
            if _number(member.get("index")):
                snapshot.insert("workflow_members", thread, child, member["index"],
                        member["attempt"] if _number(member.get("attempt")) else None,
                        _id(member.get("agentId")), _id(member.get("childThreadId")),
                        _enum(member.get("state"), ("queued", "running", "completed", "failed")),
                        member["totalTokens"] if _number(member.get("totalTokens")) else None)
        if driver and origin == "provider_native":
            children[(thread, child)] = (_id(p.get("childThreadId")), _id(p.get("providerThreadId")))
            for segment in map(_obj, _list(p.get("nativeSegments"))):
                segments.append((thread, child, child, driver, segment))

    return children


def _expected_children(snapshot, native, tables, runs):
    # A subagent turn item names the expected child even if its own projection
    # or the remote ledger is missing. Avoid reading unrelated text/tool items.
    for row, p in _rows(native, tables, "turn_items", " WHERE type='subagent'"):
        run = _id(row.get("run_id"))
        if (run not in runs or runs[run][0] != row.get("thread_id")
                or not _matches(row, p, ("run_id", "runId"), ("thread_id", "threadId"),
                                ("turn_item_id", "id"), ("type", "type"))
                or not _id(p.get("subagentId"))):
            continue
        remote = _obj(p.get("remoteChild"))
        _participant(snapshot, run, row["thread_id"], "subagent_item", p["subagentId"],
                     child=p.get("childThreadId"), remote=remote, status=p.get("status"))


def _attempts(snapshot, native, tables, runs, providers):
    attempts = {}
    for row, p in _rows(native, tables, "run_attempts"):
        aid, run = _id(row.get("attempt_id")), _id(row.get("run_id"))
        if not aid or run not in runs or row.get("thread_id") != runs[run][0]:
            continue
        pt = _id(row.get("provider_thread_id"))
        turn, root = _id(row.get("provider_turn_id")), _id(row.get("root_node_id"))
        provider = providers.get(pt)
        valid = (provider and root and provider.thread == runs[run][0] and provider.instance is not None
                 and p.get("providerInstanceId") == provider.instance
                 and row.get("provider_instance_id", provider.instance) == provider.instance
                 and _matches(row, p, ("attempt_id", "id"), ("run_id", "runId"),
                     ("provider_thread_id", "providerThreadId"), ("provider_turn_id", "providerTurnId"),
                     ("root_node_id", "rootNodeId")))
        key = None
        if valid:
            driver = provider.driver
            sid = _id(p.get("nativeThreadId"))
            if sid:
                key = f"{DRIVERS[driver]}:{sid}"
                snapshot.insert("sessions", run, key)
            attempts[aid] = (run, provider.thread, pt, root, turn, driver, sid, "nativeThreadId" in p)
        snapshot.insert("attempts", aid, run, pt, turn, _enum(p.get("status"), STATES),
                key, provider.session if valid else None, _start(p))
    return attempts


def _grok_call_refs(snapshot, native, tables, owner, columns):
    if "provider_turn_id" not in columns:
        return
    key, run, thread, pt, turn = owner
    for row, item in _rows(native, tables, "turn_items", " WHERE provider_turn_id=?", (turn,)):
        if (row.get("type") not in {"dynamic_tool", "file_search", "web_search", "command_execution", "file_change"}
                or (row.get("run_id"), row.get("thread_id"), row.get("provider_thread_id")) != (run, thread, pt)
                or not _matches(row, item, ("run_id", "runId"), ("thread_id", "threadId"),
                                ("provider_thread_id", "providerThreadId"), ("provider_turn_id", "providerTurnId"),
                                ("turn_item_id", "id"), ("type", "type"))):
            continue
        call = _strong(item.get("nativeItemRef"), "grok")
        if call and _id(row["turn_item_id"]):
            snapshot.insert("grok_calls", key, call, turn, row["turn_item_id"])


def _turns(snapshot, native, tables, attempts):
    item_columns = {r[1] for r in native.execute("PRAGMA table_info(orchestration_v2_projection_turn_items)")}
    for row, p in _rows(native, tables, "provider_turns", " WHERE run_attempt_id IS NOT NULL"):
        aid = _id(row.get("run_attempt_id"))
        if aid not in attempts:
            continue
        run, thread, pt, root, expected, driver, sid, has_destination = attempts[aid]
        turn = _id(row.get("provider_turn_id"))
        if (not turn or (expected and turn != expected) or row.get("node_id") != root
                or row.get("thread_id") != thread or row.get("provider_thread_id") != pt
                or not _matches(row, p, ("node_id", "nodeId"), ("provider_turn_id", "id"),
                    ("run_attempt_id", "runAttemptId"), ("provider_thread_id", "providerThreadId"))):
            continue
        # A reusable provider thread's latest cursor cannot locate a historical input.
        turn_sid = _strong(p.get("nativeThreadRef"), driver)
        if "nativeThreadRef" in p and (not turn_sid or (has_destination and turn_sid != sid)):
            sid = None
        else:
            sid = turn_sid or sid
        key = f"{DRIVERS[driver]}:{sid}" if sid else None
        snapshot.insert("turns", turn, aid, run, key, _enum(p.get("status"), STATES),
                _usage(p.get("turnTokenUsage")), _usage(p.get("tokenUsage"), context=True),
                _response_ids(p.get("nativeResponseIds"), driver), _closure(p.get("usageClosure"), driver))
        if not key:
            continue
        snapshot.insert("sessions", run, key)
        witnesses = set()
        for i in map(_obj, _list(p.get("nativeInputs"))):
            identity = _id(i.get("nativeId"))
            pair = (i.get("kind"), i.get("validation"))
            if identity and _v1(i) and INPUT_TYPES.get(driver) == pair:
                witnesses.add((identity, *pair))
        if driver == "grok":
            _grok_call_refs(snapshot, native, tables, (key, run, thread, pt, turn), item_columns)
            identity = _strong(p.get("nativeTurnRef"), driver)
            if identity and re.fullmatch(r"prompt:(0|[1-9][0-9]{0,15})", identity) and int(identity[7:]) <= MAX_INT:
                witnesses.add((identity, "message", "grok_prompt_index"))
        if p.get("promptDropped") is not True:
            for identity, kind, validation in sorted(witnesses):
                snapshot.insert("inputs", run, key, identity, kind, validation, aid, turn, None)


# Native unresolved reasons are open strings. Only known codes can leave the source.
REASONS = frozenset("""
    workflow_member_native_input_boundary_missing owning_child_segment_unresolved
    owning_child_call_missing native_start_call_missing native_start_call_not_observed
    child_native_input_boundary_missing claude_resume_response_boundary_missing
    background_start_call_not_observed background_native_usage_boundary_unavailable
    background_start_not_observed codex_owning_child_turn_unresolved awaiting_initial_native_child_turn
    codex_resume_response_boundary_missing opencode_child_resume_boundary_missing
    awaiting_initial_native_child_input
""".split())


def _source(value):
    s = _obj(value)
    kind = s.get("type")
    if kind in ("app_run", "native_segment"):
        keys = ("threadId", "runId") if kind == "app_run" else ("threadId", "subagentId", "segmentId")
        if all(_id(s.get(k)) for k in keys):
            return {"type": kind, **{k: s[k] for k in keys}}
    return {"type": "unresolved", "reason": _enum(s.get("reason"), REASONS) or "unknown"}


def _boundary(value):
    b = _obj(value)
    if b.get("state") == "native_turns":
        turns = _list(b.get("nativeTurnIds"))
        if turns and all(_id(t) for t in turns):
            return dict(state="native_turns", nativeTurnIds=turns)
    if b.get("state") == "native_input" and _id(b.get("nativeInputId")):
        return dict(state="native_input", nativeInputId=b["nativeInputId"])
    return {"state": "unresolved", "reason": _enum(b.get("reason"), REASONS) or "unknown"}


def _segments(snapshot, rows, runs, children):
    child_boundaries = {}
    # An enclosing subagent's runId names its original launch. Each segment
    # carries its own initiating run or earlier segment, including on resume.
    segments = {}
    for thread, owner, subagent, driver, p in rows:
        sid, operation, segment = (_id(p.get(k)) for k in ("nativeSessionId", "nativeOperationId", "id"))
        kind = _enum(p.get("kind"), ("start", "resume", "workflow_member", "background"))
        if not (_v1(p) and sid and operation and segment and kind and p.get("usageFinality") == "unreconciled"
                and p.get("status") in ("running", "ended", "roster_absent", "interrupted", "unknown")):
            continue
        key = thread, owner, segment
        data = Segment(subagent, driver, sid, operation, kind, p, _source(p.get("source")),
                _purpose(p.get("purpose")), _boundary(p.get("boundary")))
        # Conflicting copies of one initiating operation cannot prove inputs.
        if key in segments and segments[key] != data:
            segments[key] = None
        else:
            segments[key] = data

    destinations = {(r[0], r[1]) for r in snapshot.con.execute(
        "SELECT run_id,session_key FROM t3_source_sessions WHERE source_path=?", (snapshot.path,))}
    resolved = {}
    # Resolve exact predecessor edges independent of projection table order.
    # Unresolved edges, cycles, crossed scopes and crossed sessions stay gaps.
    pending = {k for k, v in segments.items() if v}
    while pending:
        done = set()
        for key in pending:
            subagent, driver, sid, op, kind, p, source, purpose, boundary = segments[key]
            if purpose == INVALID:
                continue
            if source["type"] == "app_run":
                run = source["runId"]
                if (runs.get(run) != (source["threadId"], purpose)
                        or (run, f"{DRIVERS[driver]}:{sid}") not in destinations):
                    continue
            elif source["type"] == "native_segment":
                parent = source["threadId"], source["subagentId"], source["segmentId"]
                run = resolved.get(parent)
                if run is None:
                    continue
                parent_data = segments[parent]
                if (not parent_data.subagent or parent_data.driver != driver
                        or parent_data.payload.get("nativeChildId") != sid
                        or parent_data.purpose != purpose or parent_data.boundary["state"] == "unresolved"):
                    continue
            else:
                continue
            resolved[key] = run
            done.add(key)
        if not done:
            break
        pending -= done

    for key, data in segments.items():
        if data is None:
            continue
        thread, owner, segment = key
        subagent, driver, sid, operation, kind, p, source, purpose, boundary = data
        child = _id(p.get("nativeChildId"))
        run = resolved.get(key)
        ref = purpose[2]
        snapshot.insert("segments", owner, thread, segment, subagent, run,
                snapshot.task(ref) if ref else None,
                kind, driver, sid, operation, child, _json(source), _json(boundary),
                _enum(p.get("status"), ("running", "ended", "roster_absent", "interrupted", "unknown")),
                _enum(p.get("usageFinality"), ("unreconciled",)),
                _json([i for i in _list(p.get("noticeIds")) if _id(i)]),
                _json([i for i in _list(p.get("wakeDispositions")) if i in (
                    "offered", "consumed_in_active", "dropped_disabled", "dropped_restart", "duplicate")]),
                _json(purpose[3]), _date(p.get("observedAt")))
        if not (run and subagent and child and kind != "background"):
            continue
        if driver not in ("codex", "opencode"):
            continue
        child_key = f"{DRIVERS[driver]}:{child}"
        snapshot.insert("sessions", run, child_key)
        # Current native contracts certify only Codex child turn boundaries
        # and OpenCode child input boundaries. Claude task ids are reusable.
        if driver == "codex" and boundary["state"] == "native_turns":
            ids = boundary["nativeTurnIds"]
            input_kind = "turn"
        elif driver == "opencode" and boundary["state"] == "native_input":
            ids = [boundary["nativeInputId"]]
            input_kind = "message"
        else:
            continue
        child_thread, child_pt = children[(thread, subagent)]
        for identity in sorted(set(ids)):
            snapshot.insert("inputs", run, child_key, identity, input_kind,
                    "segment_" + boundary["state"], None, None, segment)
            if child_thread and child_pt:
                lookup = (child_thread, child_pt, driver, child, identity)
                child_boundaries.setdefault(lookup, set()).add((run, segment))
    return child_boundaries


def _child_turns(snapshot, native, tables, boundaries, providers):
    if not boundaries:
        return
    for row, p in _rows(native, tables, "provider_turns", " WHERE run_attempt_id IS NULL"):
        pt, turn = _id(row.get("provider_thread_id")), _id(row.get("provider_turn_id"))
        provider = providers.get(pt)
        if (not turn or not provider or provider.thread != row.get("thread_id")
                or not _matches(row, p, ("provider_turn_id", "id"), ("node_id", "nodeId"),
                                ("provider_thread_id", "providerThreadId"), ("run_attempt_id", "runAttemptId"))):
            continue
        thread, driver, _, _, provider_sid = provider
        sid = _strong(p["nativeThreadRef"], driver) if "nativeThreadRef" in p else provider_sid
        for i in map(_obj, _list(p.get("nativeInputs"))):
            native_id = _id(i.get("nativeId"))
            if not native_id or not _v1(i) or (i.get("kind"), i.get("validation")) != INPUT_TYPES.get(driver):
                continue
            for run, segment in boundaries.get((thread, pt, driver, sid, native_id), ()):
                snapshot.insert("child_turns", run, segment, turn, f"{DRIVERS[driver]}:{sid}",
                        _enum(p.get("status"), STATES), _usage(p.get("turnTokenUsage")),
                        _usage(p.get("tokenUsage"), context=True))


# In a snapshot the primary key identifies the native object. Across snapshots
# an object's immutable coordinates must agree. Different mutable readings
# leave that field unknown until the snapshots agree again.
MUTABLE = {
    "runs": {"status", "active_attempt_id", "start_state"},
    "attempts": {"status", "start_state"},
    "turns": {"status", "usage_json", "context_usage_json", "native_response_ids_json", "usage_closure_json"},
    "segments": {"status", "observed_at", "notice_ids_json", "wake_dispositions_json"},
    "child_turns": {"status", "usage_json", "context_usage_json"},
}


def _source_tables(con, path):
    # The previous writer replaced the *whole* mirror, then recorded its path
    # and import time in the same savepoint. Preserve that last source on upgrade.
    prior = con.execute("SELECT path,imported_at FROM sources WHERE harness='t3'"
                        " ORDER BY imported_at DESC LIMIT 2").fetchall()
    old_path = (os.path.realpath(prior[0][0]) if prior and
                (len(prior) == 1 or prior[0][1] != prior[1][1]) else None)
    for table, columns in TABLES.items():
        mirror, source = f"t3_purpose_{table}", f"t3_source_{table}"
        con.execute(f"CREATE TABLE IF NOT EXISTS {mirror} ({columns})")
        for name in ADDED.get(table, ()):
            # Appending keeps the positional source layout: path, then mirror columns.
            for target in (mirror, source):
                if db._has_table(con, target) and name not in {
                        r[1] for r in con.execute(f"PRAGMA table_info({target})")}:
                    con.execute(f"ALTER TABLE {target} ADD COLUMN {name} TEXT")
        if not db._has_table(con, source):
            con.execute(f"CREATE TABLE {source} AS SELECT CAST(NULL AS TEXT) AS source_path,"
                        f" * FROM {mirror} WHERE 0")
            if table == "grok_calls":
                con.execute(f"ALTER TABLE {source} ADD COLUMN turn_item_id TEXT")
            con.execute(f"CREATE INDEX {source}_path ON {source}(source_path)")
            if old_path and old_path != path:
                extra = ",NULL" if table == "grok_calls" else ""
                con.execute(f"INSERT INTO {source} SELECT ?,*{extra} FROM {mirror}", (old_path,))
        con.execute(f"DELETE FROM {source} WHERE source_path=?", (path,))


def _upgrade_retained_run_keys(con):
    # Other source files may have rotated away. A run retained its full
    # reference, and only a resolved segment proved it had that run's purpose.
    # Old delivery rows retained the delivery PR but lost the purpose kind.
    for row in con.execute("""
        SELECT DISTINCT host,repository,kind,number,task_id FROM t3_source_runs
        WHERE host <> 'github.com' AND task_id = host || '/' || repository || '#' || number
        COLLATE NOCASE""").fetchall():
        new = _task(con, row)
        args = (new, row["task_id"], row["kind"])
        con.execute("""UPDATE t3_source_segments AS s SET task_id=?
            WHERE task_id=? AND EXISTS (
                SELECT 1 FROM t3_source_runs r WHERE r.source_path=s.source_path
                AND r.run_id=s.run_id AND r.task_id=s.task_id AND r.kind=?)""", args)
        con.execute("UPDATE t3_source_runs SET task_id=? WHERE task_id=? AND kind=?", args)


def _materialize(con, table):
    mirror, source = f"t3_purpose_{table}", f"t3_source_{table}"
    info = con.execute(f"PRAGMA table_info({mirror})").fetchall()
    names = [row["name"] for row in info]
    keys = [row["name"] for row in info if row["pk"]]
    mutable = MUTABLE.get(table, set())
    con.execute(f"DELETE FROM {mirror}")
    if not keys:
        con.execute(f"INSERT INTO {mirror} SELECT DISTINCT {','.join(names)} FROM {source}")
        return

    def agreed(name):
        # COUNT ignores NULL. Missing and present values also disagree.
        return (f"CASE WHEN count({name})=count(*) AND min({name})=max({name})"
                f" THEN min({name}) END" if name in mutable else name)

    immutable = [name for name in names if name not in mutable and name not in keys]
    having = (f" HAVING count(DISTINCT json_array({','.join(immutable)}))=1" if immutable else "")
    con.execute(f"INSERT INTO {mirror} SELECT {','.join(map(agreed, names))}"
                f" FROM {source} GROUP BY {','.join(keys)}{having}")


def _exclude_disputed_calls(con):
    # A turn item is immutable even when two copies disagree about its call
    # ID. Do not let either version establish a call-to-completion bridge.
    con.execute("""WITH disputed AS (
        SELECT turn_item_id FROM t3_source_grok_calls WHERE turn_item_id IS NOT NULL
        GROUP BY turn_item_id HAVING count(DISTINCT json_array(session_key,call_id,provider_turn_id))>1
    )
    DELETE FROM t3_purpose_grok_calls WHERE (session_key,call_id) IN (
        SELECT session_key,call_id FROM t3_source_grok_calls WHERE turn_item_id IN disputed)""")


def _exclude_disputed_inputs(con):
    # Inputs are parsed under source-local parents. Withhold the native input
    # on every path if one parent key has contradictory immutable coordinates.
    # This also prevents a different claimant from winning by elimination.
    con.execute("""WITH disputed_segment_runs AS (
        -- A nested segment can inherit the disputed segment as a predecessor.
        SELECT s.run_id FROM t3_source_segments s LEFT JOIN t3_purpose_segments t
        USING(thread_id,owner_id,segment_id) WHERE t.owner_id IS NULL
    ), turn_sources AS (
        SELECT provider_turn_id,count(DISTINCT source_path) AS copies
        FROM t3_source_turns GROUP BY provider_turn_id
    ), disputed_inputs AS (
        SELECT i.session_key,i.native_id FROM t3_source_inputs i
        LEFT JOIN t3_purpose_runs r ON r.run_id=i.run_id
        LEFT JOIN t3_purpose_attempts a ON a.attempt_id=i.attempt_id
        LEFT JOIN t3_purpose_turns t ON t.provider_turn_id=i.provider_turn_id
        WHERE r.run_id IS NULL OR (i.attempt_id IS NOT NULL AND a.attempt_id IS NULL)
           OR (i.provider_turn_id IS NOT NULL AND t.provider_turn_id IS NULL)
           OR (i.segment_id IS NOT NULL AND i.run_id IN disputed_segment_runs)
        UNION
        -- Only source-local turn/input joins can witness a response. In
        -- particular a Grok call in one copy cannot borrow another copy's
        -- prompt index when its own turn has no such index.
        SELECT i.session_key,i.native_id FROM t3_source_inputs i JOIN turn_sources USING(provider_turn_id)
        GROUP BY i.session_key,i.native_id,i.provider_turn_id
        HAVING count(DISTINCT i.source_path)<copies
    )
    DELETE FROM t3_purpose_inputs WHERE (session_key,native_id) IN disputed_inputs""")


def refresh(con, native, tables):
    """Replace one source and publish the consensus under the caller's savepoint."""
    path = native.execute("PRAGMA database_list").fetchone()[2]
    _source_tables(con, path)
    con.execute("CREATE INDEX IF NOT EXISTS t3_source_runs_task ON t3_source_runs(task_id,run_id)")
    con.execute("CREATE INDEX IF NOT EXISTS t3_source_inputs_session ON t3_source_inputs(session_key,native_id)")
    snapshot = Snapshot(con, path)
    con.execute("CREATE INDEX IF NOT EXISTS t3_purpose_runs_task ON t3_purpose_runs(task_id)")
    con.execute("CREATE INDEX IF NOT EXISTS t3_purpose_inputs_native"
                " ON t3_purpose_inputs(session_key,native_id)")
    con.execute("CREATE INDEX IF NOT EXISTS t3_purpose_inputs_run ON t3_purpose_inputs(run_id)")
    _upgrade_retained_run_keys(con)
    aware = _deliveries(snapshot, native, tables)
    runs = _runs(snapshot, native, tables)
    if runs:
        _capabilities(snapshot, native, tables)
        segments = []
        providers = _providers(snapshot, native, tables, runs, segments)
        attempts = _attempts(snapshot, native, tables, runs, providers)
        _turns(snapshot, native, tables, attempts)
        children = _subagents(snapshot, native, tables, runs, segments)
        _expected_children(snapshot, native, tables, runs)
        boundaries = _segments(snapshot, segments, runs, children)
        _child_turns(snapshot, native, tables, boundaries, providers)
    con.execute("CREATE TABLE IF NOT EXISTS t3_source_aware_threads("
                "source_path TEXT, thread_id TEXT, PRIMARY KEY(source_path,thread_id))")
    con.execute("DELETE FROM t3_source_aware_threads WHERE source_path=?", (path,))
    con.executemany("INSERT INTO t3_source_aware_threads VALUES(?,?)", ((path, t) for t in aware))
    for table in TABLES:
        _materialize(con, table)
    _exclude_disputed_calls(con)
    _exclude_disputed_inputs(con)
    return {r[0] for r in con.execute(
        "SELECT thread_id FROM t3_source_aware_threads UNION SELECT thread_id FROM t3_source_runs"
        " UNION SELECT thread_id FROM t3_source_deliveries")}
