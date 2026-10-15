"""Full content behind ledger events, read on demand from native logs.

The ledger keeps no tool inputs, outputs or full assistant text (README
"Privacy"). This module finds them again through each event's source and
native id, applies secret redaction, and returns them without writing
anything. Each source is parsed once per call, so a batch of events from
the same session costs one read.

Per event the status is one of:
- ok: the native record was found in a source that only grew since import.
  `reason` is `live` at the indexed path, or `relocated` when that path is
  gone and the same file name was read from the harness's session or archive tree
- source_missing: the log is not readable, the OpenCode database or its
  session row is gone, or a Grok session's updates log is gone. `reason`
  is `gone` (nothing with that file name remains in the session or archive
  tree, or the OpenCode session row is gone), `ambiguous` (more than one
  copy), or `no_source` (no source row)
- source_changed: the log shrank or was replaced since import (`reason`
  `replaced`)
- not_found: the source is intact but holds no record with that native id
- no_such_event: the ledger has no event with that id
Content keys appear only for ok. A missing source is never reported as a
complete session.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from . import privacy
from .db import _has_table
from .ingest import _tail_sha

TOOL_FAMILIES = ("tool_call", "tool_result", "read", "skill_read",
                 "file_change", "skill_invoke", "permission")

# Codex item fields that hold a result rather than the call itself.
_ITEM_RESULT_KEYS = ("aggregated_output", "stdout", "stderr", "output",
                     "result", "formatted_output")


class _Index:
    """Content of one native source, keyed by native id."""

    def __init__(self, joiner: str = "\n\n"):
        self.joiner = joiner
        self.calls: dict[str, dict] = {}
        self.results: dict[str, object] = {}
        self.texts: dict[str, str] = {}
        self.context: dict[str, str] = {}
        self.records: dict[object, object] = {}
        self._pending: list[str] = []

    def text(self, key, text: str) -> None:
        if not isinstance(text, str) or not text:
            return
        if key is not None:
            if key in self.texts:
                return
            self.texts[key] = text
        self._pending.append(text)

    def call(self, key, name, value) -> None:
        if not isinstance(key, str) or not key:
            return
        entry = self.calls.setdefault(key, {"name": name, "input": value})
        if entry["name"] in (None, "unknown") and name:
            entry["name"] = name
        if isinstance(entry["input"], dict) and isinstance(value, dict):
            entry["input"] = {**entry["input"], **value}
        elif entry["input"] is None:
            entry["input"] = value
        if key not in self.context and self._pending:
            self.context[key] = self.joiner.join(self._pending)

    def result(self, key, value) -> None:
        if isinstance(key, str) and key and value is not None:
            self.results[key] = value
        self.boundary()

    def boundary(self) -> None:
        self._pending = []


def _lines(path: str):
    """(line index, parsed object) for complete JSONL lines, as ingest counts them."""
    with open(path, "rb") as fh:
        ordinal = 0
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            text = raw.decode("utf-8", "replace")
            if not text.strip():
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                obj = None
            yield ordinal, obj
            ordinal += 1


def _claude_result(content):
    if isinstance(content, list):
        if all(isinstance(b, dict) and b.get("type") == "text" for b in content):
            return "\n".join(str(b.get("text", "")) for b in content)
        return [{**b, "source": "[image omitted]"}
                if isinstance(b, dict) and b.get("type") == "image" else b
                for b in content]
    return content


def _index_claude(path: str) -> _Index:
    index = _Index()
    for ordinal, obj in _lines(path):
        index.records[ordinal] = obj
        if not isinstance(obj, dict):
            continue
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if obj.get("type") == "assistant" and isinstance(content, list):
            key = obj.get("uuid") or message.get("id")
            for i, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    index.text(f"{key}:{i}", block.get("text"))
                elif block.get("type") == "tool_use":
                    index.call(block.get("id"), block.get("name"),
                               block.get("input"))
        elif obj.get("type") == "user":
            results = [b for b in content if isinstance(b, dict)
                       and b.get("type") == "tool_result"] \
                if isinstance(content, list) else []
            for block in results:
                index.result(block.get("tool_use_id"),
                             _claude_result(block.get("content")))
            index.boundary()
    return index


def _codex_text(content) -> str:
    if not isinstance(content, list):
        return ""
    return "".join(str(c.get("text", "")) for c in content
                   if isinstance(c, dict) and isinstance(c.get("text"), str))


def _parsed(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _index_codex(path: str) -> _Index:
    from .adapters.codex import _fallback_native_id

    index = _Index()
    for line, obj in _lines(path):
        # Integer keys are the complete-line index only. A metadata ordinal
        # shares that namespace and would hide a different physical record.
        index.records[line] = obj
        if not isinstance(obj, dict):
            continue
        fallback = _fallback_native_id(path, line)
        index.records[fallback] = obj
        p = obj.get("payload")
        if not isinstance(p, dict):
            continue
        ptype = p.get("type")
        if obj.get("type") == "event_msg" and ptype == "turn_aborted":
            if p.get("turn_id"):
                index.records[f"turn_aborted:{p['turn_id']}"] = obj
            else:
                index.records[f"turn_aborted:{fallback}"] = obj
                # The legacy turn_aborted:None key collapsed distinct aborts.
                # Leave it to _content's retained file-position lookup.
        elif obj.get("type") == "event_msg" and ptype == "task_complete":
            index.records[f"task_complete:{p.get('turn_id')}"] = obj
        if obj.get("type") == "response_item":
            if ptype == "message" and p.get("role") == "assistant":
                text = _codex_text(p.get("content"))
                index.text(p.get("id") or fallback, text)
                if not p.get("id"):
                    raw = obj.get("ordinal")
                    if text and isinstance(raw, int) and not isinstance(raw, bool):
                        # A lookup alias is not another native text unit.
                        index.texts.setdefault(f"ordinal:{raw}", text)
            elif ptype == "message" and p.get("role") == "user":
                message_id = p.get("id")
                if isinstance(message_id, str) and message_id:
                    index.records[message_id] = obj
                index.boundary()
            elif ptype == "function_call":
                index.call(p.get("call_id") or p.get("id"), p.get("name"),
                           _parsed(p.get("arguments")))
            elif ptype == "custom_tool_call":
                index.call(p.get("call_id") or p.get("id"), p.get("name"),
                           p.get("input"))
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                out = p.get("output")
                index.result(p.get("call_id") or p.get("id"),
                             _codex_text(out) if isinstance(out, list) else out)
        elif obj.get("type") == "event_msg" and ptype == "item_completed":
            item = p.get("item")
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "AgentMessage":
                index.text(item.get("id"), _codex_text(item.get("content")))
            elif itype == "UserMessage":
                index.boundary()
            elif itype not in ("Reasoning",) and item.get("id"):
                key = item.get("call_id") or item.get("id")
                search = itype == "Extension" and item.get("kind") == "web.search"
                result_keys = _ITEM_RESULT_KEYS + ("results",) if search else _ITEM_RESULT_KEYS
                index.call(key, "web_search" if search else item.get("tool") or itype, {
                    k: v for k, v in item.items()
                    if k not in result_keys and k not in ("id", "type")})
                found = [item[k] for k in result_keys if k in item]
                index.result(key, found[0] if found else None)
                if key != item.get("id"):
                    index.calls.setdefault(item["id"], index.calls[key])
                    if key in index.results:
                        index.results.setdefault(item["id"], index.results[key])
    return index


def _grok_name(update: dict):
    meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
    tool = meta.get("x.ai/tool")
    if isinstance(tool, dict) and isinstance(tool.get("name"), str):
        return tool["name"]
    return update.get("title")


def _index_grok(path: str) -> _Index:
    from .adapters.grok import _content_text, _valid_native_id
    index = _Index(joiner="")
    updates = os.path.join(os.path.dirname(path), "updates.jsonl")
    if os.path.exists(updates):
        for ordinal, obj in _lines(updates):
            params = obj.get("params") if isinstance(obj, dict) else None
            update = params.get("update") if isinstance(params, dict) else None
            if not isinstance(update, dict):
                continue
            kind = update.get("sessionUpdate")
            call = update.get("toolCallId")
            if kind == "agent_message_chunk":
                content = _content_text(update.get("content"))
                if content:
                    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
                    index.text(_valid_native_id(meta.get("eventId")) or f"ordinal:{ordinal}", content)
            elif kind == "user_message_chunk":
                index.boundary()
            elif kind in ("tool_call", "tool_call_update"):
                index.call(call, _grok_name(update), update.get("rawInput"))
                if "rawOutput" in update or "content" in update:
                    value = update.get("rawOutput", update.get("content"))
                    if value is not None:
                        index.results[call] = value
                if update.get("status") in ("completed", "failed", "cancelled"):
                    index.boundary()
    for ordinal, obj in _lines(path):
        index.records[ordinal] = obj
    return index


def _index_opencode(path: str) -> _Index:
    db_path, _, session = path.partition("#")
    index = _Index()
    native = sqlite3.connect(Path(db_path).absolute().as_uri() + "?mode=ro",
                             uri=True)
    try:
        rows = native.execute(
            "SELECT p.id, p.data, m.data FROM part p"
            " LEFT JOIN message m ON m.id = p.message_id"
            " WHERE p.session_id=? ORDER BY p.time_created, p.id",
            (session,)).fetchall()
        messages = native.execute(
            "SELECT id, data FROM message WHERE session_id=?",
            (session,)).fetchall()
    finally:
        native.close()
    # Lifecycle events (message errors) are keyed by message id.
    for message_id, raw in messages:
        try:
            index.records[message_id] = json.loads(raw)
        except (ValueError, TypeError):
            continue
    for part_id, raw, message in rows:
        try:
            data = json.loads(raw)
            role = (json.loads(message) or {}).get("role") if message else None
        except (ValueError, TypeError, AttributeError):
            continue
        if not isinstance(data, dict):
            continue
        index.records[part_id] = data
        if data.get("type") == "text":
            if role == "assistant":
                if data.get("text") == "":
                    # OpenCode can clear an indexed part without removing it.
                    # Keep the empty value out of tool narration.
                    index.texts[part_id] = ""
                else:
                    index.text(part_id, data.get("text"))
            else:
                index.boundary()
        elif data.get("type") == "tool":
            state = data.get("state") if isinstance(data.get("state"), dict) else {}
            index.call(data.get("callID"), data.get("tool"), state.get("input"))
            index.result(data.get("callID"),
                         state.get("output", state.get("error")))
    return index


_INDEXERS = {"claude": _index_claude, "codex": _index_codex,
             "grok": _index_grok, "opencode": _index_opencode}


class _NameIndex:
    """Basename to a single path under a root, for one availability check.

    A name that occurs twice under the same root is ambiguous and is not a
    recovery candidate. The index is not reused across calls: a file can
    appear between them.
    """

    def __init__(self):
        self._roots: dict[str, dict[str, str | None]] = {}

    def find(self, roots: list[str], name: str) -> str | None | bool:
        """The unique path, None when absent, or False when ambiguous."""
        seen = None
        ambiguous = False
        for root in roots:
            hits = self._root(root)
            if name not in hits:
                continue
            hit = hits[name]
            if hit is None:
                ambiguous = True
                continue
            if seen is None:
                seen = hit
            elif seen != hit:
                ambiguous = True
        if seen is not None and not ambiguous:
            return seen
        if ambiguous:
            return False
        return None

    def _root(self, root: str) -> dict[str, str | None]:
        cached = self._roots.get(root)
        if cached is not None:
            return cached
        found: dict[str, str | None] = {}
        if os.path.isdir(root):
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    if not name.endswith(".jsonl"):
                        continue
                    path = os.path.join(dirpath, name)
                    if name in found:
                        found[name] = None
                    else:
                        found[name] = path
        self._roots[root] = found
        return found


def _search_roots(harness: str, path: str) -> list[str]:
    """Session and archive trees that may hold a file moved off `path`.

    Codex keeps the same rollout name when it archives a session. A kit
    `CODEX_HOME` uses the same `sessions` layout, so a missing kit file is
    also sought under the default Codex home. Other harnesses have no
    archive tree Observer knows how to read.
    """
    if harness != "codex" or not path:
        return []
    roots: list[str] = []
    for marker in ("/archived_sessions/", "/sessions/"):
        at = path.find(marker)
        if at != -1:
            base = path[:at]
            roots.append(os.path.join(base, "sessions"))
            roots.append(os.path.join(base, "archived_sessions"))
            break
    if any(part in path for part in ("/sessions/", "/archived_sessions/", "/.codex/")):
        from .adapters.codex import ARCHIVE_ROOT, DEFAULT_ROOT
        roots.extend((DEFAULT_ROOT, ARCHIVE_ROOT))
    out = []
    seen = set()
    for root in roots:
        if root and root not in seen:
            seen.add(root)
            out.append(root)
    return out


def _file_state(row, path: str, *, ignore_ino: bool) -> str:
    """ok or source_changed for a file that exists. OSError is source_missing."""
    try:
        st = os.stat(path)
    except OSError:
        return "source_missing"
    if not ignore_ino and row["ino"] is not None and st.st_ino != row["ino"]:
        return "source_changed"
    if st.st_size < (row["size_bytes"] or 0):
        return "source_changed"
    offset = row["read_offset"] or 0
    if offset and row["tail_sha256"]:
        try:
            with open(path, "rb") as fh:
                if _tail_sha(fh, offset) != row["tail_sha256"]:
                    return "source_changed"
        except OSError:
            return "source_missing"
    return "ok"


def resolve_source(row, names: _NameIndex | None = None) -> dict:
    """Where an indexed source can be read, and why.

    `status` is `ok`, `source_missing` or `source_changed`. `reason` is
    `live`, `relocated`, `gone`, `ambiguous`, `replaced` or `no_source`.
    `path` is set only when status is `ok`.
    """
    path = row["path"] if "path" in row.keys() else None
    harness = row["harness"] if "harness" in row.keys() else None
    if not path or not harness:
        return {"status": "source_missing", "reason": "no_source", "path": None}
    if harness == "opencode":
        db_path, sep, session = path.partition("#")
        if (os.path.exists(db_path) and sep and session
                and _opencode_session_exists(db_path, session)):
            return {"status": "ok", "reason": "live", "path": path}
        return {"status": "source_missing", "reason": "gone", "path": None}
    if os.path.exists(path):
        state = _file_state(row, path, ignore_ino=False)
        if state == "ok":
            return {"status": "ok", "reason": "live", "path": path}
        if state == "source_missing":
            return {"status": "source_missing", "reason": "gone", "path": None}
        return {"status": "source_changed", "reason": "replaced", "path": None}
    names = names or _NameIndex()
    found = names.find(_search_roots(harness, path), os.path.basename(path))
    if found is False:
        return {"status": "source_missing", "reason": "ambiguous", "path": None}
    if not found:
        return {"status": "source_missing", "reason": "gone", "path": None}
    state = _file_state(row, found, ignore_ino=True)
    if state == "ok":
        return {"status": "ok", "reason": "relocated", "path": found}
    if state == "source_missing":
        return {"status": "source_missing", "reason": "gone", "path": None}
    return {"status": "source_changed", "reason": "replaced", "path": None}


def _source_state(row) -> str:
    """ok, source_missing or source_changed for one stored source row."""
    return resolve_source(row)["status"]


def _event_reason(resolved: dict, state: str) -> str:
    if state == "source_changed":
        return "replaced"
    if state == "source_missing":
        return resolved["reason"] if resolved["reason"] in (
            "gone", "ambiguous", "no_source") else "gone"
    if state == "not_found":
        return resolved["reason"]
    return resolved["reason"]


_AVAIL_ORDER = ("live", "relocated", "ledger_only", "changed")


def _empty_counts() -> dict:
    return {key: 0 for key in _AVAIL_ORDER}


def _session_status(counts: dict, *, unreadable: bool = False) -> str:
    """available, partial or ledger_only. Never complete.

    `unreadable` covers a backing source that is gone or replaced even when
    it contributed no indexed events, such as a response-only session.
    """
    readable = counts["live"] + counts["relocated"]
    gone = counts["ledger_only"]
    changed = counts["changed"]
    if gone == 0 and changed == 0 and not unreadable:
        return "available"
    if readable == 0 and changed == 0:
        return "ledger_only"
    return "partial"


def _scope_status(sessions: dict) -> str:
    """One scope is available only when every session in it is."""
    if sessions["partial"] or (
            sessions["available"] and sessions["ledger_only"]):
        return "partial"
    if sessions["ledger_only"]:
        return "ledger_only"
    return "available"


def _key_filter(column: str, session_key, session_keys) -> tuple[str, list]:
    if session_key is not None:
        return f" AND {column}=?", [session_key]
    if session_keys is not None:
        keys = sorted(session_keys)
        marks = ",".join("?" * len(keys))
        return f" AND {column} IN ({marks})", keys
    return "", []


def _backing_without_events(con, event_rows, *, session_key, session_keys,
                            harness) -> list:
    """Sources that back a session but have no event rows.

    A response-only session still has a native source. Event counts miss it,
    and a default of available would hide that the source is gone.
    """
    selects = []
    args: list = []
    for table in ("sessions", "events", "user_messages", "responses", "turns",
                  "codex_occurrences"):
        # Older schema-3 ledgers acquire occurrence metadata only on sync.
        if table == "codex_occurrences" and not _has_table(con, table):
            continue
        clause, key_args = _key_filter("session_key", session_key, session_keys)
        selects.append(
            f"SELECT session_key, source_id FROM {table}"
            f" WHERE source_id IS NOT NULL{clause}")
        args.extend(key_args)
    clause, key_args = _key_filter(
        "sess.session_key", session_key, session_keys)
    selects.append(
        "SELECT sess.session_key, src.id FROM sessions sess"
        " JOIN sources src ON src.harness = sess.harness"
        " AND src.session_id = sess.native_id"
        f" WHERE src.id IS NOT NULL{clause}")
    args.extend(key_args)
    harness_sql = ""
    if harness is not None:
        harness_sql = " WHERE harness=?"
        args.append(harness)
    found = con.execute(
        "SELECT session_key, source_id, 0 n, harness, path, size_bytes,"
        " read_offset, tail_sha256, ino FROM ("
        "SELECT b.session_key, b.source_id,"
        " COALESCE(src.harness, sess.harness, substr(b.session_key, 1,"
        " instr(b.session_key, ':') - 1)) harness,"
        " src.path, src.size_bytes, src.read_offset, src.tail_sha256, src.ino"
        f" FROM ({' UNION '.join(selects)}) b"
        " LEFT JOIN sources src ON src.id = b.source_id"
        " LEFT JOIN sessions sess ON sess.session_key = b.session_key"
        f") backed{harness_sql}", args).fetchall()
    seen = {(row["session_key"], row["source_id"]) for row in event_rows}
    return [row for row in found
            if (row["session_key"], row["source_id"]) not in seen]


def availability(con: sqlite3.Connection, session_key: str | None = None,
                 session_keys: set | None = None,
                 harness: str | None = None) -> dict:
    """Per session, how many indexed events still have a readable native source.

    A machine summary omits paths. One session includes each source's status
    and reason so a caller can see why hydration will fail.
    """
    where = []
    args: list = []
    if session_key is not None:
        where.append("e.session_key=?")
        args.append(session_key)
    elif session_keys is not None:
        keys = sorted(session_keys)
        if not keys:
            empty = _empty_counts()
            return {"status": "available", "events": empty, "sessions": {
                "available": 0, "partial": 0, "ledger_only": 0},
                "reasons": {}, "by_harness": {}, "unavailable_sources": []}
        where.append(
            f"e.session_key IN ({','.join('?' * len(keys))})")
        args.extend(keys)
    if harness is not None:
        where.append(
            "COALESCE(s.harness, substr(e.session_key, 1,"
            " instr(e.session_key, ':') - 1))=?")
        args.append(harness)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = list(con.execute(
        "SELECT e.session_key, e.source_id, COUNT(*) n,"
        " COALESCE(s.harness, substr(e.session_key, 1,"
        " instr(e.session_key, ':') - 1)) harness,"
        " s.path, s.size_bytes, s.read_offset, s.tail_sha256, s.ino"
        " FROM events e LEFT JOIN sources s ON s.id = e.source_id"
        f"{clause} GROUP BY e.session_key, e.source_id", args))
    rows.extend(_backing_without_events(
        con, rows, session_key=session_key, session_keys=session_keys,
        harness=harness))
    names = _NameIndex()
    resolved_by: dict[object, dict] = {}
    per: dict[str, dict] = {}
    per_harness: dict[str, dict] = {}
    reasons: dict[str, int] = {}

    def bucket(harness_name: str) -> dict:
        found = per_harness.get(harness_name)
        if found is None:
            found = {"events": _empty_counts(), "reasons": {}}
            per_harness[harness_name] = found
        return found

    for row in rows:
        source_id = row["source_id"]
        if source_id not in resolved_by:
            if row["path"] is None:
                resolved_by[source_id] = {
                    "status": "source_missing", "reason": "no_source",
                    "path": None}
            else:
                resolved_by[source_id] = _with_companion(
                    con, row, resolve_source(row, names))
        resolved = resolved_by[source_id]
        if resolved["status"] == "ok" and resolved["reason"] == "relocated":
            kind = "relocated"
        elif resolved["status"] == "ok":
            kind = "live"
        elif resolved["status"] == "source_changed":
            kind = "changed"
        else:
            kind = "ledger_only"
        # Readable and eventless is not a gap, and it is not an extra
        # available session. Only a gone or replaced source marks one.
        if row["n"] == 0 and kind in ("live", "relocated"):
            continue
        entry = per.setdefault(row["session_key"], {
            "events": _empty_counts(), "reasons": {}, "sources": [],
            "harness": row["harness"], "unreadable": False})
        if row["n"] == 0:
            entry["unreadable"] = True
        else:
            entry["events"][kind] += row["n"]
        if kind != "live" and row["n"]:
            entry["reasons"][resolved["reason"]] = (
                entry["reasons"].get(resolved["reason"], 0) + row["n"])
            reasons[resolved["reason"]] = reasons.get(resolved["reason"], 0) + row["n"]
        elif kind != "live":
            entry["reasons"].setdefault(resolved["reason"], 0)
        if kind not in ("live", "relocated"):
            entry.setdefault("lost_sources", []).append(source_id)
        if session_key is not None:
            entry["sources"].append({
                "source_id": source_id,
                "status": resolved["status"],
                "reason": resolved["reason"],
                "events": row["n"],
                "path": row["path"],
            })
        if row["n"]:
            harness_bucket = bucket(row["harness"] or "unknown")
            harness_bucket["events"][kind] += row["n"]
            if kind != "live":
                harness_bucket["reasons"][resolved["reason"]] = (
                    harness_bucket["reasons"].get(resolved["reason"], 0)
                    + row["n"])
    sessions = {"available": 0, "partial": 0, "ledger_only": 0}
    events = _empty_counts()
    for entry in per.values():
        status = _session_status(
            entry["events"], unreadable=entry.get("unreadable", False))
        entry["status"] = status
        sessions[status] += 1
        for key in _AVAIL_ORDER:
            events[key] += entry["events"][key]
    summary = {
        "status": _scope_status(sessions),
        "events": events,
        "sessions": sessions,
        "reasons": reasons,
        "by_harness": per_harness,
        "unavailable_sources": sorted({
            sid for entry in per.values()
            for sid in entry.get("lost_sources", [])
            if isinstance(sid, int)}),
    }
    if session_keys is not None:
        summary["by_session"] = {
            key: entry["status"] for key, entry in per.items()}
    if session_key is not None:
        one = per.get(session_key) or {
            "status": "available", "events": _empty_counts(),
            "reasons": {}, "sources": [], "harness": None}
        one["session_key"] = session_key
        return one
    return summary


def _opencode_session_exists(db_path: str, session: str) -> bool:
    """The native session row is the OpenCode source, not the database file."""
    try:
        native = sqlite3.connect(
            Path(db_path).absolute().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = native.execute(
            "SELECT 1 FROM session WHERE id=? LIMIT 1", (session,)).fetchone()
    except sqlite3.Error:
        return False
    finally:
        native.close()
    return row is not None


def _grok_updates_state(con: sqlite3.Connection, path: str) -> str:
    """Grok tool content lives in the session's updates.jsonl; check it too."""
    updates = os.path.join(os.path.dirname(path), "updates.jsonl")
    if updates == path:
        return "ok"
    row = con.execute(
        "SELECT harness, path, size_bytes, read_offset, tail_sha256, ino"
        " FROM sources WHERE harness='grok' AND path=?", (updates,)).fetchone()
    if row is None:
        return "ok" if os.path.exists(updates) else "source_missing"
    return _source_state(row)


def _with_companion(con: sqlite3.Connection, row, resolved: dict) -> dict:
    """The same readability check hydration uses, including companion logs.

    A Grok session's tool text lives in updates.jsonl. If that file is gone
    or replaced, every event from the session fails hydration, so availability
    must not call those events live.
    """
    if resolved["status"] != "ok":
        return resolved
    harness = row["harness"] if "harness" in row.keys() else None
    if harness != "grok" or not resolved.get("path"):
        return resolved
    grok_state = _grok_updates_state(con, resolved["path"])
    if grok_state == "ok":
        return resolved
    return {
        "status": grok_state,
        "reason": "gone" if grok_state == "source_missing" else "replaced",
        "path": None,
    }


# Structured inputs split a secret's name from its value, which the string
# rules in privacy.redact_secrets only catch together. A string under one
# of these keys (case, `-` and `_` ignored) or under an environment-style
# NAME such as GH_TOKEN is replaced whole.
_SECRET_KEYS = frozenset({
    "apikey", "accesstoken", "refreshtoken", "clientsecret", "secret",
    "password", "passwd", "token", "privatekey", "accesskey",
    "authorization", "credential", "credentials", "auth", "bearer"})
_SECRET_ENV_KEY = re.compile(
    r"^[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY"
    r"|PRIVATE_?KEY|CREDENTIALS?)[A-Z0-9_]*$")


def _secret_key(key) -> bool:
    if not isinstance(key, str):
        return False
    return (key.lower().replace("-", "").replace("_", "") in _SECRET_KEYS
            or bool(_SECRET_ENV_KEY.match(key)))


def _redacted(value):
    if isinstance(value, str):
        return privacy.redact_secrets(value)
    if isinstance(value, list):
        return [_redacted(v) for v in value]
    if isinstance(value, dict):
        return {k: privacy.REDACTED if _secret_key(k) and isinstance(v, str)
                and v else _redacted(v) for k, v in value.items()}
    return value


def _content(event, index: _Index) -> dict | None:
    native = event["native_id"]
    family = event["family"]
    if family == "assistant_message":
        text = index.texts.get(native)
        return None if text is None else {"text": text}
    if family in TOOL_FAMILIES:
        for key in (native, native.split(":", 1)[0]):
            if key in index.calls or key in index.results:
                call = index.calls.get(key) or {}
                out = {"tool": call.get("name"), "input": call.get("input"),
                       "result": index.results.get(key)}
                if key in index.context:
                    out["assistant_text"] = index.context[key]
                return out
    for key in (native, event["ordinal_num"]):
        if key in index.records:
            return {"record": index.records[key]}
    return None


def show(con: sqlite3.Connection, event_ids: list[int]) -> list[dict]:
    """Content for each requested event id, in request order."""
    wanted = sorted(set(event_ids))
    rows = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        for row in con.execute(
                "SELECT e.id, e.session_key, e.family, e.name, e.native_id,"
                " e.ordinal_num, e.ts, s.harness, s.path, s.size_bytes,"
                " s.read_offset, s.tail_sha256, s.ino"
                " FROM events e LEFT JOIN sources s ON s.id = e.source_id"
                f" WHERE e.id IN ({','.join('?' * len(chunk))})", chunk):
            rows[row["id"]] = row
    names = _NameIndex()
    indexes: dict[str, tuple] = {}
    out = []
    for event_id in event_ids:
        event = rows.get(event_id)
        if event is None:
            out.append({"id": event_id, "status": "no_such_event"})
            continue
        item = {"id": event_id, "harness": event["harness"],
                "session_key": event["session_key"],
                "family": event["family"], "name": event["name"],
                "native_id": event["native_id"], "ts": event["ts"],
                "source": event["path"]}
        path = event["path"] or ""
        if path not in indexes:
            if event["path"]:
                resolved = resolve_source(event, names)
            else:
                resolved = {"status": "source_missing", "reason": "no_source",
                            "path": None}
            resolved = _with_companion(con, event, resolved)
            state = resolved["status"]
            reason = resolved["reason"]
            read_path = resolved["path"]
            index = None
            if state == "ok" and event["harness"] in _INDEXERS:
                try:
                    index = _INDEXERS[event["harness"]](read_path)
                except (OSError, sqlite3.Error):
                    state = "source_missing"
                    reason = "gone"
                    read_path = None
            indexes[path] = (state, reason, index, read_path)
        state, reason, index, read_path = indexes[path]
        content = _content(event, index) if index is not None else None
        if state == "ok" and content is None:
            state = "not_found"
            reason = "not_found"
        item["status"] = state
        item["reason"] = reason
        if read_path and read_path != event["path"]:
            item["read_path"] = read_path
        if state == "ok":
            item.update(_redacted(content))
        out.append(item)
    return out


def _block(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def render(items: list[dict]) -> str:
    parts = []
    for item in items:
        head = (f"== event {item['id']} {item.get('harness') or ''}"
                f" {item.get('family') or ''} {item.get('name') or ''}"
                f" [{item['status']}]").rstrip()
        lines = [head]
        if item.get("source"):
            lines.append(f"source: {item['source']}")
        if item.get("read_path"):
            lines.append(f"read_path: {item['read_path']}")
        if item.get("reason") and item["reason"] != "live":
            lines.append(f"reason: {item['reason']}")
        for key in ("assistant_text", "text", "input", "result", "record"):
            if key in item and item[key] is not None:
                lines.append(f"--- {key}")
                lines.append(_block(item[key]))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
