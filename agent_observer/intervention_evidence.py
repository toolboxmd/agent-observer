"""Closed transport metadata from native inputs, never their private bodies."""

import re


_STEER = re.compile(
    r"\ANote from T3 Code: the (user|agent in T3 thread [^\s()]+) "
    r"interrupted your previous turn, if it was still running, to deliver the message below\. "
    r"Tool calls stopped for this message were interrupted, not refused, even if their results "
    r"say the user declined them or does not want them\. "
    r"Follow the message below before deciding whether to retry a stopped action\. "
    r"Stopped actions may have partly run, other running work may have stopped too, "
    r"and actions that finished keep their effects\.\n\n[^\s].*\Z", re.DOTALL)
_RUNTIME_INFO = r"<runtime_info>(?:(?!</?runtime_info>).)*</runtime_info>"
_RUNTIME_SUFFIX = r"\s*(?:" + _RUNTIME_INFO + r"\s*)?\Z"
_COMPLETION = re.compile(
    r"\ADelegated task (?P<id>[^\s]+) reached a terminal state\. "
    r"Use task_status with taskId (?P=id) to read the result\." + _RUNTIME_SUFFIX, re.DOTALL)
_COMPLETIONS = re.compile(
    r"\ADelegated tasks [^\n]+ reached terminal states\. "
    r"Use task_status with each taskId to read the results\." + _RUNTIME_SUFFIX, re.DOTALL)


def ensure_table(con):
    con.execute(
        "CREATE TABLE IF NOT EXISTS intervention_prompt_evidence("
        " native_id TEXT PRIMARY KEY REFERENCES user_messages(native_id)"
        " ON UPDATE CASCADE ON DELETE CASCADE,"
        " transport_kind TEXT CHECK(transport_kind IN ('steer','completion')),"
        " actor_kind TEXT CHECK(actor_kind IN ('client','server')),"
        " actor_reason TEXT NOT NULL)")


def record(con, native_id, text, *, sdk=False, allowed=True):
    """Parse only anchored transport envelopes, excluding Claude scaffolding/children.

    The envelope is sender evidence even when a native provider mislabeled the
    input as user-authored. Ordinary SDK inputs need a matching origin, never
    an inference from the thread opener or a preceding conversational turn.
    """
    kind, actor = None, None
    reason = ('native_sdk_prompt_without_matching_origin' if native_id.startswith('claude:')
              else 'native_prompt_without_matching_origin') if sdk else 'actor_evidence_missing'
    if allowed:
        steer = _STEER.match(text) if sdk else None
        if steer:
            kind = 'steer'
            actor = 'client' if steer[1] == 'user' else 'server'
            reason = 't3_steer_sender'
        elif _COMPLETION.match(text) or _COMPLETIONS.match(text):
            kind, actor, reason = 'completion', 'server', 'delegated_task_notice'
    con.execute(
        "INSERT INTO intervention_prompt_evidence VALUES(?,?,?,?)"
        " ON CONFLICT(native_id) DO UPDATE SET transport_kind=excluded.transport_kind,"
        " actor_kind=excluded.actor_kind,actor_reason=excluded.actor_reason",
        (native_id, kind, actor, reason))


def backfill_root_reasons(con):
    """Keep a stored gap even for harnesses without a transport-envelope parser."""
    ensure_table(con)
    con.execute(
        "INSERT OR IGNORE INTO intervention_prompt_evidence"
        " SELECT u.native_id,NULL,NULL,'native_prompt_without_matching_origin'"
        " FROM user_messages u JOIN sessions s ON s.session_key=u.session_key"
        " JOIN t3_threads t ON t.native_session=s.native_id"
        " WHERE t.thread_id=t.root_thread_id AND t.opened_by='client'")
