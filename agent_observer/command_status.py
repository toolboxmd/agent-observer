"""Command-specific result semantics, shared by ingest and ledger correction."""

import re
import shlex


# Keep operators distinct from quoted words, including quoted ';' and '|'.
_SHELL_TOKEN = re.compile(r"""
    (?P<space>[\t\r ]+)
  | (?P<newline>\n)
  | (?P<comment>\#[^\n]*)
  | (?P<operator>&&|\|\||<<-?|>>|>&|<&|[;&|()<>])
  | (?P<word>(?:'[^']*'|"(?:\\[\s\S]|[^"\\])*"|\\[\s\S]|[^\s'"\\;&|()<>])+)
""", re.VERBOSE)


def _tokens(script):
    """Lex the supported shell list syntax without executing any input.

    Heredoc bodies are data: even unquoted operators there do not split the
    script. Retain raw words so quoting cannot turn data into an operator.
    """
    result, heredocs = [], []
    offset = 0
    delimiter = None
    while offset < len(script):
        match = _SHELL_TOKEN.match(script, offset)
        if match is None:
            raise ValueError("unsupported or unclosed shell word")
        offset = match.end()
        kind, value = match.lastgroup, match.group()
        if kind in ("space", "comment"):
            continue
        if kind == "word" and delimiter is not None:
            words = shlex.split(value)
            if len(words) != 1:
                raise ValueError("invalid heredoc delimiter")
            heredocs.append((words[0], delimiter == "<<-"))
            delimiter = None
        elif kind == "operator" and value in ("<<", "<<-"):
            delimiter = value
        result.append((kind, value))
        if kind == "newline":
            for end, strip_tabs in heredocs:
                while True:
                    if offset >= len(script):
                        raise ValueError("unclosed heredoc")
                    line_end = script.find("\n", offset)
                    line_end = len(script) if line_end < 0 else line_end + 1
                    line = script[offset:line_end].rstrip("\n")
                    offset = line_end
                    if (line.lstrip("\t") if strip_tabs else line) == end:
                        break
            heredocs.clear()
    if delimiter is not None or heredocs:
        raise ValueError("unclosed heredoc")
    return result


def _last_chain(tokens):
    """A sequence's last AND-list owns its status, including short circuits."""
    chain, command, last = [], [], []
    for kind, value in tokens:
        if kind == "newline" and not command:
            continue  # Blank lines or continuation after &&.
        if kind == "newline" or (kind == "operator" and value == ";"):
            last = chain + [command]
            chain, command = [], []
        elif kind == "operator" and value == "&&":
            if not command:
                raise ValueError("empty AND command")
            chain.append(command)
            command = []
        else:
            command.append((kind, value))
    if chain and not command:
        raise ValueError("unfinished AND-list")
    return chain + [command] if command else last


def _command_words(tokens):
    words = []
    index = 0
    while index < len(tokens):
        kind, raw = tokens[index]
        if kind == "operator":
            # Redirections do not replace the status-producing command.
            if raw not in ("<", ">", ">>", "<&", ">&"):
                return []  # Pipelines, OR, subshells and other unsupported syntax.
            if index + 1 >= len(tokens) or tokens[index + 1][0] != "word":
                return []
            if words and words[-1].isdigit():
                words.pop()  # Optional file descriptor, e.g. 2>file.
            index += 2
            continue
        if "$(" in raw or "`" in raw:
            return []  # A substitution can itself fail; no output inference.
        words.extend(shlex.split(raw))
        index += 1
    return words


def _known_gate(words):
    """The observed AND-list gates cannot themselves explain exit 8.

    Limit this to git's status/rev-parse and gh pr view without jq/template
    execution or a browser. Unknown gates (notably pytest) stay ambiguous.
    """
    if not words:
        return False
    executable, args = words[0].rsplit("/", 1)[-1], words[1:]
    if executable == "git":
        if args[:1] == ["-C"] and len(args) >= 3:
            args = args[2:]
        return args == ["rev-parse", "HEAD"] or args in (
            ["status", "--porcelain=v1"], ["status", "--short"])
    if executable == "gh" and args[:2] == ["pr", "view"]:
        args = args[2:]
        while args:
            if args[0] in ("-R", "--repo", "--json") and len(args) >= 2:
                args = args[2:]
            elif args[0].isdigit():
                args = args[1:]
            else:
                return False
        return True
    return False


def pending_pr_checks(family, target, detail) -> bool:
    """Recognize exit 8 only when gh pr checks can account for that status.

    Earlier commands separated by newline or ';' do not own the result.
    In an AND-list any failing command can own it, so unknown gates prevent
    classification. Quoting and heredocs never create command separators.
    """
    if family != "tool_result" or not isinstance(target, str):
        return False
    if detail.get("exit_code", detail.get("exitCode")) != 8:
        return False
    try:
        chain = _last_chain(_tokens(target))
        commands = [_command_words(command) for command in chain]
    except ValueError:
        return False
    pending = False
    for words in commands:
        if (len(words) >= 3 and words[0].rsplit("/", 1)[-1] == "gh"
                and words[1:3] == ["pr", "checks"]):
            pending = True
        elif not _known_gate(words):
            return False
    return pending
