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
PRIVACY_VERSION = 1

SUBMISSION_EXCERPT_CHARS = 300
ASSISTANT_EXCERPT_CHARS = 400
LINE_EXCERPT_CHARS = 200
DETAIL_JSON_CHARS = 4000

# Rule 1/2 marker: '<' followed by a letter (Unicode-aware), '/' or '!',
# or '<<<'. Plain '<' before whitespace, digits, punctuation or the end of
# text is kept, as is a bare '<<'.
_TAG_LIKE_RE = re.compile(r"<[^\W\d_]|</|<!|<<<")
_WS_RE = re.compile(r"\s+")


def submission_excerpt(text: object, *, is_genuine: bool,
                       is_main_session: bool = True) -> str:
    """Rule 1 excerpt: '' unless a genuine main-session human submission."""
    if not is_genuine or not is_main_session:
        return ""
    if not isinstance(text, str) or not text:
        return ""
    match = _TAG_LIKE_RE.search(text)
    head = text[:match.start()] if match else text
    return _WS_RE.sub(" ", head).strip()[:SUBMISSION_EXCERPT_CHARS]


def assistant_excerpt(text: object) -> str:
    """Rule 2 excerpt: last 400 chars of assistant text, or '' on markers."""
    if not isinstance(text, str) or not text:
        return ""
    span = text[-ASSISTANT_EXCERPT_CHARS:]
    if _TAG_LIKE_RE.search(span) or "<<<" in span:
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
    if _TAG_LIKE_RE.search(span) or "<<<" in span:
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
