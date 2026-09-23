"""Ledger privacy: the single implementation of the privacy spec rules 1-6.

Every adapter and writer routes through this module; no private copies of
these rules exist elsewhere. Fail closed: when in doubt, store less.

Rule 1: submissions.text_excerpt is empty unless the submission is a genuine
human submission of the main session. For a genuine submission, keep the
human text only up to the first tag-like marker (a '<' followed by a letter,
'/' or '!', or the first '<<<'). No tag parsing, so quoted '>' and nesting
edge cases truncate at the marker. Collapse whitespace, keep 300 chars.
Rule 2: assistant excerpts keep the last 400 characters of the assistant's
own message text, only when that span holds no tag-like marker and no '<<<'.
Rule 3: PRIVACY_VERSION records the rules a source was imported under.
Rule 4: import_errors.error is one closed category, else the fallback.
Rule 5: import_errors.line_excerpt holds only sorted top-level key names.
Rule 6: events.detail_json keeps only allowlisted keys with typed values.
"""

from __future__ import annotations

import json
import re

# Rule 3: bump when any rule in this module changes meaning. A stored source
# version that differs forces a full re-import with in-place correction.
PRIVACY_VERSION = 2

SUBMISSION_EXCERPT_CHARS = 300
ASSISTANT_EXCERPT_CHARS = 400
LINE_EXCERPT_CHARS = 200
DETAIL_JSON_CHARS = 4000

# Rule 1/2 marker: '<' followed by a letter (str.isalpha, so Unicode
# numerals such as U+2460 are not letters), '/' or '!', or '<<<'. Plain
# '<' before whitespace, digits, punctuation or the end of text is kept,
# as is a bare '<<'.
_WS_RE = re.compile(r"\s+")


def _marker_at(text: str, index: int) -> bool:
    """Whether a tag-like marker starts at text[index] (which holds '<')."""
    if text.startswith("<<<", index):
        return True
    nxt = index + 1
    if nxt >= len(text):
        return False
    ch = text[nxt]
    return ch == "/" or ch == "!" or ch.isalpha()


def _marker_pos(text: str) -> int | None:
    """Offset of the first tag-like marker in text, or None when absent."""
    start = 0
    while True:
        idx = text.find("<", start)
        if idx == -1:
            return None
        if _marker_at(text, idx):
            return idx
        start = idx + 1


def contains_marker(value: object) -> bool:
    """Whether a string holds a tag-like marker or '<<<' anywhere."""
    return isinstance(value, str) and _marker_pos(value) is not None


def submission_excerpt(text: object, *, is_genuine: bool,
                       is_main_session: bool = True) -> str:
    """Rule 1 excerpt: '' unless a genuine main-session human submission."""
    if not is_genuine or not is_main_session:
        return ""
    if not isinstance(text, str) or not text:
        return ""
    match = _marker_pos(text)
    head = text[:match] if match is not None else text
    return _WS_RE.sub(" ", head).strip()[:SUBMISSION_EXCERPT_CHARS]


def is_main_session(*, thread_source: object = None,
                    session_id: object = None, thread_id: object = None,
                    observed_thread_ids: object = ()) -> bool:
    """Rule 1 main-session gate shared by every adapter; fail closed.

    True only when the source proves a human-owned main thread: the native
    thread/source metadata says thread_source is exactly "user", and the
    native thread identity is a non-empty string equal to session_id, as is
    every observed own-thread identity. A divergent own thread means a
    child or worker rollout; a missing or mistyped thread identity means
    unknown. Both yield False, so no excerpt is stored — including a
    partial import that knows session_id and thread_source but not yet the
    thread identity. Referenced worker threads (spawn targets recorded
    beside a main thread) are not own-thread identities and must never be
    passed here; only the rollout's own thread identity counts.
    """
    if thread_source != "user":
        return False
    if not isinstance(session_id, str) or not session_id:
        return False
    if not isinstance(thread_id, str) or not thread_id:
        return False
    if thread_id != session_id:
        return False
    if isinstance(observed_thread_ids, (list, tuple, set, frozenset)):
        observed = list(observed_thread_ids)
    elif observed_thread_ids is None:
        observed = []
    else:
        return False
    for tid in observed:
        if not isinstance(tid, str) or not tid or tid != session_id:
            return False
    return True


def assistant_excerpt(text: object) -> str:
    """Rule 2 excerpt: last 400 chars of assistant text, or '' on markers."""
    if not isinstance(text, str) or not text:
        return ""
    span = text[-ASSISTANT_EXCERPT_CHARS:]
    if _marker_pos(span) is not None:
        return ""
    return span


# Rule 4: the closed error category set. Anything else maps to the fallback
# ERROR_FALLBACK, so exception names, messages and record values never land
# in import_errors.error.
ERROR_CATEGORIES = frozenset({
    "malformed_json",
    "unknown_record",
    "schema_error",
    "missing_id",
    "malformed_usage",
    "usage_conflict",
    "source_unreadable",
    "unsupported_schema",
})
ERROR_FALLBACK = "import_error"


def error_category(value: object) -> str:
    """Exactly one closed category, or the fixed fallback."""
    if isinstance(value, str) and value in ERROR_CATEGORIES:
        return value
    return ERROR_FALLBACK


def line_excerpt(line: object) -> str:
    """Rule 5: sorted top-level key names of a JSON object, else ''.

    Names, never values; at most LINE_EXCERPT_CHARS characters. Any
    non-object record (bad JSON, lists, scalars) yields an empty excerpt.
    """
    if not isinstance(line, str) or not line.strip():
        return ""
    try:
        obj = json.loads(line)
    except ValueError:
        return ""
    if not isinstance(obj, dict):
        return ""
    return ",".join(sorted(str(k) for k in obj))[:LINE_EXCERPT_CHARS]


# Rule 6: per-event-family detail allowlist. Allowed keys per family are
# exactly the fields analysis.py, report.py and cli.py read:
# - file_change.paths: analysis._changed_paths (target plus detail paths)
# - tool_result.exit_code/exitCode: analysis._failed
# - read.start_line/num_lines/cmd: analysis._repeated_reads grouping key
# - skill_read.start_line/num_lines/cmd/skill: repeated reads plus
#   analysis._repeated_skills skill identity
# - assistant_message.excerpt: analysis._permission_seeking
# Every other family keeps no detail. Values may be numbers, booleans,
# fixed-length hex hashes, native identifiers of the expected type, file
# paths, shell commands, or closed status/kind strings; anything else is
# dropped. Never titles, messages, error text, outputs, content, arguments
# or other free text.
EVENT_DETAIL_ALLOWLIST: dict[str, dict[str, str]] = {
    "file_change": {"paths": "paths"},
    "tool_result": {"exit_code": "int", "exitCode": "int"},
    "read": {"start_line": "int", "num_lines": "int", "cmd": "command"},
    "skill_read": {"start_line": "int", "num_lines": "int", "cmd": "command",
                   "skill": "identifier"},
    "assistant_message": {"excerpt": "excerpt"},
    "tool_call": {},
    "skill_invoke": {},
    "compaction": {},
    "lifecycle": {},
    "permission": {},
}

_TARGET_CHARS = 500


def filter_target(target: object) -> str | None:
    """Rule 6 targets: keep strings (paths, commands, identifiers).

    Non-strings are rejected, never coerced; kept strings are bounded.
    """
    if isinstance(target, str):
        return target[:_TARGET_CHARS]
    return None


def _valid_int(value: object) -> bool:
    return type(value) is int


def _valid_identifier(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value[:_TARGET_CHARS]
    return None


def _valid_command(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value[:_TARGET_CHARS]
    return None


def _valid_excerpt(value: object) -> str | None:
    """Rule 2, enforced here as well as in assistant_excerpt.

    The filter keeps the last ASSISTANT_EXCERPT_CHARS characters of the
    given text, and only when that span holds no tag-like marker and no
    '<<<'. An overlong or untrimmed value passed directly is reduced to
    the rule-2 span instead of persisting verbatim.
    """
    if not isinstance(value, str) or not value:
        return None
    span = value[-ASSISTANT_EXCERPT_CHARS:]
    if _marker_pos(span) is not None:
        return None
    return span


def _valid_paths(value: object) -> list[str] | None:
    if isinstance(value, dict):
        candidates = [k for k in value.keys() if isinstance(k, str) and k]
    elif isinstance(value, (list, tuple)):
        candidates = [p for p in value if isinstance(p, str) and p]
    else:
        return None
    return sorted({p[:_TARGET_CHARS] for p in candidates}) or None


def filter_detail(family: str, detail: object) -> dict:
    """Rule 6 detail filter: allowlisted keys with correctly typed values."""
    if not isinstance(detail, dict):
        return {}
    allowed = EVENT_DETAIL_ALLOWLIST.get(family)
    if not allowed:
        return {}
    out: dict = {}
    for key, kind in allowed.items():
        if key not in detail:
            continue
        value = detail[key]
        if kind == "int":
            if _valid_int(value):
                out[key] = value
        elif kind == "identifier":
            kept = _valid_identifier(value)
            if kept is not None:
                out[key] = kept
        elif kind == "command":
            kept = _valid_command(value)
            if kept is not None:
                out[key] = kept
        elif kind == "excerpt":
            kept = _valid_excerpt(value)
            if kept is not None:
                out[key] = kept
        elif kind == "paths":
            kept = _valid_paths(value)
            if kept is not None:
                out[key] = kept
    return out


# Rule 6, event identity: every family expects a native identifier of one
# type only — a non-empty string. Anything else (None, numbers, dicts,
# lists) is invalid, never stringified into the ledger; the central event
# writer quarantines such an event as missing_id.
def filter_native_id(family: str, value: object) -> str | None:
    """The validated native event id, or None when it has the wrong type."""
    _ = family
    if isinstance(value, str) and value:
        return value
    return None


# Rule 6, event names: closed per-family enums of canonical name/kind
# values. The members are exactly what the adapters emit as protocol
# tokens (message and compaction markers, lifecycle kinds, response-item
# types, fixed dynamic-tool mappings, unknown-kind fallbacks) plus the
# command-tool kinds the analysis detectors classify on. Native instance
# values — tool and skill names, file basenames, server or tool paths such
# as secret_token, unknown_tool, dynamic.secret or AGENTS.md — are never
# members, so they fail closed to NULL even when identifier-shaped. Event
# identity still travels in native_id, and paths/commands in target.
EVENT_NAME_ENUMS: dict[str, frozenset] = {
    "assistant_message": frozenset({"assistant_message"}),
    "compaction": frozenset({"context_compaction", "compact_boundary"}),
    "lifecycle": frozenset({
        "task_complete", "turn_aborted", "turn_duration",
        "subagent_activity", "thread_goal_updated", "api_error",
        "stop_hook_summary", "informational", "Plan", "HookPrompt",
        "EnteredReviewMode", "ExitedReviewMode", "collab.unknown",
    }),
    "tool_call": frozenset(),
    "tool_result": frozenset({
        "bash", "Bash", "exec", "shell", "run_terminal_cmd", "run_command",
        "function_call_output", "custom_tool_call_output",
        "image_view", "web_search", "mcp.unknown", "dynamic.unknown",
        "unknown",
    }),
    "file_change": frozenset({"file_change"}),
    "read": frozenset(),
    "skill_read": frozenset(),
    "skill_invoke": frozenset(),
    "permission": frozenset(),
}


def filter_event_name(family: str, value: object) -> str | None:
    """The validated event name, or None for a wrong type/unknown value."""
    if not isinstance(value, str) or not value:
        return None
    allowed = EVENT_NAME_ENUMS.get(family)
    if not allowed:
        return None
    if value in allowed:
        return value
    return None


# Rule 6, event statuses: closed per-family enums. None stays None (no
# status); any other value of the wrong type or outside the family set is
# dropped, never stringified or passed through.
_COMMON_STATUS = frozenset({
    "ok", "error", "denied", "completed", "success", "failed", "failure",
})
EVENT_STATUS_ENUMS: dict[str, frozenset] = {
    "tool_call": _COMMON_STATUS | frozenset({"cancelled"}),
    "tool_result": _COMMON_STATUS | frozenset({"cancelled"}),
    "read": _COMMON_STATUS | frozenset({"cancelled"}),
    "skill_read": _COMMON_STATUS | frozenset({"cancelled"}),
    "lifecycle": frozenset(
        {"completed", "cancelled", "denied", "ok", "error"}),
    "permission": frozenset({"denied"}),
    "file_change": frozenset(),
    "compaction": frozenset(),
    "assistant_message": frozenset(),
    "skill_invoke": frozenset(),
}


def filter_event_status(family: str, value: object) -> str | None:
    """The validated event status, or None when absent or not allowed."""
    if value is None:
        return None
    allowed = EVENT_STATUS_ENUMS.get(family)
    if not allowed:
        return None
    if isinstance(value, str) and value in allowed:
        return value
    return None
