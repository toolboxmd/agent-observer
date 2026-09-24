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

from .. import db, privacy
from ..identity import SessionIdentity, skill_from_path
from ..ingest import (JsonlSource, MissingNativeId, fingerprint, insert_event,
                      iso_ts, text_hash)

HARNESS = "claude"
SEMANTICS = "claude:input_excludes_cache,output_includes_thinking"
DEFAULT_ROOT = os.path.expanduser("~/.claude/projects")
SCAFFOLD_PREFIXES = ("<command-name>", "<local-command", "<system-reminder>",
                     "Caveat:", "<task-notification>", "<bash-",
                     "<hook", "<stop-hook")
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

# Benign native metadata records: recognized as known and ignored. Their
# contents are never stored; only the session/timing fields read before
# dispatch (identifiers, never free text) are kept. Anything not listed
# here still raises _UnsupportedSchema, so arbitrary future record types
# stay quarantined instead of being silently dropped.
#
# last-prompt stays here deliberately: it is known duplicate state that
# repeats the latest human prompt already carried by user and
# queue-operation records, which are the canonical prompt evidence. It
# stores nothing.
#
# cost-state is NOT ignored: it carries per-model token counters whose
# reconciliation with assistant usage is an open follow-up, so it stays
# quarantined as unsupported_schema (nothing stored) instead of being
# silently dropped.
IGNORED_METADATA_TYPES = frozenset({
    "agent-name",
    "atis-latch",
    "bridge-session",
    "custom-title",
    "file-history-delta",
    "file-history-snapshot",
    "last-prompt",
    "mode",
    "permission-mode",
    "pr-link",
})


class _MalformedUsage(ValueError):
    """A usage bucket that is not NULL or a real integer counter."""


class _UsageConflict(ValueError):
    """An exact-ID repeat whose counters differ from the stored final row."""


class _UnsupportedSchema(ValueError):
    """A record type the adapter does not support."""


_CLAUDE_COUNTER_KEYS = ("input_tokens", "cache_creation_input_tokens",
                        "cache_read_input_tokens", "output_tokens")


def _valid_counter(value) -> bool:
    """Only NULL or a real integer counter; booleans are malformed."""
    if value is None:
        return True
    return isinstance(value, int) and not isinstance(value, bool)


_FINALITY_TABLE = "claude_response_finality"


def _ensure_finality_table(con) -> None:
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {_FINALITY_TABLE}"
        "(response_id TEXT PRIMARY KEY, finalized_at REAL)")


def _is_finalized(con, rid: str) -> bool:
    try:
        row = con.execute(
            f"SELECT 1 FROM {_FINALITY_TABLE} WHERE response_id=?",
            (rid,)).fetchone()
    except sqlite3.DatabaseError:
        return False
    return row is not None


def _mark_finalized(con, rid: str) -> None:
    import time as _time
    con.execute(
        f"INSERT OR IGNORE INTO {_FINALITY_TABLE}"
        "(response_id, finalized_at)"
        " VALUES(?, ?)", (rid, _time.time()))


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
    """A string target only: paths, commands and skill names.

    Non-string native values never stringify into the ledger; the caller
    then stores no target. Search patterns and URLs are free text and are
    not targets.
    """
    if not isinstance(tool_input, dict):
        return None
    for key in ("file_path", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    command = tool_input.get("command")
    if isinstance(command, str) and command:
        return command[:500]
    skill = tool_input.get("skill")
    if isinstance(skill, str) and skill:
        return skill
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
                     turn_id=self.turn, update=self.src.privacy_stale, **kw)


def import_claude_file(con: sqlite3.Connection, path: str,
                       full: bool = False) -> dict:
    stats = {"lines": 0, "responses_inserted": 0, "responses_duplicate": 0,
             "responses_updated": 0,
             "submissions_inserted": 0, "events_inserted": 0,
             "events_duplicate": 0, "compactions": 0, "malformed": 0}
    src = JsonlSource(con, HARNESS, path, full=full)
    r = _Reader(con, src, stats)
    _ensure_finality_table(con)
    # Durable per-response finality is authoritative for accepted final
    # rows. It covers copied sources, full reimports and incremental
    # appends. The file prefix is never authority on its own, so a malformed
    # final prefix can never finalize a response before any row exists.
    try:
        for row in con.execute(
                f"SELECT response_id FROM {_FINALITY_TABLE}"):
            rid = row["response_id"] if "response_id" in row.keys() else row[0]
            if isinstance(rid, str) and rid.startswith(f"{HARNESS}:"):
                r.finalized.add(rid[len(HARNESS) + 1:])
    except sqlite3.DatabaseError:
        pass
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
            src.error(ordinal,
                      "malformed_json" if obj is None else "unknown_record",
                      line)
            continue
        try:
            _ingest(r, obj, ordinal)
        except _UsageConflict:
            stats["malformed"] += 1
            src.error(ordinal, "usage_conflict", line)
        except _MalformedUsage:
            stats["malformed"] += 1
            src.error(ordinal, "malformed_usage", line)
        except MissingNativeId:
            stats["malformed"] += 1
            src.error(ordinal, "missing_id", line)
        except _UnsupportedSchema:
            stats["malformed"] += 1
            src.error(ordinal, "unsupported_schema", line)
        except (KeyError, TypeError, ValueError, AttributeError):
            stats["malformed"] += 1
            src.error(ordinal, "schema_error", line)
    if r.native_session:
        fields = {"started_at": r.first_ts, "ended_at": r.last_ts, **r.meta,
                  **r.identity.fields(con)}
        if r.agent_id:
            fields["parent_session_key"] = f"{HARNESS}:{r.native_session}"
            fields["role"] = "subagent"
        db.upsert_session(con, r.session_key, HARNESS,
                          r.session_key.split(":", 1)[1], src.source_id, **fields)
        if src.privacy_stale:
            # Rule 7: native free-text titles are never stored; a version
            # change clears any title an older import kept.
            con.execute("UPDATE sessions SET title=NULL WHERE session_key=?",
                        (r.session_key,))
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
    elif kind == "queue-operation":
        _queue_operation(r, obj, ordinal, ts)
    elif kind == "system":
        _system(r, obj, ordinal, ts)
    elif kind == "ai-title":
        # Rule 7: native free-text titles are discarded, never stored.
        return
    elif kind in IGNORED_METADATA_TYPES:
        # Known benign metadata: recognized, never stored, never an error.
        return
    else:
        raise _UnsupportedSchema(f"unsupported record type: {kind!r}")


def _usage_values(usage) -> dict:
    """Native usage buckets with unknown preserved as NULL.

    Only NULL or real integer counters are accepted; booleans, strings,
    lists and other shapes are malformed and quarantined before any SQL.
    Only recognized numeric counters are validated; string metadata such as
    service_tier is ignored. A usage object with no recognized numeric
    counter is malformed and never creates a response row or finality.
    total_tokens is the harness's own total and is only known when every
    bucket is known; a partial sum from some buckets is never constructed."""
    if not isinstance(usage, dict):
        raise _MalformedUsage("malformed usage")
    details_raw = usage.get("output_tokens_details")
    if details_raw is None:
        details: dict = {}
    elif not isinstance(details_raw, dict):
        raise _MalformedUsage("malformed usage details")
    else:
        details = details_raw
    for key in _CLAUDE_COUNTER_KEYS:
        if not _valid_counter(usage.get(key)):
            raise _MalformedUsage("malformed usage counter")
    thinking = details.get("thinking_tokens")
    if not _valid_counter(thinking):
        raise _MalformedUsage("malformed usage counter")
    # service_tier and any other unrecognized metadata are ignored: they
    # never validate or invalidate a usage block.
    if not any(k in usage for k in _CLAUDE_COUNTER_KEYS) and \
            "thinking_tokens" not in details:
        raise _MalformedUsage("malformed usage")
    values = [usage.get(k) for k in _CLAUDE_COUNTER_KEYS]
    return {
        "input_tokens": usage.get("input_tokens"),
        "cached_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_write_input_tokens": usage.get("cache_creation_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_output_tokens": details.get("thinking_tokens"),
        "total_tokens": sum(values)
        if all(type(v) is int for v in values) else None,
    }


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
    is still streaming and non-final. Finality persists per response in a
    durable adapter table keyed by response_id, so a stale copied source or
    a full reimport can never overwrite a finalized row: an exact later
    duplicate is a no-op and any differing counters are quarantined. A
    malformed final block never reaches here, so it never finalizes."""
    rid = f"{HARNESS}:{mid}"
    if mid in r.finalized or _is_finalized(r.con, rid):
        r.finalized.add(mid)
        seen = _stored_counters(r, rid)
        if seen != values:
            raise _UsageConflict("usage conflict")
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
            _mark_finalized(r.con, rid)
            r.finalized.add(mid)
        return
    if _stored_counters(r, rid) == values:
        r.stats["responses_duplicate"] += 1
        if final:
            _mark_finalized(r.con, rid)
            r.finalized.add(mid)
        return
    # The message is still streaming and non-final (or its final block just
    # arrived): the latest native block is the authority, across imports as
    # well. Final rows never reach this update path.
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
        _mark_finalized(r.con, rid)
        r.finalized.add(mid)


def _assistant(r: _Reader, obj: dict, ordinal: int, ts) -> None:
    message = obj.get("message")
    if message is None:
        message = {}
    if message and not isinstance(message, dict):
        raise ValueError("unsupported message shape")
    if not isinstance(message, dict):
        raise ValueError("unsupported message shape")
    mid = message.get("id")
    model = message.get("model")
    if mid and "usage" in message and model != "<synthetic>":
        usage_raw = message.get("usage")
        if usage_raw is None or (isinstance(usage_raw, dict) and not usage_raw):
            pass
        else:
            # Validated before any SQL; malformed buckets quarantine here and
            # never finalize, so a later valid block still inserts normally.
            values = _usage_values(usage_raw)
            _ingest_usage_row(r, obj, ordinal, ts, mid, model, values,
                              message.get("stop_reason") is not None)
    for index, block in enumerate(message.get("content") or []):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use" and block.get("id"):
            raw_name = block.get("name")
            name = raw_name if isinstance(raw_name, str) and raw_name \
                else "unknown"
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            target = _target(name, tool_input)
            r.pending[block["id"]] = (name, tool_input)
            r.event("tool_call", block["id"], ordinal, ts, name=name, target=target,
                    fingerprint=fingerprint(name, json.dumps(tool_input, sort_keys=True)[:4000]))
            if name == "Skill":
                raw_skill = tool_input.get("skill") or tool_input.get("command")
                skill = raw_skill if isinstance(raw_skill, str) and raw_skill \
                    else "unknown"
                r.event("skill_invoke", block["id"], ordinal, ts, name=skill,
                        target=skill)
            if target and name in EDIT_TOOLS:
                r.event("file_change", block["id"], ordinal, ts, name=name,
                        target=target, fingerprint=fingerprint(target))
        elif btype == "text" and block.get("text"):
            text = block["text"]
            excerpt = privacy.assistant_excerpt(text)
            r.event("assistant_message", f"{obj.get('uuid') or mid}:{index}", ordinal, ts,
                    name="assistant_message", size_bytes=len(text),
                    fingerprint=text_hash(text),
                    detail={"excerpt": excerpt} if excerpt else None)


def _submission(r: _Reader, native_id, ordinal, ts, kind: str, text: str) -> None:
    # Privacy rule 1 via agent_observer/privacy.py: only a genuine
    # main-session human submission keeps an excerpt. Interrupt, synthetic,
    # command and scaffolding kinds keep an empty excerpt, as does any text
    # from a child sub-agent session or from a record with no native
    # session identity (fail closed when unknown), so file and preference
    # contents can never persist. Identity extraction already saw the
    # complete text.
    excerpt = privacy.submission_excerpt(
        text, is_genuine=kind == "genuine",
        is_main_session=r.agent_id is None and r.native_session is not None)
    genuine = 1 if kind == "genuine" else 0
    cur = r.con.execute(
        "INSERT OR IGNORE INTO submissions(native_id, source_id, session_key,"
        " turn_id, ordinal_num, ts, kind, text_hash, text_excerpt, is_genuine)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"{HARNESS}:{native_id}", r.src.source_id, r.session_key, r.turn, ordinal,
         ts, kind, text_hash(text), excerpt, genuine))
    if cur.rowcount:
        r.stats["submissions_inserted"] += 1
    elif r.src.privacy_stale:
        # Rule 3: a privacy version change corrects rows in place.
        r.con.execute(
            "UPDATE submissions SET source_id=?, session_key=?, turn_id=?,"
            " ordinal_num=?, ts=?, kind=?, text_hash=?, text_excerpt=?,"
            " is_genuine=? WHERE native_id=?",
            (r.src.source_id, r.session_key, r.turn, ordinal, ts, kind,
             text_hash(text), excerpt, genuine, f"{HARNESS}:{native_id}"))
        r.stats["submissions_updated"] = \
            r.stats.get("submissions_updated", 0) + 1


def _queue_native(r: _Reader, ordinal: int) -> str:
    """Stable deterministic identity for one enqueue record.

    queue-operation records carry no native uuid, so the identity is the
    session plus the structural source ordinal: deterministic across full
    and incremental reimports of the append-only file.
    """
    sess = r.native_session or f"file:{os.path.basename(r.src.path)}"
    return f"queue:{sess}:{ordinal}"


def _queue_operation(r: _Reader, obj: dict, ordinal: int, ts) -> None:
    """One mid-turn typed prompt from a queue-operation record.

    Only operation enqueue with a string prompt payload (the real native
    shape is a plain `content` string) may create a submission. It is a
    genuine human submission typed mid-turn in the main session: the text
    is routed through privacy.submission_excerpt and only the safe
    excerpt plus text_hash/kind/identifiers persist. dequeue, remove and
    every other operation store nothing and create no import error, as
    does an enqueue without prompt text (fail closed).
    """
    if obj.get("operation") != "enqueue":
        return
    content = obj.get("content")
    if not isinstance(content, str) or not content:
        return
    native = _queue_native(r, ordinal)
    full = f"{HARNESS}:{native}"
    # Already merged into its user record (reimport): no-op.
    if r.con.execute("SELECT 1 FROM submissions WHERE alias_id=?",
                     (full,)).fetchone():
        return
    # Reverse ordering: the user record this prompt became already
    # imported in this turn. Fold the queue identity into it instead of
    # adding a second row. Only a never-merged user row qualifies, so a
    # repeated identical prompt in a later turn keeps its own row.
    if r.turn is not None:
        user = r.con.execute(
            "SELECT native_id FROM submissions"
            " WHERE session_key=? AND text_hash=? AND turn_id=?"
            " AND alias_id IS NULL AND native_id NOT LIKE ?"
            " ORDER BY ordinal_num LIMIT 1",
            (r.session_key, text_hash(content), r.turn,
             f"{HARNESS}:queue:%")).fetchone()
        if user is not None:
            r.con.execute("UPDATE submissions SET alias_id=? WHERE native_id=?",
                          (full, user["native_id"]))
            r.stats["submissions_dedup"] = \
                r.stats.get("submissions_dedup", 0) + 1
            return
    # The queued prompt opens its turn, mirroring attachment queued_command.
    r.turn = f"{HARNESS}:{native}"
    _submission(r, native, ordinal, ts, "genuine", content)


def _merge_queue_record(r: _Reader, obj: dict, native: str, ordinal: int,
                        ts, kind: str, text: str) -> bool:
    """Fold a user record into its earlier enqueue row. True when merged.

    The enqueue this prompt was typed as is the unmerged queue row with
    the same session, text hash and turn (the turn the enqueue opened,
    still current). The merged row keeps the canonical user native id
    and retains the queue identity in alias_id without adding a second
    row, so each prompt counts once; a queue-only prompt keeps its row.
    The user record is the canonical kind authority, so its
    classification wins on merge while the excerpt is recomputed from
    the same text through the same privacy gate.
    """
    full = f"{HARNESS}:{native}"
    if r.con.execute("SELECT 1 FROM submissions WHERE native_id=?",
                     (full,)).fetchone():
        # Canonical row already exists (reimport): the normal path keeps
        # it a no-op or corrects it in place under a stale version.
        return False
    if r.turn is None:
        return False
    row = r.con.execute(
        "SELECT native_id FROM submissions"
        " WHERE session_key=? AND text_hash=? AND turn_id=?"
        " AND native_id LIKE ? ORDER BY ordinal_num LIMIT 1",
        (r.session_key, text_hash(text), r.turn,
         f"{HARNESS}:queue:%")).fetchone()
    if row is None:
        return False
    queue_native = row["native_id"]
    new_turn = f"{HARNESS}:{obj.get('promptId') or native}" \
        if kind == "genuine" else r.turn
    excerpt = privacy.submission_excerpt(
        text, is_genuine=kind == "genuine",
        is_main_session=r.agent_id is None and r.native_session is not None)
    r.con.execute(
        "UPDATE submissions SET native_id=?, alias_id=?, source_id=?,"
        " session_key=?, turn_id=?, ordinal_num=?, ts=?, kind=?,"
        " text_hash=?, text_excerpt=?, is_genuine=? WHERE native_id=?",
        (full, queue_native, r.src.source_id, r.session_key, new_turn,
         ordinal, ts, kind, text_hash(text), excerpt,
         1 if kind == "genuine" else 0, queue_native))
    if kind == "genuine":
        r.turn = new_turn
    r.stats["submissions_dedup"] = r.stats.get("submissions_dedup", 0) + 1
    return True


def _user_kind(r: _Reader, obj: dict, text: str) -> str:
    if obj.get("isSidechain") or r.agent_id:
        return "synthetic"
    stripped = text.lstrip()
    if stripped.startswith(INTERRUPT_PREFIX):
        return "interrupt"
    # Known synthetic and scaffolding markers are rejected before any
    # human-origin metadata is accepted. A contradictory record carrying
    # origin.kind=human or promptSource=typed is never genuine and keeps an
    # empty excerpt (fail closed on privacy).
    if stripped.startswith(SKILL_BASE_PREFIX):
        # A loaded skill body arrives as message text: skill-load evidence,
        # never a genuine human submission.
        return "scaffolding"
    if obj.get("isMeta") or stripped.startswith(SCAFFOLD_PREFIXES):
        return "command" if stripped.startswith("<command-name>") else "scaffolding"
    origin = obj.get("origin") if isinstance(obj.get("origin"), dict) else {}
    if origin.get("kind") == "human" or obj.get("promptSource") == "typed":
        return "genuine"
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
                detail={"skill": skill_from_path(base + "/")})
    kind = _user_kind(r, obj, text)
    native = obj.get("uuid") or f"ordinal:{ordinal}"
    if _merge_queue_record(r, obj, native, ordinal, ts, kind, text):
        return
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
    detail = {"exit_code": structured.get("exit_code"),
              "exitCode": structured.get("exitCode")}
    r.event("tool_result", call_id, ordinal, ts, name=name or "unknown",
            target=_target(name or "", tool_input), status=status, size_bytes=size,
            detail=detail)
    if denied:
        r.event("permission", call_id, ordinal, ts, name=name or "unknown",
                status="denied")
    file_info = structured.get("file") if isinstance(structured.get("file"), dict) else None
    path = (file_info or {}).get("filePath") or (tool_input or {}).get(READ_TOOLS.get(name or "", ""), None)
    if not isinstance(path, str):
        path = None
    if name in READ_TOOLS and path and status == "ok":
        r.identity.observe_path(path)
        family = "skill_read" if skill_from_path(path) else "read"
        start = (file_info or {}).get("startLine")
        lines = (file_info or {}).get("numLines")
        r.event(family, f"{call_id}:read", ordinal, ts, name=os.path.basename(path),
                target=path, size_bytes=size,
                fingerprint=fingerprint(path, start, lines),
                detail={"start_line": start, "num_lines": lines,
                        "skill": skill_from_path(path)})


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
        prompt = attachment.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            return
        stripped = prompt.lstrip()
        # Scaffolding markers fail closed before human metadata: a queued
        # prompt that is a known marker is never genuine even when it
        # carries humanTurn or origin.kind=human.
        if stripped.startswith(SCAFFOLD_PREFIXES) or stripped.startswith(
                (SKILL_BASE_PREFIX, INTERRUPT_PREFIX)):
            kind = "interrupt" if stripped.startswith(
                INTERRUPT_PREFIX) else "synthetic"
        else:
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
        r.event("compaction", native, ordinal, ts, name="compact_boundary")
    elif subtype == "turn_duration":
        r.event("lifecycle", native, ordinal, ts, name="turn_duration",
                duration_ms=obj.get("durationMs"))
    elif subtype in ("api_error", "stop_hook_summary", "informational"):
        r.event("lifecycle", native, ordinal, ts, name=subtype,
                status=obj.get("level"))
