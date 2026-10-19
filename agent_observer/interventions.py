"""Judge interventions: where someone acted on an agent in a main session.

Observer runs no model. It selects interventions deterministically, gives an
outside judge (an LLM) each one in context from the native logs, and
stores the judge's verdicts (agent_observer/verdicts.py). An intervention is one
of:

- prompt: a genuine user message (`sub:<user message native id>`);
- interrupt: an interrupt user message, or a Codex `turn_aborted` event
  (`ev:<session_key>/<family>/<native id>`);
- rejection: a denied permission that a person decided: Claude's
  `user-rejected`, or a denial without a kind on another harness.

Hook and rule denials (Claude `permission-rule`) and auto-mode blocks are
policy, never interventions; they are counted separately.

`created_by` says who acted, from evidence only: `user`, `agent` or
`unknown`. Chromeria turn and prompt origins outrank the session's
entrypoint; nothing is guessed.

Intervention text and context are read on request and never stored; secrets
are redacted on the way out.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path

from . import codex_order, lookup, privacy
from .ingest import iso_ts

KINDS = ("prompt", "interrupt", "rejection")
CREATED_BY = ("user", "agent", "unknown")
# Origins whose corrections count toward the Objective (user decision,
# 2026-10-12): a person's and another agent's. Unknown stays out.
COUNTED = ("user", "agent")
# Session entrypoints that prove who typed: a terminal or desktop app is a
# person, a headless run is an agent.
INTERACTIVE_ENTRYPOINTS = frozenset({"cli", "Codex Desktop", "codex-tui"})
HEADLESS_ENTRYPOINTS = frozenset({"sdk-cli", "codex_exec"})
POLICY_DENIALS = {"permission-rule": "hook_or_rule",
                  "automode-blocked": "automode"}
DENIED = ("denied", "deny")
# Context window: whole turns before and after the intervention, and at most
# DEFAULT_ITEMS agent messages and tool calls per turn. Judges vary these
# to measure accuracy against context size.
DEFAULT_TURNS_BEFORE = 1
DEFAULT_TURNS_AFTER = 1
DEFAULT_ITEMS = 8
DEFAULT_CHARS = 2000
# The person's own words (intervention, previous and next prompt) get a separate,
# larger limit: a judge must never lose them to the context limit.
DEFAULT_TEXT_CHARS = 20000


def iso_to_ts(value):
    return iso_ts(value)


def _iso(ts) -> str | None:
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class _Evidence:
    """Chromeria origin evidence and thread positions, loaded once per call."""

    def __init__(self, con: sqlite3.Connection):
        self.turns = self._pairs(
            con, "SELECT message_id, actor_kind FROM t3_turn_origins")
        self.prompts = {(r[0], r[1]): r[2] for r in self._rows(
            con, "SELECT session_key, native_id, actor_kind FROM t3_native_prompt_origins")}
        self.restricted_origins = {r[0] for r in self._rows(
            con, "SELECT native_id FROM claude_native_kinds WHERE origin_allowed=0")}
        self.native = {r[0]: (r[1], r[2], r[3]) for r in self._rows(
            con, "SELECT native_id,transport_kind,actor_kind,actor_reason"
                 " FROM intervention_prompt_evidence")}
        self.related: dict[str, list[str]] = {}
        self.grouped: dict[str, str] = {}
        self._group_steers(con)
        # Thread position: root (a top-level thread a person opened),
        # launched (top-level, opened by an agent or without evidence), or
        # child. A session seen in several threads takes the least
        # person-like position.
        self.threads: dict[str, str] = {}
        rank = {"root": 0, "launched": 1, "child": 2}
        # A ledger synced before opened_by existed reads every top-level
        # thread as launched (no evidence), never as person-opened.
        opened = "opened_by" if "opened_by" in {
            r[1] for r in self._rows(con, "PRAGMA table_info(t3_threads)")} \
            else "NULL"
        for session, position in self._rows(
                con, "SELECT native_session,"
                     " CASE WHEN thread_id != root_thread_id THEN 'child'"
                     f" WHEN {opened} = 'client' THEN 'root'"
                     " ELSE 'launched' END FROM t3_threads"):
            if rank[position] >= rank.get(self.threads.get(session), -1):
                self.threads[session] = position

    def _group_steers(self, con):
        # The next substantive native input after Claude's interrupt marker
        # must be an explicit T3 steer. Same-source positions establish the
        # link; proximity alone must never turn a real refusal into a steer.
        previous = {}
        for row in con.execute(
                "SELECT native_id,source_id,session_key,ordinal_num,kind,ts"
                " FROM user_messages WHERE kind NOT IN ('scaffolding','command')"
                " ORDER BY source_id,ordinal_num"):
            prior = previous.get(row['source_id'])
            previous[row['source_id']] = row
            if self.native.get(row['native_id'], (None,))[0] != 'steer' or prior is None \
                    or row['source_id'] is None or row['ordinal_num'] is None \
                    or prior['ordinal_num'] is None or prior['kind'] != 'interrupt' \
                    or prior['session_key'] != row['session_key']:
                continue
            canonical = 'sub:' + row['native_id']
            related = ['sub:' + prior['native_id']]
            for event in con.execute(
                    "SELECT session_key,native_id FROM events WHERE source_id=?"
                    " AND session_key=? AND family='permission' AND status IN ('denied','deny')"
                    " AND json_extract(detail_json,'$.denial')='user-rejected'"
                    " AND ? - ts BETWEEN 0 AND 1.0 AND ordinal_num < ?",
                    (row['source_id'], row['session_key'], prior['ts'], prior['ordinal_num'])):
                related.append(f"ev:{event['session_key']}/permission/{event['native_id']}")
            self.related[canonical] = related
            self.grouped.update((identity, canonical) for identity in related)

    @staticmethod
    def _rows(con, sql) -> list:
        try:
            return con.execute(sql).fetchall()
        except sqlite3.OperationalError:
            return []  # a ledger never synced with T3 has no mirror tables

    def _pairs(self, con, sql) -> dict:
        return {r[0]: r[1] for r in self._rows(con, sql)}

    def thread(self, session_native) -> str:
        return self.threads.get(session_native, "none")

    def prompt_actor(self, harness, native_id, session_native, turn_id):
        actor = None
        if harness == "claude":
            actor = self.turns.get(native_id.partition(":")[2])
            # Claude inputs keep the preceding conversational turn even
            # when their own UUID has no origin. It is not author evidence.
            turn_id = None
        actors = {self.prompts.get((f"{harness}:{session_native}", identity))
                  for identity in (native_id, turn_id)} - {None}
        return actor or (next(iter(actors)) if len(actors) == 1 else None)

    def origin_restricted(self, harness, native_id) -> bool:
        """Claude sidechain and scaffolding rows stay uncounted.

        A server origin would otherwise admit the synthetic row as another
        agent's message. Ordinary SDK messages keep origin_allowed and
        still count."""
        return harness == "claude" and native_id in self.restricted_origins


def _created_by(kind: str, actor, entrypoint, thread: str, denial=None) -> str:
    if actor == "client":
        return "user"
    if actor == "server":
        return "agent"
    if entrypoint in HEADLESS_ENTRYPOINTS:
        return "agent"
    if kind != "prompt" and thread in ("child", "launched"):
        # An agent can stop a thread it spawned or launched, or answer its
        # pending request, so a stop or refusal there is no evidence of a
        # person.
        return "unknown"
    if kind == "rejection" and denial == "user-rejected":
        return "user"
    if entrypoint in INTERACTIVE_ENTRYPOINTS:
        return "user"
    if kind != "prompt" and thread == "root":
        # A stop in a thread a person opened is that person's.
        return "user"
    return "unknown"


_SESSION_COLS = ("s.harness, s.native_id AS session_native, s.project_dir,"
                 " s.entrypoint, s.session_key")


def _scope(harness=None, project=None, attrs=None) -> tuple[str, list]:
    where = ["(s.role IS NULL OR s.role != 'subagent')"]
    args: list = []
    if harness:
        where.append("s.harness = ?")
        args.append(harness)
    if project:
        where.append("s.project_dir LIKE ?")
        args.append(f"%{project}%")
    for name, value in sorted((attrs or {}).items()):
        where.append("EXISTS (SELECT 1 FROM session_attributes a WHERE"
                     " a.session_key = s.session_key AND a.name = ?"
                     " AND a.value = ?)")
        args += [name, value]
    return " AND ".join(where), args


def _base(row, intervention_id: str, kind: str, ts, created_by: str, thread: str) -> dict:
    out = {"id": intervention_id, "kind": kind, "created_by": created_by, "thread": thread,
           "ts": ts, "at": _iso(ts), "session": row["session_key"],
           "harness": row["harness"], "project": row["project_dir"]}
    if created_by == 'unknown':
        out['actor_reason'] = 'actor_evidence_missing'
    return out


def _from_submission(row, ev: _Evidence) -> dict | None:
    """An intervention for a genuine prompt, an interrupt, or a message another
    agent sent (a synthetic user message Chromeria records as agent-sent);
    None for other synthetic input such as reminders."""
    transport, sender, reason = ev.native.get(
        row['native_id'], (None, None, 'origin_evidence_not_imported'))
    kind = "interrupt" if row["kind"] == "interrupt" or transport == 'steer' else "prompt"
    thread = ev.thread(row["session_native"])
    identity = row["native_id"]
    if row["harness"] == "claude":
        from .adapters.claude import prompt_native_id
        identity = prompt_native_id(row)
    actor = ev.prompt_actor(row["harness"], identity,
                            row["session_native"], row["turn_id"]) or sender \
        if kind == "prompt" or transport == 'steer' else None
    if transport == 'completion':
        # A complete notice contains no user instruction. Codex can attach
        # its preceding human turn id to this separate system input.
        actor = sender
    if row["kind"] == "synthetic" and (
            actor != "server" or ev.origin_restricted(row["harness"], row["native_id"])):
        return None
    created_by = "unknown" if row["kind"] == "unknown" and not actor \
        else _created_by(kind, actor, row["entrypoint"], thread)
    out = _base(row, f"sub:{row['native_id']}", kind, row["ts"], created_by, thread)
    if created_by == 'unknown':
        out['actor_reason'] = reason
    if out['id'] in ev.related:
        out['related_ids'] = ev.related[out['id']]
    return out


def _denial(row):
    try:
        return (json.loads(row["detail_json"] or "{}") or {}).get("denial")
    except (ValueError, AttributeError):
        return None


def _event_kind(row) -> str | None:
    """interrupt, rejection, policy:<bucket>, or None for no intervention."""
    if row["family"] == "lifecycle":
        return "interrupt"
    denial = _denial(row)
    if denial in POLICY_DENIALS:
        return "policy:" + POLICY_DENIALS[denial]
    if denial == "user-rejected" or (denial is None
                                     and row["harness"] != "claude"):
        return "rejection"
    return None  # `interrupted` is counted from the interrupt itself


def _from_event(row, ev: _Evidence, kind: str) -> dict:
    thread = ev.thread(row["session_native"])
    intervention = _base(row, f"ev:{row['session_key']}/{row['family']}/"
                        f"{row['native_id']}", kind, row["ts"],
                   _created_by(kind, None, row["entrypoint"], thread,
                           _denial(row)), thread)
    if kind == "rejection":
        intervention["tool"] = row["name"]
    return intervention


_SUB_KINDS = "('genuine', 'interrupt', 'synthetic', 'unknown')"
_SUB_SQL = ("SELECT u.native_id, u.alias_id, u.kind, u.ts, u.turn_id, u.ordinal_num,"
            " u.source_id,"
            f" {_SESSION_COLS} FROM user_messages u"
            " JOIN sessions s ON s.session_key = u.session_key")
_EVENT_SQL = ("SELECT e.id, e.family, e.name, e.native_id, e.ts,"
              f" e.detail_json, {_SESSION_COLS} FROM events e"
              " JOIN sessions s ON s.session_key = e.session_key")
_EVENT_WHERE = ("((e.family = 'lifecycle' AND e.name IN ('turn_aborted','turn_ended')"
                " AND e.status IN ('cancelled','interrupted')) OR (e.family = 'permission'"
                f" AND e.status IN ({','.join(repr(d) for d in DENIED)})))")


# Claude writes a rejected tool call twice: the user-rejected denial and,
# within the same second, an interrupt submission. One act, one intervention.
_ECHO_SQL = (
    "SELECT u.native_id FROM user_messages u JOIN events e"
    " ON e.session_key = u.session_key AND e.family = 'permission'"
    f" AND e.status IN ({','.join(repr(d) for d in DENIED)})"
    " AND json_extract(e.detail_json, '$.denial') = 'user-rejected'"
    " AND u.ts - e.ts BETWEEN 0 AND 1.0"
    " WHERE u.kind = 'interrupt'")


def _echoes(con, session_key=None) -> set:
    sql, args = _ECHO_SQL, ()
    if session_key:
        sql, args = sql + " AND u.session_key = ?", (session_key,)
    return {r[0] for r in con.execute(sql, args)}


def _all(con, harness=None, project=None, attrs=None):
    """(interventions, policy denial rows) in scope, over all time."""
    ev = _Evidence(con)
    where, args = _scope(harness, project, attrs)
    found, policy = [], []
    echoes = _echoes(con)
    first = _first_activity(con)
    for row in con.execute(f"{_SUB_SQL} WHERE u.kind IN {_SUB_KINDS}"
                           f" AND {where}", args):
        intervention = None if row["native_id"] in echoes or 'sub:' + row['native_id'] in ev.grouped \
            else _from_submission(row, ev)
        if intervention is not None:
            found.append(_mark_opening(intervention, first, row))
    for row in con.execute(f"{_EVENT_SQL} WHERE {_EVENT_WHERE} AND {where}",
                           args):
        if f"ev:{row['session_key']}/{row['family']}/{row['native_id']}" in ev.grouped:
            continue
        kind = _event_kind(row)
        if kind is None:
            continue
        if kind.startswith("policy:"):
            policy.append((row["ts"], kind[len("policy:"):],
                           row["session_key"]))
        else:
            found.append(_mark_opening(_from_event(row, ev, kind), first, row))
    found.sort(key=lambda m: (m["ts"] or 0, m["id"]))
    return found, policy


def _first_activity(con, session_key=None) -> dict:
    # Keep all events at the earliest timestamp: ordinal_num orders a tie
    # only inside the same source, never across different source files.
    where = "family IN ('assistant_message', 'tool_call')"
    args = ()
    if session_key:
        where += " AND session_key = ?"
        args = (session_key,)
    rows = con.execute(
        "SELECT session_key, ts, source_id, ordinal_num FROM ("
        " SELECT session_key, ts, source_id, ordinal_num,"
        " RANK() OVER (PARTITION BY session_key ORDER BY ts) AS position"
        f" FROM events WHERE {where}) WHERE position = 1", args)
    first = {}
    for row in rows:
        first.setdefault(row["session_key"], []).append(row)
    return first


def _mark_opening(intervention: dict, first: dict, prompt) -> dict:
    """Only prompts proven to precede all agent activity are opening.

    Missing activity or incomparable ordering leaves a prompt unknown.
    Interrupts and rejections are never opening prompts.
    """
    opening = False
    if intervention["kind"] == "prompt":
        comparisons = []
        for activity in first.get(intervention["session"], []):
            if prompt["ts"] is None or activity["ts"] is None:
                comparisons.append(None)
            elif prompt["ts"] != activity["ts"]:
                comparisons.append(prompt["ts"] < activity["ts"])
            elif (prompt["source_id"] is not None
                  and prompt["source_id"] == activity["source_id"]
                  and prompt["ordinal_num"] is not None
                  and activity["ordinal_num"] is not None):
                comparisons.append(prompt["ordinal_num"] < activity["ordinal_num"])
            else:
                comparisons.append(None)
        opening = False if False in comparisons else (
            True if comparisons and None not in comparisons else None)
    intervention["opening"] = opening
    return intervention


def _in_period(ts, since, until) -> bool:
    ts = ts or 0
    return (since is None or ts >= since) and (until is None or ts < until)


def judged_ids(con, behavior: str, judge=None) -> set:
    sql, args = "SELECT DISTINCT intervention_id FROM verdicts WHERE behavior=?", \
        [behavior]
    if judge:
        sql += " AND judge=?"
        args.append(judge)
    ev = _Evidence(con)
    return {ev.grouped.get(r[0], r[0]) for r in con.execute(sql, args)}


def select(con: sqlite3.Connection, since=None, until=None, harness=None,
           project=None, created_by=COUNTED, kinds=None, unjudged=None,
           attrs=None, limit=None, judge=None) -> dict:
    """Interventions in scope. Counts cover every created_by value and kind in scope, so
    interventions the filters leave out stay visible as numbers."""
    found, policy = _all(con, harness, project, attrs)
    found = [m for m in found if _in_period(m["ts"], since, until)]
    by_created_by = {o: 0 for o in CREATED_BY}
    by_kind = {k: 0 for k in KINDS}
    for m in found:
        by_created_by[m["created_by"]] += 1
        by_kind[m["kind"]] += 1
    denials = {bucket: 0 for bucket in POLICY_DENIALS.values()}
    for ts, bucket, _ in policy:
        if _in_period(ts, since, until):
            denials[bucket] += 1
    chosen = [m for m in found
              if (not created_by or m["created_by"] in created_by)
              and (not kinds or m["kind"] in kinds)]
    if unjudged:
        done = judged_ids(con, unjudged, judge)
        chosen = [m for m in chosen if m["id"] not in done]
    if limit:
        chosen = chosen[:limit]
    return {"interventions": chosen,
            "counts": {"by_created_by": by_created_by, "by_kind": by_kind,
                       "selected": len(chosen)},
            "policy_denials": denials}


def evidence(con: sqlite3.Connection) -> _Evidence:
    """Origin evidence to share across many resolve() calls."""
    return _Evidence(con)


def resolve(con: sqlite3.Connection, intervention_id, ev=None) -> dict | None:
    """The intervention named by an id, or None when it names no intervention."""
    if not isinstance(intervention_id, str):
        return None
    ev = ev or _Evidence(con)
    intervention_id = ev.grouped.get(intervention_id, intervention_id)
    if intervention_id.startswith("sub:"):
        row = con.execute(f"{_SUB_SQL} WHERE u.native_id = ? AND u.kind IN"
                          f" {_SUB_KINDS}", (intervention_id[4:],)).fetchone()
        if row is None or row["native_id"] in _echoes(con, row["session_key"]):
            return None
        intervention = _from_submission(row, ev)
        return None if intervention is None else _mark_opening(
            intervention, _first_activity(con, row["session_key"]), row)
    if intervention_id.startswith("ev:"):
        session, _, rest = intervention_id[3:].partition("/")
        family, _, native = rest.partition("/")
        if not (session and family and native):
            return None
        row = con.execute(f"{_EVENT_SQL} WHERE e.session_key = ? AND"
                          f" e.family = ? AND e.native_id = ? AND"
                          f" {_EVENT_WHERE}",
                          (session, family, native)).fetchone()
        if row is None:
            return None
        kind = _event_kind(row)
        if kind is None or kind.startswith("policy:"):
            return None
        return _mark_opening(_from_event(row, ev, kind),
                             _first_activity(con, session), row)
    return None


# ---- context -------------------------------------------------------------

def clip(text, chars: int):
    """Keep the head and tail of a long field and say what was cut."""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str)
    if chars <= 0 or len(text) <= chars:
        return text
    half = chars // 2
    return (f"{text[:half]}…[{len(text) - 2 * half} chars cut]…"
            f"{text[len(text) - half:]}")


def _claude_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif isinstance(block, dict) and block.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def _codex_identity_keys(native_id) -> list:
    """Exact native id, then its filename and complete-record position."""
    if not isinstance(native_id, str) or not native_id:
        return []
    keys = [native_id]
    bare = native_id[6:] if native_id.startswith("codex:") else native_id
    if bare != native_id:
        keys.append(bare)
    at = bare.rfind(":file:")
    if at >= 0:
        keys.append(bare[at + 1:])
    elif bare.startswith("file:"):
        keys.append(bare)
    return keys


def _codex_submission_record(index, ordinal, native_id):
    """The user record named by native identity. Ordinals are legacy only.

    Id-less rollouts reuse metadata ordinals, so index.records[ordinal]
    can name a different input than the one stored under that ordinal.
    """
    for key in _codex_identity_keys(native_id):
        found = index.records.get(key)
        if isinstance(found, dict):
            return found
    found = index.records.get(ordinal)
    return found if isinstance(found, dict) else None


def _record_text(harness: str, path: str, index, ordinal, native_id):
    """The full user text of one submission record, or None."""
    if harness == "claude":
        rec = index.records.get(ordinal)
        if isinstance(rec, dict) and rec.get("type") == "queue-operation" \
                and rec.get("operation") == "enqueue":
            return rec.get("content") if isinstance(rec.get("content"), str) else None
        message = rec.get("message") if isinstance(rec, dict) else None
        return _claude_text(message.get("content")) \
            if isinstance(message, dict) else None
    if harness == "codex":
        rec = _codex_submission_record(index, ordinal, native_id)
        if not isinstance(rec, dict):
            return None
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            return None
        if isinstance(payload.get("message"), str):
            return payload["message"]
        text = lookup._codex_text(payload.get("content"))
        if not text and isinstance(payload.get("item"), dict):
            text = lookup._codex_text(payload["item"].get("content"))
        return text or None
    if harness == "grok":
        chunks, key = [], ordinal
        while True:
            rec = index.records.get(key)
            update = ((rec or {}).get("params") or {}).get("update") \
                if isinstance(rec, dict) else None
            if not isinstance(update, dict) \
                    or update.get("sessionUpdate") != "user_message_chunk":
                break
            content = update.get("content") or {}
            chunks.append(str(content.get("text", ""))
                          if isinstance(content, dict) else "")
            key += 1
        return "".join(chunks) or None
    if harness == "opencode":
        db_path = path.partition("#")[0]
        message = native_id.partition(":")[2]
        native = sqlite3.connect(Path(db_path).absolute().as_uri() + "?mode=ro",
                                 uri=True)
        try:
            rows = native.execute(
                "SELECT data FROM part WHERE message_id=?"
                " ORDER BY time_created, id", (message,)).fetchall()
        finally:
            native.close()
        texts = []
        for (raw,) in rows:
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict) and data.get("type") == "text":
                texts.append(str(data.get("text", "")))
        return "\n".join(texts) or None
    return None


class _Sources:
    """Native source state and parsed indexes, cached per call."""

    def __init__(self, con):
        self.con = con
        self.names = lookup._NameIndex()
        self.cache: dict[int, tuple[str, object]] = {}

    def get(self, source_id):
        if source_id in self.cache:
            return self.cache[source_id]
        row = self.con.execute(
            "SELECT id, harness, path, size_bytes, read_offset, tail_sha256,"
            " ino FROM sources WHERE id=?", (source_id,)).fetchone()
        state, index = "source_missing", None
        source = row
        if row is not None:
            resolved = lookup._with_companion(
                self.con, row, lookup.resolve_source(row, self.names))
            state = resolved["status"]
            read_path = resolved["path"] or row["path"]
            if state == "ok" and row["harness"] in lookup._INDEXERS:
                try:
                    index = lookup._INDEXERS[row["harness"]](read_path)
                except (OSError, sqlite3.Error):
                    state = "source_missing"
            source = dict(row)
            source["path"] = read_path
        self.cache[source_id] = (state, (source, index))
        return self.cache[source_id]


def _submission_text(sources: _Sources, sub) -> tuple[str, str | None]:
    state, (source, index) = sources.get(sub["source_id"])
    if state != "ok":
        return state, None
    try:
        text = _record_text(source["harness"], source["path"], index,
                            sub["ordinal_num"], sub["native_id"])
    except (OSError, sqlite3.Error):
        return "source_missing", None
    if text is None:
        return "not_found", None
    return "ok", privacy.redact_secrets(text)


_TIMELINE_FAMILIES = ("assistant_message", "tool_call", "permission",
                      "lifecycle")


def _timeline(con, session_key: str, ev: _Evidence) -> list:
    """Ordered prompts (from a person or another agent), interrupts and
    agent activity of one session. Every intervention submission is a turn
    boundary."""
    items = []
    echoes = _echoes(con, session_key)
    for row in con.execute(
            f"{_SUB_SQL} WHERE u.session_key = ? AND u.kind IN {_SUB_KINDS}",
            (session_key,)):
        intervention = None if row["native_id"] in echoes or 'sub:' + row['native_id'] in ev.grouped \
            else _from_submission(row, ev)
        if intervention is None:
            continue
        items.append({"key": intervention["id"], "ts": row["ts"],
                      "ordinal": row["ordinal_num"], "boundary": True,
                      "row": row, "what": "submission", "intervention": intervention})
    for row in con.execute(
            "SELECT e.id, e.family, e.name, e.native_id, e.ts, e.ordinal_num,"
            f" e.status, e.detail_json, e.target, {_SESSION_COLS} FROM events e"
            " JOIN sessions s ON s.session_key = e.session_key"
            " WHERE e.session_key=? AND e.family"
            f" IN ({','.join('?' * len(_TIMELINE_FAMILIES))})",
            (session_key, *_TIMELINE_FAMILIES)):
        family = row["family"]
        if family == "lifecycle" and not (row["name"] in ("turn_aborted", "turn_ended")
                                          and row["status"] in ("cancelled", "interrupted")):
            continue
        if family == "permission" and row["status"] not in DENIED:
            continue
        items.append({"key": f"ev:{session_key}/{family}/{row['native_id']}",
                      "ts": row["ts"], "ordinal": row["ordinal_num"],
                      "boundary": family == "lifecycle", "row": row,
                      "what": family,
                      "intervention": _from_event(row, ev, "interrupt")
                      if family == "lifecycle" else None})
    wanted = set()
    for item in items:
        row = item["row"]
        if item["what"] == "submission":
            wanted.add(("user", row["native_id"]))
        else:
            wanted.add(("event", str(row["id"])))
    state, order, sequences = codex_order.resolve(con, session_key, wanted)
    if state == "known":
        index = {}
        for place, group in enumerate(order):
            for ident in group:
                index[ident] = place

        def sort_key(item):
            row = item["row"]
            ident = (("user", row["native_id"]) if item["what"] == "submission"
                     else ("event", str(row["id"])))
            return (index.get(ident, len(index)), item["key"])

        items.sort(key=sort_key)
    elif state == "absent":
        items.sort(key=lambda i: (i["ts"] or 0, i["ordinal"] or 0))
    return items, state, sequences


def _agent_item(item, content: dict, chars: int) -> dict:
    row = item["row"]
    out = {"type": None, "at": _iso(row["ts"])}
    if item["what"] == "assistant_message":
        out["type"] = "assistant"
        if content.get("status") == "ok":
            out["text"] = clip(content.get("text"), chars)
        else:
            out["status"] = content.get("status")
            out["text"] = clip(_excerpt(row), chars)
    elif item["what"] == "tool_call":
        out.update({"type": "tool", "tool": row["name"]})
        if content.get("status") == "ok":
            value = content.get("input")
            command = value.get("command") if isinstance(value, dict) else None
            out["input"] = clip(command if isinstance(command, str)
                                else value, chars)
            out["output"] = clip(content.get("result"), chars)
        else:
            out["status"] = content.get("status")
            out["input"] = clip(row["target"] or "", chars)
    else:  # a denied permission inside the turn
        out.update({"type": "denied", "tool": row["name"],
                    "denial": _denial(row)})
    return out


def _excerpt(row) -> str:
    try:
        return (json.loads(row["detail_json"] or "{}") or {}).get("excerpt") \
            or ""
    except (ValueError, AttributeError):
        return ""


def _prompt(sources, item, chars) -> dict:
    """A turn boundary: a prompt (with its text and who sent it) or an
    interrupt."""
    intervention = item["intervention"]
    if item["what"] != "submission":
        return {"id": item["key"], "kind": "interrupt",
                "created_by": intervention["created_by"] if intervention else None,
                "at": _iso(item["row"]["ts"])}
    status, text = _submission_text(sources, item["row"])
    out = {"id": item["key"], "kind": intervention["kind"],
           "created_by": intervention["created_by"], "at": _iso(item["row"]["ts"])}
    if status == "ok":
        out["text"] = clip(text, chars)
    else:
        out["status"] = status
    return out


def _turns(line, at: int, turns_before: int, turns_after: int, items: int):
    """Index ranges of whole turns around position `at`.

    A turn runs from one boundary to the next. Before turns are
    (opening boundary or None at session start, kept activity, omitted
    count); after turns are (kept activity, omitted count, closing
    boundary or None at session end), the first being the intervention's own
    turn. `items` keeps the last activity of a before turn and the first
    of an after turn."""
    bounds = [i for i, item in enumerate(line) if item["boundary"]]
    earlier = [i for i in bounds if i < at]
    later = [i for i in bounds if i > at]
    before = []
    starts = ([None] if (not earlier or earlier[0] > 0) else []) + earlier
    for n, opening in enumerate(starts):
        first = 0 if opening is None else opening + 1
        stop = starts[n + 1] if n + 1 < len(starts) else at
        span = [i for i in range(first, stop) if not line[i]["boundary"]]
        if opening is None and not span:
            continue
        kept = span[-items:] if items else []
        before.append((opening, kept, len(span) - len(kept)))
    before = before[-turns_before:] if turns_before else []
    after = []
    edge = at
    for closing in later[:turns_after] if turns_after else []:
        span = list(range(edge + 1, closing))
        kept = span[:items] if items else []
        after.append((kept, len(span) - len(kept), closing))
        edge = closing
    if turns_after and len(after) < turns_after:
        span = list(range(edge + 1, len(line)))
        if span or not after:
            kept = span[:items] if items else []
            after.append((kept, len(span) - len(kept), None))
    return before, after


def show(con: sqlite3.Connection, intervention_ids: list,
         turns_before=DEFAULT_TURNS_BEFORE, turns_after=DEFAULT_TURNS_AFTER,
         items=DEFAULT_ITEMS, chars=DEFAULT_CHARS,
         text_chars=DEFAULT_TEXT_CHARS) -> list[dict]:
    """Each intervention in context: its full text, and whole turns before and
    after it (each a prompt from a person or agent plus the agent's
    messages and tool calls). The window is a parameter, echoed back, so a
    judge can measure accuracy against context size. Status per intervention is
    ok, source_missing, source_changed, not_found, no_such_intervention, or
    order_unknown when no source sequence covers the turn."""
    window = {"turns_before": turns_before, "turns_after": turns_after,
              "items": items, "chars": chars, "text_chars": text_chars}
    if min(window.values()) < 0:
        raise ValueError("window values must be 0 or more")
    sources = _Sources(con)
    ev = _Evidence(con)
    timelines: dict[str, list] = {}
    out = []
    for intervention_id in intervention_ids:
        intervention = resolve(con, intervention_id, ev)
        if intervention is None:
            out.append({"id": intervention_id, "status": "no_such_intervention"})
            continue
        intervention_id = intervention['id']
        session = intervention["session"]
        if session not in timelines:
            timelines[session] = _timeline(con, session, ev)
        line, order_state, sequences = timelines[session]
        at = next(i for i, item in enumerate(line) if item["key"] == intervention_id)
        status, text = "ok", None
        if intervention_id.startswith("sub:"):
            status, text = _submission_text(sources, line[at]["row"])
        else:
            row = line[at]["row"]
            source = con.execute("SELECT source_id FROM events WHERE id=?",
                                 (row["id"],)).fetchone()
            status = sources.get(source["source_id"])[0] if source else \
                "source_missing"
        item = dict(intervention)
        item["status"] = status
        item["window"] = window
        if order_state in ("partial", "conflict"):
            if status == "ok":
                item["status"] = "order_unknown"
                if text is not None:
                    item["text"] = clip(text, text_chars)
            item["source_sequences"] = [
                [[list(ident) for ident in sorted(group)] for group in sequence]
                for sequence in sequences]
            out.append(item)
            continue
        if status != "ok":
            out.append(item)
            continue
        if text is not None:
            item["text"] = clip(text, text_chars)
        before, after = _turns(line, at, turns_before, turns_after, items)
        spans = [i for _, span, _ in before for i in span] + \
            [i for span, _, _ in after for i in span]
        ids = [line[i]["row"]["id"] for i in spans
               if line[i]["what"] in ("assistant_message", "tool_call")]
        contents = {c["id"]: c for c in lookup.show(con, ids)} if ids else {}

        def activity(span):
            return [_agent_item(line[i], contents.get(line[i]["row"]["id"], {}),
                                chars) for i in span]
        item["before"] = [
            {"prompt": None if opening is None
             else _prompt(sources, line[opening], text_chars),
             "activity": activity(span), "omitted": omitted}
            for opening, span, omitted in before]
        item["after"] = [
            {"activity": activity(span), "omitted": omitted,
             "prompt": None if closing is None
             else _prompt(sources, line[closing], text_chars)}
            for span, omitted, closing in after]
        out.append(item)
    return out


def render(items: list[dict]) -> str:
    parts = []
    for item in items:
        lines = [f"== {item['id']} [{item['status']}]"]
        if item["status"] == "no_such_intervention":
            parts.append(lines[0])
            continue
        lines.append(f"{item['kind']} by {item['created_by']} in {item['session']}"
                     f" ({item['harness']}, thread {item['thread']}) at"
                     f" {item['at']} opening={json.dumps(item['opening'])}")
        if item.get('actor_reason'):
            lines.append(f"actor_reason={item['actor_reason']}")
        for turn in item.get("before", []):
            if turn["prompt"]:
                lines.append(_render_prompt(turn["prompt"]))
            lines += [_render_entry(e) for e in turn["activity"]]
        if "text" in item:
            lines += ["--- intervention", item["text"]]
        for turn in item.get("after", []):
            lines += [_render_entry(e) for e in turn["activity"]]
            if turn["prompt"]:
                lines.append(_render_prompt(turn["prompt"]))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _render_prompt(prompt: dict) -> str:
    return (f"--- {prompt['kind']} by {prompt.get('created_by') or 'unknown'}"
            f"\n{prompt.get('text') or ''}").rstrip()


def _render_entry(entry: dict) -> str:
    if entry["type"] == "assistant":
        return f"--- assistant\n{entry.get('text') or ''}"
    if entry["type"] == "tool":
        return (f"--- tool {entry['tool']}\n{entry.get('input') or ''}"
                f"\n>>> {entry.get('output') or ''}")
    return f"--- denied {entry['tool']} ({entry.get('denial') or 'no kind'})"
