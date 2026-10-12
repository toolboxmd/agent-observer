"""Judge moments: where a person acted in a main session, in context.

Observer runs no model. It selects moments deterministically, gives an
outside judge (an LLM) each one in context from the native logs, and
stores the judge's verdicts (agent_observer/verdicts.py). A moment is one
of:

- prompt: a genuine submission (`sub:<submission native id>`);
- interrupt: an interrupt submission, or a Codex `turn_aborted` event
  (`ev:<session_key>/<family>/<native id>`);
- rejection: a denied permission that a person decided: Claude's
  `user-rejected`, or a denial without a kind on another harness.

Hook and rule denials (Claude `permission-rule`) and auto-mode blocks are
policy, never moments; they are counted separately.

`origin` says who acted, from evidence only: `human`, `agent` or
`unknown`. Chromeria turn and prompt origins outrank the session's
entrypoint; nothing is guessed.

Moment text and context are read on request and never stored; secrets
are redacted on the way out.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path

from . import lookup, privacy
from .ingest import iso_ts

KINDS = ("prompt", "interrupt", "rejection")
ORIGINS = ("human", "agent", "unknown")
# Origins whose corrections count toward the Objective (user decision,
# 2026-10-12): a person's and another agent's. Unknown stays out.
COUNTED_ORIGINS = ("human", "agent")
# Session entrypoints that prove who typed: a terminal or desktop app is a
# person, a headless run is an agent.
INTERACTIVE_ENTRYPOINTS = frozenset({"cli", "Codex Desktop", "codex-tui"})
HEADLESS_ENTRYPOINTS = frozenset({"sdk-cli", "codex_exec"})
POLICY_DENIALS = {"permission-rule": "hook_or_rule",
                  "automode-blocked": "automode"}
DENIED = ("denied", "deny")
# Context window: whole turns before and after the moment, and at most
# DEFAULT_ITEMS agent messages and tool calls per turn. Judges vary these
# to measure accuracy against context size.
DEFAULT_TURNS_BEFORE = 1
DEFAULT_TURNS_AFTER = 1
DEFAULT_ITEMS = 8
DEFAULT_CHARS = 2000
# The person's own words (moment, previous and next prompt) get a separate,
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
    """Chromeria origins and thread positions, loaded once per call."""

    def __init__(self, con: sqlite3.Connection):
        self.turns = self._pairs(
            con, "SELECT message_id, actor_kind FROM t3_turn_origins")
        self.prompts = self._pairs(
            con, "SELECT native_session || '/' || text_hash, actor_kind"
                 " FROM t3_prompt_origins")
        # Thread position: root (a top-level thread a person opened),
        # launched (top-level, opened by an agent or without evidence), or
        # child. A session seen in several threads takes the least
        # person-like position.
        self.threads: dict[str, str] = {}
        rank = {"root": 0, "launched": 1, "child": 2}
        for session, position in self._rows(
                con, "SELECT native_session,"
                     " CASE WHEN thread_id != root_thread_id THEN 'child'"
                     " WHEN opened_by = 'client' THEN 'root'"
                     " ELSE 'launched' END FROM t3_threads"):
            if rank[position] >= rank.get(self.threads.get(session), -1):
                self.threads[session] = position

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

    def prompt_actor(self, harness, native_id, session_native, digest):
        actor = None
        if harness == "claude":
            actor = self.turns.get(native_id.partition(":")[2])
        return actor or self.prompts.get(f"{session_native}/{digest}")


def _origin(kind: str, actor, entrypoint, thread: str, denial=None) -> str:
    if actor == "client":
        return "human"
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
        return "human"
    if entrypoint in INTERACTIVE_ENTRYPOINTS:
        return "human"
    if kind != "prompt" and thread == "root":
        # A stop in a thread a person opened is that person's.
        return "human"
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


def _base(row, moment_id: str, kind: str, ts, origin: str, thread: str) -> dict:
    return {"id": moment_id, "kind": kind, "origin": origin, "thread": thread,
            "ts": ts, "at": _iso(ts), "session": row["session_key"],
            "harness": row["harness"], "project": row["project_dir"]}


def _from_submission(row, ev: _Evidence) -> dict | None:
    """A moment for a genuine prompt, an interrupt, or a message another
    agent sent (a synthetic submission Chromeria records as agent-sent);
    None for other synthetic input such as reminders."""
    kind = "interrupt" if row["kind"] == "interrupt" else "prompt"
    thread = ev.thread(row["session_native"])
    actor = ev.prompt_actor(row["harness"], row["native_id"],
                            row["session_native"], row["text_hash"]) \
        if kind == "prompt" else None
    if row["kind"] == "synthetic" and actor != "server":
        return None
    return _base(row, f"sub:{row['native_id']}", kind, row["ts"],
                 _origin(kind, actor, row["entrypoint"], thread), thread)


def _denial(row):
    try:
        return (json.loads(row["detail_json"] or "{}") or {}).get("denial")
    except (ValueError, AttributeError):
        return None


def _event_kind(row) -> str | None:
    """interrupt, rejection, policy:<bucket>, or None for no moment."""
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
    moment = _base(row, f"ev:{row['session_key']}/{row['family']}/"
                        f"{row['native_id']}", kind, row["ts"],
                   _origin(kind, None, row["entrypoint"], thread,
                           _denial(row)), thread)
    if kind == "rejection":
        moment["tool"] = row["name"]
    return moment


_SUB_KINDS = "('genuine', 'interrupt', 'synthetic')"
_SUB_SQL = ("SELECT u.native_id, u.kind, u.ts, u.text_hash, u.ordinal_num,"
            " u.source_id,"
            f" {_SESSION_COLS} FROM submissions u"
            " JOIN sessions s ON s.session_key = u.session_key")
_EVENT_SQL = ("SELECT e.id, e.family, e.name, e.native_id, e.ts,"
              f" e.detail_json, {_SESSION_COLS} FROM events e"
              " JOIN sessions s ON s.session_key = e.session_key")
_EVENT_WHERE = ("((e.family = 'lifecycle' AND e.name = 'turn_aborted'"
                " AND e.status = 'cancelled') OR (e.family = 'permission'"
                f" AND e.status IN ({','.join(repr(d) for d in DENIED)})))")


# Claude writes a rejected tool call twice: the user-rejected denial and,
# within the same second, an interrupt submission. One act, one moment.
_ECHO_SQL = (
    "SELECT u.native_id FROM submissions u JOIN events e"
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
    """(moments, policy denial rows) in scope, over all time."""
    ev = _Evidence(con)
    where, args = _scope(harness, project, attrs)
    found, policy = [], []
    echoes = _echoes(con)
    for row in con.execute(f"{_SUB_SQL} WHERE u.kind IN {_SUB_KINDS}"
                           f" AND {where}", args):
        moment = None if row["native_id"] in echoes \
            else _from_submission(row, ev)
        if moment is not None:
            found.append(moment)
    for row in con.execute(f"{_EVENT_SQL} WHERE {_EVENT_WHERE} AND {where}",
                           args):
        kind = _event_kind(row)
        if kind is None:
            continue
        if kind.startswith("policy:"):
            policy.append((row["ts"], kind[len("policy:"):]))
        else:
            found.append(_from_event(row, ev, kind))
    found.sort(key=lambda m: (m["ts"] or 0, m["id"]))
    return found, policy


def _in_period(ts, since, until) -> bool:
    ts = ts or 0
    return (since is None or ts >= since) and (until is None or ts < until)


def judged_ids(con, behavior: str, judge=None) -> set:
    sql, args = "SELECT DISTINCT moment_id FROM verdicts WHERE behavior=?", \
        [behavior]
    if judge:
        sql += " AND judge=?"
        args.append(judge)
    return {r[0] for r in con.execute(sql, args)}


def select(con: sqlite3.Connection, since=None, until=None, harness=None,
           project=None, origins=COUNTED_ORIGINS, kinds=None, unjudged=None,
           attrs=None, limit=None, judge=None) -> dict:
    """Moments in scope. Counts cover every origin and kind in scope, so
    moments the filters leave out stay visible as numbers."""
    found, policy = _all(con, harness, project, attrs)
    found = [m for m in found if _in_period(m["ts"], since, until)]
    by_origin = {o: 0 for o in ORIGINS}
    by_kind = {k: 0 for k in KINDS}
    for m in found:
        by_origin[m["origin"]] += 1
        by_kind[m["kind"]] += 1
    denials = {bucket: 0 for bucket in POLICY_DENIALS.values()}
    for ts, bucket in policy:
        if _in_period(ts, since, until):
            denials[bucket] += 1
    chosen = [m for m in found
              if (not origins or m["origin"] in origins)
              and (not kinds or m["kind"] in kinds)]
    if unjudged:
        done = judged_ids(con, unjudged, judge)
        chosen = [m for m in chosen if m["id"] not in done]
    if limit:
        chosen = chosen[:limit]
    return {"moments": chosen,
            "counts": {"by_origin": by_origin, "by_kind": by_kind,
                       "selected": len(chosen)},
            "policy_denials": denials}


def evidence(con: sqlite3.Connection) -> _Evidence:
    """Origin evidence to share across many resolve() calls."""
    return _Evidence(con)


def resolve(con: sqlite3.Connection, moment_id, ev=None) -> dict | None:
    """The moment named by an id, or None when it names no moment."""
    if not isinstance(moment_id, str):
        return None
    ev = ev or _Evidence(con)
    if moment_id.startswith("sub:"):
        row = con.execute(f"{_SUB_SQL} WHERE u.native_id = ? AND u.kind IN"
                          f" {_SUB_KINDS}", (moment_id[4:],)).fetchone()
        if row is None or row["native_id"] in _echoes(con, row["session_key"]):
            return None
        return _from_submission(row, ev)
    if moment_id.startswith("ev:"):
        session, _, rest = moment_id[3:].partition("/")
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
        return _from_event(row, ev, kind)
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


def _record_text(harness: str, path: str, index, ordinal, native_id):
    """The full user text of one submission record, or None."""
    if harness == "claude":
        rec = index.records.get(ordinal)
        message = rec.get("message") if isinstance(rec, dict) else None
        return _claude_text(message.get("content")) \
            if isinstance(message, dict) else None
    if harness == "codex":
        rec = index.records.get(ordinal)
        payload = rec.get("payload") if isinstance(rec, dict) else None
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
        self.cache: dict[int, tuple[str, object]] = {}

    def get(self, source_id):
        if source_id in self.cache:
            return self.cache[source_id]
        row = self.con.execute(
            "SELECT id, harness, path, size_bytes, read_offset, tail_sha256,"
            " ino FROM sources WHERE id=?", (source_id,)).fetchone()
        state, index = "source_missing", None
        if row is not None:
            state = lookup._source_state(row)
            if state == "ok" and row["harness"] == "grok":
                state = lookup._grok_updates_state(self.con, row["path"])
            if state == "ok" and row["harness"] in lookup._INDEXERS:
                try:
                    index = lookup._INDEXERS[row["harness"]](row["path"])
                except (OSError, sqlite3.Error):
                    state = "source_missing"
        self.cache[source_id] = (state, (row, index))
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
    agent activity of one session. Every moment submission is a turn
    boundary."""
    items = []
    echoes = _echoes(con, session_key)
    for row in con.execute(
            f"{_SUB_SQL} WHERE u.session_key = ? AND u.kind IN {_SUB_KINDS}",
            (session_key,)):
        moment = None if row["native_id"] in echoes \
            else _from_submission(row, ev)
        if moment is None:
            continue
        items.append({"key": moment["id"], "ts": row["ts"],
                      "ordinal": row["ordinal_num"], "boundary": True,
                      "row": row, "what": "submission", "moment": moment})
    for row in con.execute(
            "SELECT id, family, name, native_id, ts, ordinal_num, status,"
            " detail_json, target FROM events WHERE session_key=? AND family"
            f" IN ({','.join('?' * len(_TIMELINE_FAMILIES))})",
            (session_key, *_TIMELINE_FAMILIES)):
        family = row["family"]
        if family == "lifecycle" and not (row["name"] == "turn_aborted"
                                          and row["status"] == "cancelled"):
            continue
        if family == "permission" and row["status"] not in DENIED:
            continue
        items.append({"key": f"ev:{session_key}/{family}/{row['native_id']}",
                      "ts": row["ts"], "ordinal": row["ordinal_num"],
                      "boundary": family == "lifecycle", "row": row,
                      "what": family, "moment": None})
    items.sort(key=lambda i: (i["ts"] or 0, i["ordinal"] or 0))
    return items


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
    moment = item["moment"]
    if item["what"] != "submission":
        return {"id": item["key"], "kind": "interrupt",
                "origin": moment["origin"] if moment else None,
                "at": _iso(item["row"]["ts"])}
    status, text = _submission_text(sources, item["row"])
    out = {"id": item["key"], "kind": moment["kind"],
           "origin": moment["origin"], "at": _iso(item["row"]["ts"])}
    if status == "ok":
        out["text"] = clip(text, chars)
    else:
        out["status"] = status
    return out


def _turns(line, at: int, turns_before: int, turns_after: int, items: int):
    """Index ranges of whole turns around position `at`.

    A turn runs from one boundary to the next. Before turns are
    (opening boundary or None at session start, activity); after turns
    are (activity, closing boundary or None at session end), the first
    being the moment's own turn."""
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
        before.append((opening, span[-items:] if items else []))
    before = before[-turns_before:] if turns_before else []
    after = []
    edge = at
    for closing in later[:turns_after] if turns_after else []:
        span = list(range(edge + 1, closing))
        after.append((span[:items] if items else [], closing))
        edge = closing
    if turns_after and len(after) < turns_after:
        span = list(range(edge + 1, len(line)))
        if span or not after:
            after.append((span[:items] if items else [], None))
    return before, after


def show(con: sqlite3.Connection, moment_ids: list,
         turns_before=DEFAULT_TURNS_BEFORE, turns_after=DEFAULT_TURNS_AFTER,
         items=DEFAULT_ITEMS, chars=DEFAULT_CHARS,
         text_chars=DEFAULT_TEXT_CHARS) -> list[dict]:
    """Each moment in context: its full text, and whole turns before and
    after it (each a prompt from a person or agent plus the agent's
    messages and tool calls). The window is a parameter, echoed back, so a
    judge can measure accuracy against context size. Status per moment is
    ok, source_missing, source_changed, not_found or no_such_moment."""
    window = {"turns_before": turns_before, "turns_after": turns_after,
              "items": items, "chars": chars, "text_chars": text_chars}
    sources = _Sources(con)
    ev = _Evidence(con)
    timelines: dict[str, list] = {}
    out = []
    for moment_id in moment_ids:
        moment = resolve(con, moment_id, ev)
        if moment is None:
            out.append({"id": moment_id, "status": "no_such_moment"})
            continue
        session = moment["session"]
        if session not in timelines:
            timelines[session] = _timeline(con, session, ev)
        line = timelines[session]
        at = next(i for i, item in enumerate(line) if item["key"] == moment_id)
        status, text = "ok", None
        if moment_id.startswith("sub:"):
            status, text = _submission_text(sources, line[at]["row"])
        else:
            row = line[at]["row"]
            source = con.execute("SELECT source_id FROM events WHERE id=?",
                                 (row["id"],)).fetchone()
            status = sources.get(source["source_id"])[0] if source else \
                "source_missing"
        item = dict(moment)
        item["status"] = status
        item["window"] = window
        if status != "ok":
            out.append(item)
            continue
        if text is not None:
            item["text"] = clip(text, text_chars)
        before, after = _turns(line, at, turns_before, turns_after, items)
        spans = [i for _, span in before for i in span] + \
            [i for span, _ in after for i in span]
        ids = [line[i]["row"]["id"] for i in spans
               if line[i]["what"] in ("assistant_message", "tool_call")]
        contents = {c["id"]: c for c in lookup.show(con, ids)} if ids else {}

        def activity(span):
            return [_agent_item(line[i], contents.get(line[i]["row"]["id"], {}),
                                chars) for i in span]
        item["before"] = [
            {"prompt": None if opening is None
             else _prompt(sources, line[opening], text_chars),
             "activity": activity(span)} for opening, span in before]
        item["after"] = [
            {"activity": activity(span),
             "prompt": None if closing is None
             else _prompt(sources, line[closing], text_chars)}
            for span, closing in after]
        out.append(item)
    return out


def render(items: list[dict]) -> str:
    parts = []
    for item in items:
        lines = [f"== {item['id']} [{item['status']}]"]
        if item["status"] == "no_such_moment":
            parts.append(lines[0])
            continue
        lines.append(f"{item['kind']} by {item['origin']} in {item['session']}"
                     f" ({item['harness']}, thread {item['thread']}) at"
                     f" {item['at']}")
        for turn in item.get("before", []):
            if turn["prompt"]:
                lines.append(_render_prompt(turn["prompt"]))
            lines += [_render_entry(e) for e in turn["activity"]]
        if "text" in item:
            lines += ["--- moment", item["text"]]
        for turn in item.get("after", []):
            lines += [_render_entry(e) for e in turn["activity"]]
            if turn["prompt"]:
                lines.append(_render_prompt(turn["prompt"]))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _render_prompt(prompt: dict) -> str:
    return (f"--- {prompt['kind']} by {prompt.get('origin') or 'unknown'}"
            f"\n{prompt.get('text') or ''}").rstrip()


def _render_entry(entry: dict) -> str:
    if entry["type"] == "assistant":
        return f"--- assistant\n{entry.get('text') or ''}"
    if entry["type"] == "tool":
        return (f"--- tool {entry['tool']}\n{entry.get('input') or ''}"
                f"\n>>> {entry.get('output') or ''}")
    return f"--- denied {entry['tool']} ({entry.get('denial') or 'no kind'})"
