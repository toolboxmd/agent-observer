"""Command-specific result semantics, shared by ingest and ledger correction."""

import shlex


def pending_pr_checks(family, target, detail) -> bool:
    """Only a standalone gh pr checks exit 8 means checks are pending.

    Compound shell commands can return a different command's exit code, so
    they cannot establish this result. Never infer it from output text.
    """
    if family != "tool_result" or not isinstance(target, str):
        return False
    if detail.get("exit_code", detail.get("exitCode")) != 8:
        return False
    if any(part in target for part in ("\n", "`", "$(")):
        return False
    try:
        lexer = shlex.shlex(target, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return False
    return (len(words) >= 3 and words[0].rsplit("/", 1)[-1] == "gh"
            and words[1:3] == ["pr", "checks"]
            and not any(word and all(c in ";&|()<>" for c in word)
                        for word in words[3:]))
