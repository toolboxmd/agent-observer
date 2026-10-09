"""Full content behind ledger events, read on demand from native logs.

The ledger keeps no tool inputs, outputs or full assistant text (README
"Privacy"). This module finds them again through each event's source and
native id, applies secret redaction, and returns them without writing
anything. Each source is parsed once per call, so a batch of events from
the same session costs one read.

Per event the status is one of:
- ok: the native record was found in a source that only grew since import
- source_missing: the log (or OpenCode database) no longer exists
- source_changed: the log shrank or was replaced since import
- not_found: the source is intact but holds no record with that native id
- no_such_event: the ledger has no event with that id
Content keys appear only for ok.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from . import privacy
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
    index = _Index()
    for line, obj in _lines(path):
        if not isinstance(obj, dict):
            index.records[line] = obj
            continue
        index.records[obj.get("ordinal", line)] = obj
        p = obj.get("payload")
        if not isinstance(p, dict):
            continue
        ptype = p.get("type")
        if obj.get("type") == "response_item":
            if ptype == "message" and p.get("role") == "assistant":
                index.text(p.get("id"), _codex_text(p.get("content")))
            elif ptype == "message" and p.get("role") == "user":
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
                index.call(key, item.get("tool") or itype, {
                    k: v for k, v in item.items()
                    if k not in _ITEM_RESULT_KEYS and k not in ("id", "type")})
                found = [item[k] for k in _ITEM_RESULT_KEYS if k in item]
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
    index = _Index(joiner="")
    updates = os.path.join(os.path.dirname(path), "updates.jsonl")
    if os.path.exists(updates):
        for _, obj in _lines(updates):
            params = obj.get("params") if isinstance(obj, dict) else None
            update = params.get("update") if isinstance(params, dict) else None
            if not isinstance(update, dict):
                continue
            kind = update.get("sessionUpdate")
            call = update.get("toolCallId")
            if kind == "agent_message_chunk":
                content = update.get("content")
                if isinstance(content, dict):
                    index.text(None, content.get("text"))
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
    finally:
        native.close()
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
                index.text(None, data.get("text"))
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


def _source_state(row) -> str:
    """ok, source_missing or source_changed for one stored source row."""
    path = row["path"]
    if row["harness"] == "opencode":
        return "ok" if os.path.exists(path.partition("#")[0]) else "source_missing"
    try:
        st = os.stat(path)
    except OSError:
        return "source_missing"
    if row["ino"] is not None and st.st_ino != row["ino"]:
        return "source_changed"
    if st.st_size < (row["size_bytes"] or 0):
        return "source_changed"
    offset = row["read_offset"] or 0
    if offset and row["tail_sha256"]:
        with open(path, "rb") as fh:
            if _tail_sha(fh, offset) != row["tail_sha256"]:
                return "source_changed"
    return "ok"


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


def _redacted(value):
    if isinstance(value, str):
        return privacy.redact_secrets(value)
    if isinstance(value, list):
        return [_redacted(v) for v in value]
    if isinstance(value, dict):
        return {k: _redacted(v) for k, v in value.items()}
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
    for key in (event["ordinal_num"], native):
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
    indexes: dict[str, tuple[str, _Index | None]] = {}
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
        path = event["path"]
        if path not in indexes:
            state = _source_state(event) if path else "source_missing"
            if state == "ok" and event["harness"] == "grok":
                state = _grok_updates_state(con, path)
            index = None
            if state == "ok" and event["harness"] in _INDEXERS:
                try:
                    index = _INDEXERS[event["harness"]](path)
                except (OSError, sqlite3.Error):
                    state = "source_missing"
            indexes[path] = (state, index)
        state, index = indexes[path]
        content = _content(event, index) if index is not None else None
        if state == "ok" and content is None:
            state = "not_found"
        item["status"] = state
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
        for key in ("assistant_text", "text", "input", "result", "record"):
            if key in item and item[key] is not None:
                lines.append(f"--- {key}")
                lines.append(_block(item[key]))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
