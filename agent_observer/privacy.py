"""Ledger privacy: the single implementation of the privacy spec rules 1-6 and 8.

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
Rule 7 (docs/contracts.md) covers tables not named here.
Rule 8: events.target keeps a tool call's full path or command up to
ARGUMENT_CHARS, with known secret patterns redacted, apply_patch hunk
bodies reduced to their file header lines, and file bodies written by a
heredoc, echo or printf omitted.
"""

from __future__ import annotations

import json
import re

# Rule 3: bump when any rule in this module changes meaning. A stored source
# version that differs forces a full re-import with in-place correction.
PRIVACY_VERSION = 7

SUBMISSION_EXCERPT_CHARS = 300
ASSISTANT_EXCERPT_CHARS = 400
LINE_EXCERPT_CHARS = 200
DETAIL_JSON_CHARS = 4000

# Rule 1/2 marker: '<' followed by a letter (str.isalpha, so Unicode
# numerals such as U+2460 are not letters), '/' or '!', or '<<<'. Plain
# '<' before whitespace, digits, punctuation or the end of text is kept,
# as is a bare '<<'.
_WS_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


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
# - skill_read.start_line/num_lines/cmd/skill/skill_path: repeated reads
#   plus analysis._repeated_skills skill identity (identifier in skill,
#   installed file path in skill_path, never in target)
# - skill_invoke.skill_path: installed SKILL.md path when the native
#   invocation supplies a directory, otherwise omitted
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
                   "skill": "identifier", "skill_path": "path"},
    "assistant_message": {"excerpt": "excerpt"},
    "tool_call": {},
    "skill_invoke": {"skill_path": "path"},
    "compaction": {},
    "lifecycle": {},
    "permission": {},
}

_TARGET_CHARS = 500

# Rule 8: one target argument (a path or a full shell command, including a
# Codex code-mode exec program) keeps up to 64 KiB. The longest real values
# measured on 2026-09-29 over 14 days of local sessions were 42,102 chars
# (Claude Bash) and 54,843 chars (Codex exec); p99 was under 8,000.
ARGUMENT_CHARS = 65536

REDACTED = "[redacted]"

# Rule 8: known secret shapes. Each match is replaced by REDACTED; for
# assignments and flags the name survives and only the value is replaced.
_SECRET_RES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
               r"(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bA(?:KIA|SIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)
_SECRET_VALUE_RES = (
    # Uppercase NAME=value (environment style) where NAME mentions a
    # token, secret, password or key.
    re.compile(r"(\b[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY"
               r"|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIALS?)[A-Z0-9_]*=)"
               r"(\"[^\"]*\"|'[^']*'|[^\s;&|]+)"),
    # Exact lowercase keys such as "api_key": "..." or password=...
    re.compile(r"(?i)((?<![A-Za-z0-9_])[\"']?(?:api_?key|access_?token"
               r"|refresh_?token|client_?secret|secret|password|passwd|token)"
               r"[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&|}]+)"),
    # --token value, --password=value and similar flags.
    re.compile(r"(?i)(--?(?:token|secret|password|passwd|api-?key"
               r"|access-?key|auth)(?:=|\s+))(\"[^\"]*\"|'[^']*'|[^\s;&|]+)"),
    # Authorization headers and bearer tokens.
    re.compile(r"(?i)(\b(?:bearer|basic)\s+)([A-Za-z0-9._~+/=-]{16,})"),
    # user:password@ in URLs.
    re.compile(r"(://[^/\s:@]+:)([^/\s@]+)(@)"),
)

_PATCH_RE = re.compile(r"\*\*\* Begin Patch(.*?)(?:\*\*\* End Patch|\Z)", re.S)
_PATCH_HEADER_RE = re.compile(
    r"\*\*\* (?:Add|Update|Delete) File: [^\n\\\"'`]+|\*\*\* Move to: [^\n\\\"'`]+")


def redact_secrets(text: str) -> str:
    """Rule 8: replace known secret shapes in text with REDACTED."""
    for rx in _SECRET_RES:
        text = rx.sub(REDACTED, text)
    for rx in _SECRET_VALUE_RES:
        text = rx.sub(lambda m: m.group(1) + REDACTED + (
            m.group(3) if m.lastindex and m.lastindex >= 3 else ""), text)
    return text


def strip_patch_bodies(text: str) -> str:
    """Rule 8: keep only file header lines of apply_patch hunks.

    File contents are never stored; a patch inside a command or a Codex
    exec program keeps its '*** Add/Update/Delete File:' and '*** Move to:'
    lines, whether the program spells newlines as real or escaped ones.
    """
    def keep_headers(match: re.Match) -> str:
        headers = _PATCH_HEADER_RE.findall(match.group(1))
        sep = "\n" if "\n" in match.group(0) else "\\n"
        return "*** Begin Patch" + sep + "".join(
            h.strip() + sep for h in headers) + "*** End Patch"
    return _PATCH_RE.sub(keep_headers, text)


OMITTED = "[content omitted]"

_HEREDOC_RE = re.compile(r"<<[-~]?[ \t]*(\\?['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
# A heredoc writes a file when its introducing line redirects output to a
# path (not 2>&1) or pipes into tee; an interpreter fed by a heredoc
# (python3 - <<EOF) runs a program, which is the command and stays.
_FILE_WRITE_RE = re.compile(
    r"(?:^|[^0-9&>]|(?<![0-9])1|&)>>?[ \t]*[^&\s|;>]|\btee\b")
_WRITER_RE = re.compile(r"\b(?:echo|printf)\b")


def _fd_before(text: str, j: int) -> tuple[int, str] | None:
    """The descriptor word ending right before text[j] ('>'), if any.

    As in the shell, digits form a descriptor only when they are the whole
    word (start of text or whitespace before them); BODY2>f writes BODY2
    to f.
    """
    k = j
    while k > 0 and text[k - 1].isdigit():
        k -= 1
    if k == j or (k > 0 and text[k - 1] not in " \t"):
        return None
    return k, text[k:j]


_SEPS = ("\n", "\\n")


def _terminator(sep: str, word: str) -> re.Pattern:
    return re.compile(re.escape(sep) + r"[\t ]*" + re.escape(word)
                      + r"(?=" + re.escape(sep) + r"|$|[\"'`);])")


def _heredoc_sep(text: str, match: re.Match, memo: dict) -> str | None:
    """The line separator a heredoc marker uses: real or escaped newline.

    A command in a Codex exec string spells its lines as escaped '\\n' on
    one physical line; a heredoc in a real script may hold escaped '\\n' in
    string literals. The separator whose terminator line closes the body
    wins; when both or neither close it, the one that ends the marker's
    line first. memo caches terminator searches so a pass stays linear.
    """
    ends = {s: text.find(s, match.end()) for s in _SEPS}
    closed = set()
    for sep, line_end in ends.items():
        if line_end == -1:
            continue
        key, start = (sep, match.group(2)), line_end
        cached = memo.get(key)
        if cached and cached[0] <= start and (
                cached[1] is None or cached[1] >= start):
            found = cached[1]
        else:
            hit = _terminator(sep, match.group(2)).search(text, start)
            found = hit.start() if hit else None
            memo[key] = (start, found)
        if found is not None:
            closed.add(sep)
    candidates = [s for s in _SEPS if ends[s] != -1]
    if len(closed) == 1:
        return closed.pop()
    return min(candidates, key=lambda s: ends[s]) if candidates else None


def _strip_heredocs(text: str, sep: str) -> str:
    """Omit the bodies of every heredoc on a line that writes a file.

    A pass handles only the markers that use its separator, and bounds the
    introducing line by both separators, so a '>' in an earlier or later
    line never marks the heredoc as a file write. Bodies follow their
    introducing line in marker order, so every heredoc on that line is
    consumed in turn. An unclosed body fails closed and is omitted to the
    end of the text.
    """
    out, pos, cursor, memo = [], 0, 0, {}
    while True:
        match = _HEREDOC_RE.search(text, cursor)
        if match is None:
            break
        own = _heredoc_sep(text, match, memo)
        if own is None:
            break
        if own != sep:
            cursor = match.end()
            continue
        line_start = max(
            (i + len(s) for s in _SEPS
             if (i := text.rfind(s, 0, match.start())) != -1), default=0)
        line_end = text.find(sep, match.end())
        intro = text[line_start:line_end]
        writes = _FILE_WRITE_RE.search(intro) is not None
        words = [m.group(2) for m in
                 _HEREDOC_RE.finditer(text, match.start(), line_end)]
        body_start = line_end + len(sep)
        for word in words:
            close = _terminator(sep, word).search(text, body_start - len(sep))
            if writes:
                out.append(text[pos:body_start] + OMITTED)
                pos = close.start() if close else len(text)
            if close is None:
                body_start = len(text)
                break
            body_start = close.end() + len(sep)
        cursor = min(body_start, len(text))
        if cursor <= match.start():
            cursor = match.end()
    out.append(text[pos:])
    return "".join(out)


def _is_word_at(text: str, j: int, word: str) -> bool:
    """Whether word stands alone at text[j] ('/usr/bin/tee' counts)."""
    if not text.startswith(word, j):
        return False
    before = text[j - 1] if j > 0 else " "
    after = text[j + len(word)] if j + len(word) < len(text) else " "
    return not (before.isalnum() or before in "_-.") and not (
        after.isalnum() or after in "_-.")


def _strip_inline_writes(text: str) -> str:
    """Omit the payload of echo or printf whose output reaches a file.

    One linear pass: each echo/printf pipeline is scanned once, honoring
    quotes (which may span lines) and backslash escapes, up to an unquoted
    ';', '&&', '||', a lone '&', a newline or an escaped newline. The
    payload ends at the first pipe or redirect operator. It becomes
    OMITTED when the pipeline writes stdout to a file ('>', '>>', '1>',
    '&>', '&>>', '>&file'; not 2>f or >&2) or pipes into tee in any
    later stage ('| sudo tee', '| cat | /usr/bin/tee').
    """
    out, pos, n = [], 0, len(text)
    while True:
        match = _WRITER_RE.search(text, pos)
        if match is None:
            break
        j, quote, end, writes, piped = match.end(), None, None, False, False
        while j < n:
            c = text[j]
            if c == "\\":
                if quote is None and text.startswith("n", j + 1):
                    break
                j += 2
                continue
            if quote is not None:
                if c == quote:
                    quote = None
            elif c in "'\"":
                quote = c
            elif c == "&" and text.startswith(">", j + 1):
                end = j if end is None else end
                writes = True
                j += 1
            elif c == "|" and not text.startswith("|", j + 1):
                end = j if end is None else end
                piped = True
            elif c in ";&|\n":
                break
            elif c == ">" and text[j - 1] != ">":
                fd = _fd_before(text, j)
                end = (fd[0] if fd else j) if end is None else end
                k = j + 1 + text.startswith(">", j + 1)
                dup = text.startswith("&", k)
                if dup:
                    k += 1
                if fd is None or fd[1] == "1":
                    if not dup or not (k < n and (text[k].isdigit()
                                                  or text[k] == "-")):
                        writes = True
                j = k - 1
            elif piped and _is_word_at(text, j, "tee"):
                writes = True
            j += 1
        j = min(j, n)
        out.append(text[pos:match.end()])
        if writes and end is not None:
            out.append(f" {OMITTED} " + text[end:j])
        else:
            out.append(text[match.end():j])
        pos = j
    out.append(text[pos:])
    return "".join(out)


# Here-strings feeding tee or a stdout redirect to a file: the payload word
# is omitted. Both orders are bounded to one command's length.
_HERESTRING_AFTER_RE = re.compile(
    r"((?:\btee\b|(?<![0-9&>])>)[^;&|\n<]{0,512}<<<[ \t]*)"
    r"('[^']*'|\"(?:[^\"\\]|\\.)*\"|[^\s;&|]+)")
_HERESTRING_BEFORE_RE = re.compile(
    r"(<<<[ \t]*)('[^']*'|\"(?:[^\"\\]|\\.)*\"|[^\s;&|]+)"
    r"(?=[^;&|\n]{0,512}?(?:\btee\b|(?<![0-9&>])>[ \t]*[^&\s|;>]))")


def _strip_herestrings(text: str) -> str:
    text = _HERESTRING_AFTER_RE.sub(lambda m: m.group(1) + OMITTED, text)
    return _HERESTRING_BEFORE_RE.sub(lambda m: m.group(1) + OMITTED, text)


def strip_file_writes(text: str) -> str:
    """Rule 8: omit literal file bodies written by a command.

    A heredoc whose introducing line redirects to a file or pipes into tee
    keeps its delimiters but not its body, whether newlines are real or
    escaped inside a Codex exec program. The payload of echo or printf
    redirected to a file is omitted too; the target path stays.
    """
    text = _strip_heredocs(text, "\n")
    text = _strip_heredocs(text, "\\n")
    return _strip_herestrings(_strip_inline_writes(text))


def argument_text(value: object) -> str | None:
    """Rule 8: one stored tool argument, or None for a non-string."""
    if not isinstance(value, str) or not value:
        return None
    text = strip_file_writes(strip_patch_bodies(value))
    return redact_secrets(text)[:ARGUMENT_CHARS]

# Rule 6, native identifiers for the tool/skill name families: a complete
# ASCII match of [A-Za-z_][A-Za-z0-9_.:/-]{0,79}. fullmatch (not ^...$)
# so a trailing newline can never slip through the $ anchor. Covers MCP
# names such as mcp__server__tool. The complete value is validated before
# any truncation; an overlong value fails closed instead of being cut.
_NATIVE_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:/-]{0,79}")
_HTTP_CODE_RE = re.compile(r"[0-9]{3}")

_IDENTIFIER_FAMILIES = frozenset({
    "tool_call", "tool_result", "read", "skill_read", "skill_invoke",
    "permission",
})

# Rule 6, skill targets: skill_read and skill_invoke targets hold only a
# validated native skill identifier (the same complete identifier rule as
# names). A free-text skill title, an installed directory path and any
# wrong-typed value fail closed to None.
_SKILL_TARGET_FAMILIES = frozenset({"skill_read", "skill_invoke"})


def _valid_native_identifier(value: object) -> str | None:
    """A validated native tool/skill identifier, or None when rejected."""
    if not isinstance(value, str) or not value:
        return None
    if _NATIVE_IDENTIFIER_RE.fullmatch(value) is None:
        return None
    return value


def filter_target(target: object, family: object = None) -> str | None:
    """Rule 6 targets: keep strings (paths, commands, identifiers).

    Non-strings are rejected, never coerced; kept strings are bounded.
    For the skill families (skill_read, skill_invoke) the target holds
    only a validated native skill identifier under the same complete
    identifier rule as names: titles, sentences, paths, whitespace,
    markers and wrong types fail closed to None. Other families keep
    the full string under rule 8 (argument_text): secrets redacted,
    patch bodies reduced to file headers, bounded to ARGUMENT_CHARS.
    """
    if family in _SKILL_TARGET_FAMILIES:
        return _valid_native_identifier(target)
    return argument_text(target)


def _valid_int(value: object) -> bool:
    return type(value) is int


def _valid_identifier(value: object) -> str | None:
    # Rule 6 skill identifier detail: the same native-identifier rule as
    # event names for the identifier families. The complete value must
    # match before any truncation; overlong or mistyped values fail closed.
    return _valid_native_identifier(value)


def _valid_command(value: object) -> str | None:
    kept = argument_text(value)
    return kept[:_TARGET_CHARS] if kept else None


def _valid_path_detail(value: object) -> str | None:
    """Rule 6 skill file path detail: fail closed and marker/type safe.

    Only a non-empty string without tag-like markers or control
    characters survives, bounded to _TARGET_CHARS. Relative and absolute
    spellings both survive so edit-reset logic sees every spelling;
    wrong types, empty values, markers and control characters fail
    closed to None and never persist.
    """
    if not isinstance(value, str) or not value:
        return None
    if len(value) > _TARGET_CHARS:
        return None
    if _CONTROL_RE.search(value) is not None:
        return None
    if _marker_pos(value) is not None:
        return None
    return value


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
        elif kind == "path":
            kept = _valid_path_detail(value)
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


# Rule 6, event names: the tool/skill name families (tool_call,
# tool_result, read, skill_read, skill_invoke, permission) accept only a
# native identifier matching [A-Za-z_][A-Za-z0-9_.:/-]{0,79} (see
# _valid_native_identifier). Lifecycle, compaction, assistant_message and
# file_change stay closed sets holding exactly the union of native names
# every adapter legitimately emits. Unknown families fail closed.
EVENT_NAME_ENUMS: dict[str, frozenset] = {
    "assistant_message": frozenset({"assistant_message"}),
    "compaction": frozenset({
        "context_compaction", "compact_boundary",
        "time_compacting", "compaction",
        "auto_compact_started", "auto_compact_completed",
    }),
    "lifecycle": frozenset({
        "task_complete", "turn_aborted", "turn_duration",
        "subagent_activity", "thread_goal_updated", "api_error",
        "stop_hook_summary", "informational", "Plan", "HookPrompt",
        "EnteredReviewMode", "ExitedReviewMode", "collab.unknown",
        "error", "turn_started", "turn_ended", "tool_started",
        "retry_state", "subagent_spawned", "subagent_finished",
        "task_backgrounded", "task_completed", "compaction_checkpoint",
        "hook_execution", "session_recap", "plan", "background_tasks",
        "image_compressed", "current_mode_update", "rewind_marker",
    }),
    "tool_call": frozenset(),
    "tool_result": frozenset(),
    "file_change": frozenset({
        "file_change",
        "Edit", "Write", "MultiEdit", "NotebookEdit",
        "edit", "write", "patch",
    }),
    "read": frozenset(),
    "skill_read": frozenset(),
    "skill_invoke": frozenset(),
    "permission": frozenset(),
}


def filter_event_name(family: str, value: object) -> str | None:
    """The validated event name, or None for a wrong type/unknown value.

    Identifier families (tool_call, tool_result, read, skill_read,
    skill_invoke, permission) accept only the native tool or skill name
    field matching the complete ASCII pattern
    [A-Za-z_][A-Za-z0-9_.:/-]{0,79} (this covers MCP names such as
    mcp__server__tool). Adapters must pass only that native name field,
    never a title, description, message or other free text, and such free
    text must never be stored: titles, sentences, tag-like text and
    secret-looking strings with spaces or punctuation outside the allowed
    set fail closed. No semantic title detection rejects an
    identifier-shaped native name. Lifecycle, compaction,
    assistant_message and file_change accept only their closed sets;
    unknown families fail closed.
    """
    if not isinstance(value, str) or not value:
        return None
    if family in _IDENTIFIER_FAMILIES:
        return _valid_native_identifier(value)
    allowed = EVENT_NAME_ENUMS.get(family)
    if not allowed:
        return None
    if value in allowed:
        return value
    return None


# Rule 6, event statuses: closed per-family enums. None stays None (no
# status); any other value of the wrong type or outside the family set is
# dropped, never stringified or passed through. Lifecycle and tool_result
# additionally accept HTTP status codes 100-599 given as an int or a
# string of exactly three digits, stored as the three-digit string.
_COMMON_STATUS = frozenset({
    "ok", "error", "denied", "completed", "success", "failed", "failure",
})
_HTTP_STATUS_FAMILIES = frozenset({"lifecycle", "tool_result"})
EVENT_STATUS_ENUMS: dict[str, frozenset] = {
    "tool_call": _COMMON_STATUS | frozenset({"cancelled"}),
    "tool_result": _COMMON_STATUS | frozenset({"cancelled"}),
    "read": _COMMON_STATUS | frozenset({"cancelled"}),
    "skill_read": _COMMON_STATUS | frozenset({"cancelled"}),
    "lifecycle": frozenset(
        {"completed", "cancelled", "denied", "ok", "error",
         "success", "failed"}),
    "permission": frozenset({"denied", "deny", "allow"}),
    "file_change": frozenset(),
    "compaction": frozenset(),
    "assistant_message": frozenset(),
    "skill_invoke": frozenset(),
}


def _valid_http_status(value: object) -> str | None:
    """An HTTP 100-599 code as an int or exact three-digit string, or None.

    Bools, floats, two- or four-digit values, 000/099/600/999, whitespace
    variants and arbitrary numeric prose all fail closed.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if 100 <= value <= 599:
            return str(value)
        return None
    if isinstance(value, str):
        if _HTTP_CODE_RE.fullmatch(value) is None:
            return None
        try:
            code = int(value)
        except ValueError:
            return None
        if 100 <= code <= 599:
            return value
        return None
    return None


def filter_event_status(family: str, value: object) -> str | None:
    """The validated event status, or None when absent or not allowed.

    Closed per-family sets, plus HTTP status codes 100-599 (int or exact
    three-digit string, stored as the three-digit string) for the
    lifecycle and tool_result families only. Free-text sentences, titles,
    messages and unknown enum strings return None.
    """
    if value is None:
        return None
    allowed = EVENT_STATUS_ENUMS.get(family)
    if not allowed:
        return None
    if isinstance(value, str) and value in allowed:
        return value
    if family in _HTTP_STATUS_FAMILIES:
        kept = _valid_http_status(value)
        if kept is not None:
            return kept
    return None
