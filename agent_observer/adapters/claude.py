"""Claude Code native adapter: session transcripts into the ledger.

Reads `~/.claude/projects/<project>/<session>.jsonl` and subagent transcripts
under `<session>/subagents/agent-<id>.jsonl`. A subagent is its own session
linked to its parent; its API calls are distinct messages, so parent and
subagent usage never overlap.

Counter rule: Claude writes one record per streamed content block and repeats
the same usage on each, so a response is counted once per message id. Input
excludes cache reads and cache writes; output includes thinking, so
total_tokens = input + cache_creation + cache_read + output.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3

from .. import db
from ..identity import SessionIdentity, skill_from_path
from ..ingest import JsonlSource, fingerprint, insert_event, iso_ts, text_hash

HARNESS = "claude"
SEMANTICS = "claude:input_excludes_cache,output_includes_thinking"
DEFAULT_ROOT = os.path.expanduser("~/.claude/projects")
SCAFFOLD_PREFIXES = ("<command-name>", "<local-command", "<system-reminder>",
                     "Caveat:", "<task-notification>", "<bash-")
INTERRUPT_PREFIX = "[Request interrupted"
SKILL_BASE_PREFIX = "Base directory for this skill:"
READ_TOOLS = {"Read": "file_path", "NotebookRead": "notebook_path"}
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path"}

CAPABILITIES = [
    ("model_usage", True, "assistant message usage once per message id; thinking inside output; cache read and creation separate from input"),
    ("tool_calls", True, "tool_use blocks with name, input fingerprint and target path or command"),
    ("tool_results", True, "tool_result blocks joined on tool_use_id; is_error and permission denials kept"),
    ("read_evidence", True, "Read tool results with resolved path and line range"),
    ("skill_file_reads", True, "reads under an installed Skill directory"),
    ("skill_invocation", True, "Skill tool calls name the skill"),
    ("compaction", True, "system compact_boundary records with trigger and pre-compaction tokens"),
    ("lifecycle_task", True, "turn_duration records; interrupts as submissions of kind interrupt"),
    ("human_input", True, "typed prompts, mid-turn queued prompts, slash commands and interrupts told apart"),
    ("instruction_identity", True, "AgentsMD direction block from the SessionStart hook; versioned plugin paths read"),
    ("subagents", True, "subagent transcripts as child sessions of their parent"),
]


def discover(root: str | None = None) -> list[str]:
    root = root or DEFAULT_ROOT
    main = glob.glob(os.path.join(root, "*", "*.jsonl"))
    subs = glob.glob(os.path.join(root, "*", "*", "subagents", "agent-*.jsonl"))
    return sorted(main + subs)


def sync(con: sqlite3.Connection, root: str | None = None, full: bool = False,
         source: str | None = None) -> dict:
    paths = [source] if source else discover(root)
    totals = {"harness": HARNESS, "sources": 0, "unchanged": 0,
              "responses_inserted": 0, "events_inserted": 0,
              "submissions_inserted": 0, "malformed": 0, "failed": []}
    for path in paths:
        try:
            stats = import_claude_file(con, path, full=full)
        except (OSError, sqlite3.DatabaseError, UnicodeDecodeError) as exc:
            totals["failed"].append({"path": path, "error": str(exc)})
            continue
        totals["sources"] += 1
        totals["unchanged"] += 1 if stats.get("unchanged") else 0
        for key in ("responses_inserted", "events_inserted",
                    "submissions_inserted", "malformed"):
            totals[key] += stats.get(key, 0)
    return totals


def _strings(obj):
    """Every string value inside a nested record."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _strings(value)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content
                       if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _target(name: str, tool_input: dict):
    if not isinstance(tool_input, dict):
        return None
    for key in ("file_path", "notebook_path", "path"):
        if tool_input.get(key):
            return str(tool_input[key])
    if tool_input.get("command"):
        return str(tool_input["command"])[:500]
    if tool_input.get("pattern"):
        return str(tool_input["pattern"])[:500]
    if tool_input.get("skill"):
        return str(tool_input["skill"])
    if tool_input.get("url"):
        return str(tool_input["url"])[:500]
    return None


class _Reader:
    def __init__(self, con, src: JsonlSource, stats: dict):
        self.con = con
        self.src = src
        self.stats = stats
        self.native_session = src.row["session_id"]
        self.agent_id = src.row["thread_id"]
        self.identity = SessionIdentity()
        self.meta: dict = {}
        self.first_ts = None
        self.last_ts = None
        self.turn = None
        self.pending: dict = {}
        # Message ids whose native final block (stop_reason) has been seen,
        # either earlier in this import or in the already-imported file
        # prefix. A row is updated while its message streams; once final,
        # every later same-ID repeat is validation only.
        self.finalized: set = set()

    @property
    def session_key(self) -> str:
        if not self.native_session:
            return f"{HARNESS}:file:{os.path.basename(self.src.path)}"
        if self.agent_id:
            return f"{HARNESS}:{self.native_session}:agent:{self.agent_id}"
        return f"{HARNESS}:{self.native_session}"

    def event(self, family, native_id, ordinal, ts, **kw):
        insert_event(self.con, self.stats, source_id=self.src.source_id,
                     session_key=self.session_key, family=family,
                     native_id=native_id, ordinal=ordinal, ts=ts,
                     turn_id=self.turn, **kw)


def import_claude_file(con: sqlite3.Connection, path: str,
                       full: bool = False) -> dict:
    stats = {"lines": 0, "responses_inserted": 0, "responses_duplicate": 0,
             "responses_updated": 0,
             "submissions_inserted": 0, "events_inserted": 0,
             "events_duplicate": 0, "compactions": 0, "malformed": 0}
    src = JsonlSource(con, HARNESS, path, full=full)
    r = _Reader(con, src, stats)
    if src.incremental:
        # Recover finality decided by earlier imports from the already-read
        # file prefix, so later imports validate post-final repeats instead
        # of updating them.
        r.finalized |= _prefix_finalized(src.path, src.start_offset)
    if src.incremental and src.row["session_id"]:
        # Resume the turn the previous import ended in.
        row = con.execute(
            "SELECT turn_id FROM submissions WHERE session_key=? AND kind='genuine'"
            " ORDER BY ts DESC LIMIT 1", (r.session_key,)).fetchone()
        r.turn = row["turn_id"] if row else None
    for ordinal, obj, line in src.records():
        stats["lines"] += 1
        if obj is None or not isinstance(obj, dict):
            stats["malformed"] += 1
            src.error(ordinal, "json_error", line)
            continue
        try:
            _ingest(r, obj, ordinal)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            stats["malformed"] += 1
            src.error(ordinal, f"schema_error: {exc}", line)
    if r.native_session:
        fields = {"started_at": r.first_ts, "ended_at": r.last_ts, **r.meta,
                  **r.identity.fields(con)}
        if r.agent_id:
            fields["parent_session_key"] = f"{HARNESS}:{r.native_session}"
            fields["role"] = "subagent"
        db.upsert_session(con, r.session_key, HARNESS,
                          r.session_key.split(":", 1)[1], src.source_id, **fields)
    stats.update(src.finish(session_id=r.native_session, thread_id=r.agent_id))
    stats["session_key"] = r.session_key
    con.commit()
    return stats


def _ingest(r: _Reader, obj: dict, ordinal: int) -> None:
    if obj.get("sessionId") and not r.native_session:
        r.native_session = obj["sessionId"]
    if obj.get("isSidechain") and obj.get("agentId") and not r.agent_id:
        r.agent_id = obj["agentId"]
    ts = iso_ts(obj.get("timestamp"))
    if ts is not None:
        r.first_ts = ts if r.first_ts is None else min(r.first_ts, ts)
        r.last_ts = ts if r.last_ts is None else max(r.last_ts, ts)
    for key, field in (("cwd", "project_dir"), ("gitBranch", "git_branch"),
                       ("version", "client_version"), ("entrypoint", "entrypoint")):
        if obj.get(key) and field not in r.meta:
            r.meta[field] = obj[key]
    kind = obj.get("type")
    if kind == "assistant":
        _assistant(r, obj, ordinal, ts)
    elif kind == "user":
        _user(r, obj, ordinal, ts)
    elif kind == "attachment":
        _attachment(r, obj, ordinal, ts)
    elif kind == "system":
        _system(r, obj, ordinal, ts)
    elif kind == "ai-title":
        if obj.get("aiTitle"):
            r.meta.setdefault("title", str(obj["aiTitle"])[:200])
    else:
        raise ValueError(f"unsupported record type: {kind!r}")


def _usage_values(usage) -> dict:
    """Native usage buckets with unknown preserved as NULL.

    total_tokens is the harness's own total and is only known when every
    bucket is known; a partial sum from some buckets is never constructed."""
    if not isinstance(usage, dict):
        raise ValueError(f"unsupported usage shape: {type(usage).__name__}")
    details = usage.get("output_tokens_details") or {}
    if not isinstance(details, dict):
        raise ValueError("unsupported usage details shape")
    values = [usage.get(k) for k in ("input_tokens",
                                     "cache_creation_input_tokens",
                                     "cache_read_input_tokens",
                                     "output_tokens")]
    return {
        "input_tokens": usage.get("input_tokens"),
        "cached_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_write_input_tokens": usage.get("cache_creation_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_output_tokens": details.get("thinking_tokens"),
        "total_tokens": sum(values)
        if all(isinstance(v, int) for v in values) else None,
    }


def _prefix_finalized(path: str, end_offset: int) -> set:
    """Message ids already finalized in the imported file prefix.

    An incremental import only reads appended lines, so finality decided by
    an earlier import is recovered from the source evidence itself: any
    assistant block before the resume offset that carries a native final
    stop_reason. Read-only; never touches harness state beyond reading.
    """
    finals = set()
    try:
        with open(path, "rb") as fh:
            data = fh.read(end_offset)
    except OSError:
        return finals
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        message = obj.get("message") or {}
        if not isinstance(message, dict):
            continue
        usage = message.get("usage") or {}
        if (message.get("id") and message.get("stop_reason") is not None
                and isinstance(usage, dict) and usage
                and message.get("model") != "<synthetic>"):
            finals.add(message["id"])
    return finals


def _stored_counters(r: _Reader, rid: str) -> dict:
    row = r.con.execute(
        "SELECT input_tokens, cached_input_tokens, cache_write_input_tokens,"
        " output_tokens, reasoning_output_tokens, total_tokens"
        " FROM responses WHERE response_id=?", (rid,)).fetchone()
    return dict(row) if row is not None else {}


def _ingest_usage_row(r: _Reader, obj: dict, ordinal: int, ts, mid: str,
                      model, values: dict, final: bool) -> None:
    """One streamed usage block for a message id.

    The first block inserts the row; later blocks update it while the message
    is still streaming. The final block finalizes it; every later repeat is
    validated instead of applied, and a mismatch is quarantined."""
    rid = f"{HARNESS}:{mid}"
    if mid in r.finalized:
        seen = _stored_counters(r, rid)
        if seen != values:
            raise ValueError(f"conflicting usage for {mid}:"
                             f" {seen!r} != {values!r}")
        r.stats["responses_duplicate"] += 1
        return
    cur = r.con.execute(
        "INSERT OR IGNORE INTO responses(response_id, source_id, harness,"
        " session_key, turn_id, session_id, ordinal_num, ts, model, effort,"
        " input_tokens, cached_input_tokens, cache_write_input_tokens,"
        " output_tokens, reasoning_output_tokens, total_tokens, semantics)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, r.src.source_id, HARNESS, r.session_key, r.turn,
         r.native_session, ordinal, ts, model, obj.get("effort"),
         values["input_tokens"], values["cached_input_tokens"],
         values["cache_write_input_tokens"], values["output_tokens"],
         values["reasoning_output_tokens"], values["total_tokens"],
         SEMANTICS))
    if cur.rowcount:
        r.stats["responses_inserted"] += 1
        if final:
            r.finalized.add(mid)
        return
    if _stored_counters(r, rid) == values:
        r.stats["responses_duplicate"] += 1
        if final:
            r.finalized.add(mid)
        return
    # The message is still streaming (or its final block just arrived): the
    # latest native block is the authority, across imports as well.
    r.con.execute(
        "UPDATE responses SET ordinal_num=?, ts=?,"
        " model=COALESCE(?, model), effort=COALESCE(?, effort),"
        " input_tokens=?, cached_input_tokens=?,"
        " cache_write_input_tokens=?, output_tokens=?,"
        " reasoning_output_tokens=?, total_tokens=?"
        " WHERE response_id=?",
        (ordinal, ts, model, obj.get("effort"),
         values["input_tokens"], values["cached_input_tokens"],
         values["cache_write_input_tokens"], values["output_tokens"],
         values["reasoning_output_tokens"], values["total_tokens"], rid))
    r.stats["responses_updated"] += 1
    if final:
        r.finalized.add(mid)


def _assistant(r: _Reader, obj: dict, ordinal: int, ts) -> None:
    message = obj.get("message") or {}
    if message and not isinstance(message, dict):
        raise ValueError(
            f"unsupported message shape: {type(message).__name__}")
    mid = message.get("id")
    model = message.get("model")
    usage = message.get("usage") or {}
    if mid and usage and model != "<synthetic>":
        values = _usage_values(usage)
        _ingest_usage_row(r, obj, ordinal, ts, mid, model, values,
                          message.get("stop_reason") is not None)
    for index, block in enumerate(message.get("content") or []):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use" and block.get("id"):
            name = str(block.get("name") or "unknown")
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            target = _target(name, tool_input)
            r.pending[block["id"]] = (name, tool_input)
            r.event("tool_call", block["id"], ordinal, ts, name=name, target=target,
                    fingerprint=fingerprint(name, json.dumps(tool_input, sort_keys=True)[:4000]),
                    detail={"message_id": mid})
            if name == "Skill":
                skill = str(tool_input.get("skill") or tool_input.get("command") or "unknown")
                r.event("skill_invoke", block["id"], ordinal, ts, name=skill,
                        target=skill, detail={"attribution": obj.get("attributionSkill")})
            if target and name in EDIT_TOOLS:
                r.event("file_change", block["id"], ordinal, ts, name=name,
                        target=target, fingerprint=fingerprint(target))
        elif btype == "text" and block.get("text"):
            text = block["text"]
            r.event("assistant_message", f"{obj.get('uuid') or mid}:{index}", ordinal, ts,
                    name="assistant_message", size_bytes=len(text),
                    fingerprint=text_hash(text),
                    detail={"excerpt": text[-400:], "stop_reason": message.get("stop_reason")})


def _submission(r: _Reader, native_id, ordinal, ts, kind: str, text: str) -> None:
    # Excerpts persist only genuine human input (or an interrupt); every
    # other kind keeps an empty excerpt so skill bodies, sidechain prompts,
    # hook output and scaffolding can never persist file or preference
    # contents. Identity extraction already saw the complete text.
    excerpt = text[:300] if kind in ("genuine", "interrupt") else ""
    cur = r.con.execute(
        "INSERT OR IGNORE INTO submissions(native_id, source_id, session_key,"
        " turn_id, ordinal_num, ts, kind, text_hash, text_excerpt, is_genuine)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"{HARNESS}:{native_id}", r.src.source_id, r.session_key, r.turn, ordinal,
         ts, kind, text_hash(text), excerpt, 1 if kind == "genuine" else 0))
    if cur.rowcount:
        r.stats["submissions_inserted"] += 1


def _user_kind(r: _Reader, obj: dict, text: str) -> str:
    if obj.get("isSidechain") or r.agent_id:
        return "synthetic"
    stripped = text.lstrip()
    if stripped.startswith(INTERRUPT_PREFIX):
        return "interrupt"
    if stripped.startswith(SKILL_BASE_PREFIX):
        # A loaded skill body arrives as message text: skill-load evidence,
        # never a genuine human submission.
        return "scaffolding"
    origin = obj.get("origin") if isinstance(obj.get("origin"), dict) else {}
    if origin.get("kind") == "human" or obj.get("promptSource") == "typed":
        return "genuine"
    if obj.get("isMeta") or stripped.startswith(SCAFFOLD_PREFIXES):
        return "command" if stripped.startswith("<command-name>") else "scaffolding"
    if obj.get("origin") is None and obj.get("promptSource") is None and stripped:
        # Older transcripts carry no origin: plain typed text is genuine.
        return "genuine"
    return "synthetic"


def _user(r: _Reader, obj: dict, ordinal: int, ts) -> None:
    message = obj.get("message") or {}
    content = message.get("content")
    results = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_result"] \
        if isinstance(content, list) else []
    for result in results:
        _tool_result(r, obj, result, ordinal, ts)
    if results:
        return
    text = _text(content)
    for value in _strings(content):
        r.identity.observe_text(value)
    if not text:
        return
    if text.startswith(SKILL_BASE_PREFIX):
        # The loaded Skill body arrives as a meta message naming its
        # installed directory: that is the skill-load evidence.
        base = text[len(SKILL_BASE_PREFIX):].splitlines()[0].strip()
        r.identity.observe_path(base.rstrip("/") + "/SKILL.md")
        r.event("skill_read", obj.get("uuid") or f"ordinal:{ordinal}", ordinal, ts,
                name=skill_from_path(base + "/") or os.path.basename(base),
                target=base, size_bytes=len(text),
                detail={"skill": skill_from_path(base + "/"), "evidence": "skill base directory"})
    kind = _user_kind(r, obj, text)
    native = obj.get("uuid") or f"ordinal:{ordinal}"
    if kind == "genuine":
        r.turn = f"{HARNESS}:{obj.get('promptId') or native}"
    _submission(r, native, ordinal, ts, kind, text)


def _tool_result(r: _Reader, obj: dict, result: dict, ordinal: int, ts) -> None:
    call_id = result.get("tool_use_id")
    if not call_id:
        return
    name, tool_input = r.pending.pop(call_id, (None, {}))
    body = result.get("content")
    size = len(body) if isinstance(body, str) else len(json.dumps(body or ""))
    denied = obj.get("toolDenialKind")
    status = "denied" if denied else ("error" if result.get("is_error") else "ok")
    structured = obj.get("toolUseResult") if isinstance(obj.get("toolUseResult"), dict) else {}
    detail = {"tool": name}
    if denied:
        detail["denial"] = denied
    for key in ("interrupted", "returnCodeInterpretation", "exitCode", "exit_code"):
        if key in structured:
            detail[key] = structured[key]
    r.event("tool_result", call_id, ordinal, ts, name=name or "unknown",
            target=_target(name or "", tool_input), status=status, size_bytes=size,
            detail=detail)
    if denied:
        r.event("permission", call_id, ordinal, ts, name=name or "unknown",
                status="denied", detail={"denial": denied})
    file_info = structured.get("file") if isinstance(structured.get("file"), dict) else None
    path = (file_info or {}).get("filePath") or (tool_input or {}).get(READ_TOOLS.get(name or "", ""), None)
    if name in READ_TOOLS and path and status == "ok":
        r.identity.observe_path(path)
        family = "skill_read" if skill_from_path(path) else "read"
        start = (file_info or {}).get("startLine")
        lines = (file_info or {}).get("numLines")
        r.event(family, f"{call_id}:read", ordinal, ts, name=os.path.basename(path),
                target=path, size_bytes=size,
                fingerprint=fingerprint(path, start, lines),
                detail={"start_line": start, "num_lines": lines,
                        "total_lines": (file_info or {}).get("totalLines"),
                        "skill": skill_from_path(path),
                        "content_sha": text_hash((file_info or {}).get("content") or "")
                        if file_info and file_info.get("content") is not None else None})


def _attachment(r: _Reader, obj: dict, ordinal: int, ts) -> None:
    attachment = obj.get("attachment") or {}
    atype = attachment.get("type")
    if atype == "hook_additional_context":
        for value in _strings(attachment):
            r.identity.observe_text(value)
    elif atype == "instructions":
        for entry in attachment.get("files") or []:
            if isinstance(entry, dict) and str(entry.get("path") or "").endswith(
                    ("/.claude/CLAUDE.md", "/AGENTS.md")):
                r.identity.observe_loaded_instructions(str(entry.get("content") or ""))
    elif atype == "queued_command":
        prompt = str(attachment.get("prompt") or "")
        origin = attachment.get("origin") if isinstance(attachment.get("origin"), dict) else {}
        human = attachment.get("humanTurn") is True or origin.get("kind") == "human"
        kind = "genuine" if human and attachment.get("commandMode") in (None, "prompt") \
            else "synthetic"
        native = attachment.get("source_uuid") or obj.get("uuid") or f"ordinal:{ordinal}"
        if kind == "genuine":
            r.turn = f"{HARNESS}:{native}"
        _submission(r, native, ordinal, iso_ts(attachment.get("timestamp")) or ts,
                    kind, prompt)


def _system(r: _Reader, obj: dict, ordinal: int, ts) -> None:
    subtype = obj.get("subtype")
    native = obj.get("uuid") or f"ordinal:{ordinal}"
    if subtype == "compact_boundary":
        r.stats["compactions"] += 1
        meta = obj.get("compactMetadata") or {}
        r.event("compaction", native, ordinal, ts, name="compact_boundary",
                detail={"trigger": meta.get("trigger"),
                        "pre_tokens": meta.get("preTokens")})
    elif subtype == "turn_duration":
        r.event("lifecycle", native, ordinal, ts, name="turn_duration",
                duration_ms=obj.get("durationMs"),
                detail={"message_count": obj.get("messageCount")})
    elif subtype in ("api_error", "stop_hook_summary", "informational"):
        r.event("lifecycle", native, ordinal, ts, name=subtype,
                status=obj.get("level"),
                detail={"excerpt": str(obj.get("content") or "")[:300]})
