"""Chromeria/T3 Code attribution adapter: Issues and PRs from T3 state.

Reads T3's ``state.sqlite`` strictly read-only (SQLite URI ``mode=ro``;
never writes, never deletes) from ``$T3CODE_HOME/userdata/state.sqlite``
(default ``~/.t3``) and maps it onto the existing workload tables:

- turn origin: ``orchestration_events`` ``thread.turn-start-requested``
  rows carry ``actor_kind`` ``client`` (a person typed the prompt) or
  ``server`` (a spawn or ``message_thread`` dispatch). The T3-side message
  id joins through ``projection_turns`` (``turn_id`` is the native Claude
  user-message uuid, ``pending_message_id`` the T3 message id), so a
  Claude ``promptSource: "sdk"`` submission is ``genuine`` when its turn
  was client-originated and ``synthetic`` when server-originated. Router
  ``claude -p`` workers (``sdk-cli``) never appear as T3 turn starts, so
  they are unaffected. Unknown stays unknown: a turn id with no
  client/server evidence is never flipped.
- thread to native session: ``provider_session_runtime.resume_cursor_json``
  (``resume`` for Claude, ``threadId`` for Codex, ``sessionId`` for
  OpenCode). Claude sessions rotated under one thread are also found
  through their turn-start uuids in ``submissions``.
- thread trees: child ids of the form ``sub.<parentThreadId>.<suffix>``
  (nesting allowed) group under their root. Every PR or Issue linked
  anywhere in a tree becomes one task (``<repo>#<N>``); every native
  session in the tree is wholly bound to every tree task through
  ``session_assignments``. A one-link tree attributes exclusively; a
  multi-link tree lands in the shared joint bucket under the existing
  semantics and is never divided.
- acceptance: the PR snapshot ``state``; ``merged`` completes the task
  outcome, every other state stays explicit, and nothing is inferred
  from exit codes (which are never read).
- Ghostty bodies and unknown provider shapes are out of scope: their
  rows are skipped, never guessed.
- spawns and cross-thread messages (#42): every ``sub.`` thread in
  ``projection_threads`` mirrors its parent, depth and creation time;
  every delivered message Chromeria prefixed with ``[Message from <title>
  (thread <id>)]`` (``message_thread`` into a thread that is not the
  sender's child) mirrors its sender id, target id and time. Titles and
  message text are never stored.

Privacy (fail closed): only identifiers (thread, message and session
ids), link coordinates (host, repository, number, url), PR snapshot
state and validated actor kinds reach the ledger. No prompt, title or
other free text is stored: task titles are the ``repo#N`` identifier
itself. Backfilled submission excerpts stay empty; excerpts for newly
genuine prompts come only from the native transcript import.

Stdlib only. Never writes the T3 database.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3

from .. import db, placement, privacy

# A PR head branch as T3 snapshots it; anything else is not stored.
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")

HARNESS = "t3"

CAPABILITIES = [
    ("turn_origin", True, "T3 turn-start actor_kind maps Claude sdk prompts to genuine (client) or synthetic (server)"),
    ("thread_sessions", True, "provider resume cursors map T3 threads to native Claude, Codex and OpenCode sessions"),
    ("workload_binding", True, "thread-tree PR/Issue links become repo#N tasks with whole-session ownership; multi-link trees stay shared whole"),
    ("acceptance", True, "merged PR snapshots complete task outcomes; other states stay explicit"),
    ("tool_calls", False, "no tool evidence is imported from T3 state"),
    ("human_input", False, "no submissions are synthesized; existing submissions are only reclassified"),
]

STATE_RELATIVE = os.path.join("userdata", "state.sqlite")
RATES_RELATIVE = os.path.join("userdata", "usage-model-rates.json")
TURN_START_EVENT = "thread.turn-start-requested"

# Chromeria's attribution line on a message_thread delivery whose target
# is not the sender's child; group 1 is the sender thread id.
MESSAGE_FROM_RE = re.compile(r"^\[Message from .* \(thread ([^\s()]+)\)\]$")
SUB_PREFIX = "sub."

# Closed native vocabularies. Anything outside is skipped, never stored.
ACTORS = ("client", "server")
LINK_SOURCES = ("agent", "user", "system")
MERGED_STATE = "merged"


def t3_home(root: str | None = None) -> str:
    """The T3 home directory: explicit root, $T3CODE_HOME, or ~/.t3."""
    if root:
        return root
    return os.environ.get("T3CODE_HOME") or os.path.expanduser("~/.t3")


def state_path(root: str | None = None, source: str | None = None) -> str:
    """The T3 state database: explicit source file or the home default."""
    if source:
        return source
    return os.path.join(t3_home(root), STATE_RELATIVE)


def rates_path(root: str | None = None) -> str:
    """The T3 LiteLLM rate table path (absent when T3 never fetched it)."""
    return os.path.join(t3_home(root), RATES_RELATIVE)


def _ensure_tables(con: sqlite3.Connection) -> None:
    """Adapter-local T3 mirror tables. Created here so db.py stays untouched."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS t3_turn_origins("
        " message_id TEXT PRIMARY KEY,"
        " thread_id TEXT NOT NULL,"
        " actor_kind TEXT NOT NULL,"
        " occurred_at TEXT)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS t3_threads("
        " thread_id TEXT PRIMARY KEY,"
        " root_thread_id TEXT NOT NULL,"
        " provider TEXT,"
        " adapter_key TEXT,"
        " native_session TEXT NOT NULL)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS t3_links("
        " thread_id TEXT NOT NULL,"
        " kind TEXT NOT NULL,"
        " host TEXT NOT NULL,"
        " repository TEXT NOT NULL,"
        " number INTEGER NOT NULL,"
        " url TEXT NOT NULL,"
        " source TEXT,"
        " state TEXT,"
        " linked_at TEXT,"
        " PRIMARY KEY (thread_id, kind, repository, number))")
    if "head_branch" not in {r["name"] for r in con.execute("PRAGMA table_info(t3_links)")}:
        con.execute("ALTER TABLE t3_links ADD COLUMN head_branch TEXT")
    con.execute(
        "CREATE TABLE IF NOT EXISTS t3_spawns("
        " thread_id TEXT PRIMARY KEY,"
        " parent_thread_id TEXT NOT NULL,"
        " depth INTEGER NOT NULL,"
        " created_at TEXT)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS t3_messages("
        " message_id TEXT PRIMARY KEY,"
        " sender_thread_id TEXT NOT NULL,"
        " target_thread_id TEXT NOT NULL,"
        " sent_at TEXT)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS checkouts("
        " root TEXT PRIMARY KEY,"
        " repository TEXT NOT NULL,"
        " branch TEXT,"
        " seen_at REAL NOT NULL)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS t3_outcome_provenance("
        " task_id TEXT PRIMARY KEY,"
        " acceptance_state TEXT, proof_ref TEXT)")


def turn_actor(con: sqlite3.Connection, message_id: str) -> str | None:
    """The T3 turn origin for one native message uuid, or None.

    Missing table (a ledger synced before this adapter existed) and
    unknown ids both read as None, never as a guessed origin.
    """
    if not isinstance(message_id, str) or not message_id:
        return None
    try:
        row = con.execute(
            "SELECT actor_kind FROM t3_turn_origins WHERE message_id=?",
            (message_id,)).fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    try:
        actor = row["actor_kind"]
    except (KeyError, TypeError, IndexError):
        return None
    return actor if actor in ACTORS else None


def thread_root(thread_id: str) -> str:
    """The tree root of a T3 thread id.

    Children nest as ``sub.<parent>.<suffix>``; stripping every leading
    ``sub.`` segment and taking the first dotted component reaches the
    root. Plain thread ids (including ``import:`` ids) are their own root.
    """
    rest = thread_id
    while rest.startswith("sub."):
        rest = rest[len("sub."):]
    return rest.split(".")[0]


def parent_thread(thread_id: str) -> str | None:
    """The spawning thread of a ``sub.<parent>.<suffix>`` id, or None.

    Mirrors Chromeria's ``parentThreadIdOf``: the parent is everything
    between the first ``sub.`` and the last dot.
    """
    if not thread_id.startswith(SUB_PREFIX) \
            or thread_id.rfind(".") <= len(SUB_PREFIX):
        return None
    return thread_id[len(SUB_PREFIX):thread_id.rfind(".")]


def spawn_depth(thread_id: str) -> int:
    """Spawn levels below the root: 0 for a user thread, 1 for its child."""
    depth = 0
    parent = parent_thread(thread_id)
    while parent is not None:
        depth += 1
        parent = parent_thread(parent)
    return depth


def _valid_id(value) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 200:
        return None
    if any(ord(c) < 32 for c in value):
        return None
    return value


def _valid_link(host, repository, number, url) -> tuple | None:
    if not isinstance(host, str) or not host or len(host) > 253:
        return None
    if not isinstance(repository, str) or not repository \
            or len(repository) > 200 or "/" not in repository:
        return None
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return None
    if not isinstance(url, str) or not url.startswith("https://") \
            or len(url) > 500:
        return None
    return (host, repository, number, url)


def _open_read_only(path: str) -> sqlite3.Connection:
    """Open a T3 state database read-only; raises on any failure."""
    uri = "file:" + path + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def sync(con: sqlite3.Connection, root: str | None = None,
         full: bool = False, source: str | None = None) -> dict:
    totals = {"harness": HARNESS, "sources": 0, "unchanged": 0,
              "responses_inserted": 0, "events_inserted": 0,
              "submissions_inserted": 0, "malformed": 0, "failed": []}
    _ = full  # The T3 mirror is always a full re-read; bindings are durable.
    path = state_path(root=root, source=source)
    if not os.path.exists(path):
        totals["skipped"] = f"no T3 state at {path}"
        return totals
    _ensure_tables(con)
    try:
        native = _open_read_only(path)
    except (sqlite3.Error, OSError, ValueError) as exc:
        totals["failed"].append({"path": path, "error": str(exc)})
        return totals
    try:
        _import_turn_origins(con, native, path, totals)
        threads = _import_threads(con, native, totals)
        links = _import_links(con, native, totals)
        _import_spawns(con, native, totals)
        _import_messages(con, native, totals)
        totals["submissions_reclassified"] = _backfill_submissions(con)
        totals["responses_retouched"] = _retouch_response_turns(con)
        bound = _bind_trees(con, threads, links)
        totals["tasks"] = bound["tasks"]
        totals["sessions_bound"] = bound["sessions_bound"]
        totals["outcomes"] = _write_outcomes(con, links)
        # Record checkouts while task worktrees still exist (#43).
        totals["shared_sessions"] = placement.record_shared_checkouts(con)
        con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES(?,?,?,?) ON CONFLICT(harness, path) DO UPDATE SET"
            " imported_at=excluded.imported_at",
            (HARNESS, path, _state_marker(path), db.now()))
        totals["sources"] = 1
    except sqlite3.DatabaseError as exc:
        totals["failed"].append({"path": path, "error": str(exc)})
    finally:
        try:
            native.close()
        except sqlite3.Error:
            pass
    return totals


def _state_marker(path: str) -> str:
    """A cheap change marker for the state file; never its contents."""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return f"size:{st.st_size}:mtime:{st.st_mtime_ns}"


def _record_malformed(con: sqlite3.Connection, path: str, totals: dict) -> None:
    totals["malformed"] += 1
    exists = con.execute(
        "SELECT 1 FROM import_errors WHERE harness=? AND source_path=?"
        " AND ordinal_num IS NULL AND error=?",
        (HARNESS, path, "unsupported_schema")).fetchone()
    if exists is None:
        con.execute(
            "INSERT INTO import_errors(harness, source_path, ordinal_num,"
            " error, line_excerpt, created_at) VALUES(?,?,?,?,?,?)",
            (HARNESS, path, None, "unsupported_schema", "", db.now()))


def _tables(native: sqlite3.Connection) -> set:
    try:
        return {r[0] for r in native.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError:
        return set()


def _import_turn_origins(con: sqlite3.Connection, native: sqlite3.Connection,
                         path: str, totals: dict) -> int:
    """Mirror client/server origin per native turn-start uuid.

    Joins ``projection_turns`` (native ``turn_id`` to T3
    ``pending_message_id``) against ``thread.turn-start-requested``
    events (T3 message id to ``actor_kind``). A native id is
    client-originated when any same-thread client event names its
    pending id, server-originated on an explicit server event, and
    otherwise absent: unknown stays unknown and is never stored.
    """
    tables = _tables(native)
    if "projection_turns" not in tables or "orchestration_events" not in tables:
        return 0
    try:
        turns = native.execute(
            "SELECT thread_id, turn_id, pending_message_id"
            " FROM projection_turns").fetchall()
        starts = native.execute(
            "SELECT stream_id, actor_kind, payload_json, occurred_at"
            " FROM orchestration_events WHERE event_type=?",
            (TURN_START_EVENT,)).fetchall()
    except sqlite3.DatabaseError:
        return 0
    # T3 message id -> (thread, actor, occurred_at) for well-formed
    # turn-start events.
    actors: dict[str, tuple[str, str, str | None]] = {}
    for row in starts:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (ValueError, TypeError):
            _record_malformed(con, path, totals)
            continue
        actor = row["actor_kind"]
        message_id = payload.get("messageId") if isinstance(payload, dict) else None
        thread = payload.get("threadId") if isinstance(payload, dict) else None
        if actor not in ACTORS or not _valid_id(message_id) \
                or not _valid_id(thread):
            _record_malformed(con, path, totals)
            continue
        occurred = row["occurred_at"] \
            if isinstance(row["occurred_at"], str) else None
        key = (message_id, thread)
        if key not in actors:
            actors[key] = (thread, actor, occurred)
        elif actors[key][1] == "server" and actor == "client":
            # A human turn wins over a server dispatch for the same id.
            actors[key] = (thread, actor, occurred)
    mapped = 0
    for row in turns:
        turn_id = _valid_id(row["turn_id"])
        pending = row["pending_message_id"]
        thread = _valid_id(row["thread_id"])
        if turn_id is None or thread is None:
            _record_malformed(con, path, totals)
            continue
        if not _valid_id(pending):
            # No T3 message: no origin evidence (a continuation or a
            # rotated session fragment). Unknown stays unknown.
            continue
        hit = actors.get((pending, thread))
        if hit is None:
            continue
        _, actor, occurred_at = hit
        con.execute(
            "INSERT INTO t3_turn_origins(message_id, thread_id, actor_kind,"
            " occurred_at) VALUES(?,?,?,?)"
            " ON CONFLICT(message_id) DO UPDATE SET"
            " actor_kind=CASE WHEN excluded.actor_kind='client'"
            " THEN 'client' ELSE t3_turn_origins.actor_kind END,"
            " thread_id=excluded.thread_id,"
            " occurred_at=COALESCE(excluded.occurred_at,"
            " t3_turn_origins.occurred_at)",
            (turn_id, thread, actor, occurred_at))
        mapped += 1
    totals["turns_mapped"] = mapped
    return mapped


def _cursor_session(adapter_key, cursor: dict) -> str | None:
    """The native session id inside a resume cursor, or None."""
    if not isinstance(cursor, dict):
        return None
    if adapter_key == "claudeAgent":
        return _valid_id(cursor.get("resume"))
    if adapter_key == "codex":
        return _valid_id(cursor.get("threadId"))
    if adapter_key == "opencode":
        return _valid_id(cursor.get("sessionId"))
    return None


def _import_threads(con: sqlite3.Connection, native: sqlite3.Connection,
                    totals: dict) -> dict:
    """Mirror thread to native-session rows. Returns thread -> sessions."""
    result: dict[str, list] = {}
    if "provider_session_runtime" not in _tables(native):
        totals["threads"] = 0
        return result
    try:
        rows = native.execute(
            "SELECT thread_id, provider_name, adapter_key,"
            " resume_cursor_json FROM provider_session_runtime").fetchall()
    except sqlite3.DatabaseError:
        totals["threads"] = 0
        return result
    seen = 0
    for row in rows:
        thread_id = _valid_id(row["thread_id"])
        if thread_id is None:
            totals["malformed"] += 1
            continue
        try:
            cursor = json.loads(row["resume_cursor_json"] or "{}")
        except (ValueError, TypeError):
            totals["malformed"] += 1
            continue
        native_session = _cursor_session(row["adapter_key"], cursor)
        if native_session is None:
            # Unknown provider or cursor shape (Ghostty bodies, future
            # adapters): out of scope, skipped without guessing.
            totals["malformed"] += 1
            continue
        provider = row["provider_name"]
        con.execute(
            "INSERT INTO t3_threads(thread_id, root_thread_id, provider,"
            " adapter_key, native_session) VALUES(?,?,?,?,?)"
            " ON CONFLICT(thread_id) DO UPDATE SET"
            " root_thread_id=excluded.root_thread_id,"
            " provider=excluded.provider, adapter_key=excluded.adapter_key,"
            " native_session=excluded.native_session",
            (thread_id, thread_root(thread_id),
             provider if isinstance(provider, str) else None,
             row["adapter_key"]
             if isinstance(row["adapter_key"], str) else None,
             native_session))
        result.setdefault(thread_id, []).append(native_session)
        seen += 1
    totals["threads"] = seen
    return result


def _import_links(con: sqlite3.Connection, native: sqlite3.Connection,
                  totals: dict) -> list:
    """Mirror PR and Issue links with PR snapshot state."""
    links = []
    tables = _tables(native)
    if "projection_thread_pull_requests" in tables:
        try:
            rows = native.execute(
                "SELECT thread_id, host, repository, number, url, source,"
                " linked_at, snapshot_json"
                " FROM projection_thread_pull_requests").fetchall()
        except sqlite3.DatabaseError:
            rows = []
        for row in rows:
            thread_id = _valid_id(row["thread_id"])
            valid = _valid_link(row["host"], row["repository"],
                                row["number"], row["url"])
            if thread_id is None or valid is None:
                totals["malformed"] += 1
                continue
            host, repository, number, url = valid
            state = head = None
            try:
                snapshot = json.loads(row["snapshot_json"] or "{}")
            except (ValueError, TypeError):
                snapshot = None
            if isinstance(snapshot, dict) and isinstance(
                    snapshot.get("state"), str):
                state = snapshot["state"][:32] or None
            if isinstance(snapshot, dict) and isinstance(snapshot.get("headBranch"), str) \
                    and BRANCH_RE.match(snapshot["headBranch"]):
                head = snapshot["headBranch"]
            source = row["source"]
            links.append({"thread_id": thread_id, "kind": "pr",
                          "host": host, "repository": repository,
                          "number": number, "url": url,
                          "source": source if source in LINK_SOURCES else None,
                          "state": state, "head_branch": head,
                          "linked_at": row["linked_at"]
                          if isinstance(row["linked_at"], str) else None})
    if "fork_thread_issue_links" in tables:
        try:
            rows = native.execute(
                "SELECT thread_id, host, repository, number, url, source,"
                " linked_at FROM fork_thread_issue_links").fetchall()
        except sqlite3.DatabaseError:
            rows = []
        for row in rows:
            thread_id = _valid_id(row["thread_id"])
            valid = _valid_link(row["host"], row["repository"],
                                row["number"], row["url"])
            if thread_id is None or valid is None:
                totals["malformed"] += 1
                continue
            host, repository, number, url = valid
            source = row["source"]
            links.append({"thread_id": thread_id, "kind": "issue",
                          "host": host, "repository": repository,
                          "number": number, "url": url,
                          "source": source if source in LINK_SOURCES else None,
                          "state": None, "linked_at": row["linked_at"]
                          if isinstance(row["linked_at"], str) else None})
    for link in links:
        con.execute(
            "INSERT INTO t3_links(thread_id, kind, host, repository, number,"
            " url, source, state, linked_at, head_branch) VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(thread_id, kind, repository, number) DO UPDATE SET"
            " host=excluded.host, url=excluded.url, source=excluded.source,"
            " state=excluded.state, linked_at=excluded.linked_at,"
            " head_branch=COALESCE(excluded.head_branch, t3_links.head_branch)",
            (link["thread_id"], link["kind"], link["host"],
             link["repository"], link["number"], link["url"],
             link["source"], link["state"], link["linked_at"],
             link.get("head_branch")))
    totals["links"] = len(links)
    return links


def _import_spawns(con: sqlite3.Connection, native: sqlite3.Connection,
                   totals: dict) -> None:
    """Mirror every child thread's parent, depth and creation time."""
    totals["spawns"] = 0
    if "projection_threads" not in _tables(native):
        return
    try:
        rows = native.execute(
            "SELECT thread_id, created_at FROM projection_threads"
            " WHERE thread_id LIKE 'sub.%'").fetchall()
    except sqlite3.DatabaseError:
        return
    for row in rows:
        thread_id = _valid_id(row["thread_id"])
        parent = parent_thread(thread_id) if thread_id else None
        if parent is None:
            totals["malformed"] += 1
            continue
        con.execute(
            "INSERT INTO t3_spawns(thread_id, parent_thread_id, depth,"
            " created_at) VALUES(?,?,?,?) ON CONFLICT(thread_id) DO UPDATE"
            " SET created_at=excluded.created_at",
            (thread_id, parent, spawn_depth(thread_id),
             row["created_at"] if isinstance(row["created_at"], str)
             else None))
        totals["spawns"] += 1


def _import_messages(con: sqlite3.Connection, native: sqlite3.Connection,
                     totals: dict) -> None:
    """Mirror attributed message_thread deliveries: ids and time only.

    Chromeria prefixes a delivery only when the target is not the
    sender's child, so every prefixed message is one the report lists.
    Only the attribution line is read; the body never leaves T3.
    """
    totals["messages"] = 0
    if "projection_thread_messages" not in _tables(native):
        return
    try:
        rows = native.execute(
            "SELECT message_id, thread_id, created_at,"
            " substr(text, 1, 512) AS head FROM projection_thread_messages"
            " WHERE role='user' AND text LIKE '[Message from %'").fetchall()
    except sqlite3.DatabaseError:
        return
    for row in rows:
        message_id = _valid_id(row["message_id"])
        target = _valid_id(row["thread_id"])
        head = row["head"] if isinstance(row["head"], str) else ""
        match = MESSAGE_FROM_RE.match(head.split("\n", 1)[0])
        sender = _valid_id(match.group(1)) if match else None
        if message_id is None or target is None or sender is None:
            totals["malformed"] += 1
            continue
        con.execute(
            "INSERT INTO t3_messages(message_id, sender_thread_id,"
            " target_thread_id, sent_at) VALUES(?,?,?,?)"
            " ON CONFLICT(message_id) DO NOTHING",
            (message_id, sender, target,
             row["created_at"] if isinstance(row["created_at"], str)
             else None))
        totals["messages"] += 1


def _backfill_submissions(con: sqlite3.Connection) -> int:
    """Reclassify imported Claude submissions from T3 turn evidence.

    Only rows still carrying a live human-or-synthetic verdict
    (``synthetic`` or ``genuine``) move: ``command``, ``interrupt`` and
    ``scaffolding`` verdicts from the transcript import stay put (fail
    closed on contradictory records). Genuine rows gain their
    ``claude:<message id>`` turn when missing so later responses join;
    excerpts stay empty because the backfill never reads prompt text.
    """
    try:
        origins = con.execute(
            "SELECT message_id, actor_kind FROM t3_turn_origins").fetchall()
    except sqlite3.DatabaseError:
        return 0
    retouched = 0
    for origin in origins:
        native_id = f"claude:{origin['message_id']}"
        try:
            sub = con.execute(
                "SELECT kind, turn_id FROM submissions WHERE native_id=?",
                (native_id,)).fetchone()
        except sqlite3.DatabaseError:
            continue
        if sub is None or sub["kind"] not in ("synthetic", "genuine"):
            continue
        want = "genuine" if origin["actor_kind"] == "client" else "synthetic"
        if sub["kind"] == want and (want == "synthetic" or sub["turn_id"]):
            continue
        con.execute(
            "UPDATE submissions SET kind=?, is_genuine=? WHERE native_id=?",
            (want, 1 if want == "genuine" else 0, native_id))
        if want == "genuine" and not sub["turn_id"]:
            con.execute(
                "UPDATE submissions SET turn_id=? WHERE native_id=?",
                (native_id, native_id))
        retouched += 1
    return retouched


def _retouch_response_turns(con: sqlite3.Connection) -> int:
    """Join orphaned responses to their reclassified genuine turns.

    Responses imported while their prompt read ``synthetic`` carry a
    stale or missing ``turn_id``. Per source file, a response whose turn
    is missing or names no genuine submission joins the latest preceding
    genuine submission by ordinal; responses already on a genuine turn
    and responses before the first genuine prompt stay untouched.
    """
    try:
        genuine = con.execute(
            "SELECT session_key, source_id, ordinal_num, turn_id"
            " FROM submissions WHERE is_genuine=1 AND turn_id IS NOT NULL"
            " AND session_key LIKE 'claude:%'").fetchall()
    except sqlite3.DatabaseError:
        return 0
    by_scope: dict[tuple, list] = {}
    for sub in genuine:
        by_scope.setdefault(
            (sub["session_key"], sub["source_id"]), []).append(sub)
    for turns in by_scope.values():
        turns.sort(key=lambda s: (s["ordinal_num"]
                                  if isinstance(s["ordinal_num"], int)
                                  else 10 ** 18))
    known_turns = {s["turn_id"] for turns in by_scope.values()
                   for s in turns}
    retouched = 0
    for (session_key, source_id), turns in by_scope.items():
        try:
            responses = con.execute(
                "SELECT response_id, ordinal_num, turn_id FROM responses"
                " WHERE session_key=? AND source_id=? AND is_overlap=0",
                (session_key, source_id)).fetchall()
        except sqlite3.DatabaseError:
            continue
        for resp in responses:
            if resp["turn_id"] in known_turns:
                continue
            ordinal = resp["ordinal_num"]
            if not isinstance(ordinal, int):
                continue
            owner = None
            for sub in turns:
                sub_ord = sub["ordinal_num"]
                if isinstance(sub_ord, int) and sub_ord <= ordinal:
                    owner = sub["turn_id"]
                else:
                    break
            if owner is None or owner == resp["turn_id"]:
                continue
            con.execute("UPDATE responses SET turn_id=? WHERE response_id=?",
                        (owner, resp["response_id"]))
            retouched += 1
    return retouched


def _tree_sessions(con: sqlite3.Connection, threads: dict,
                   roots: dict[str, list[str]]) -> dict[str, set]:
    """Ledger session keys per tree root.

    Cursor sessions (plus Claude subagent children) cover the live
    native session; Claude turn uuids in ``submissions`` additionally
    cover sessions rotated away under the same thread.
    """
    out: dict[str, set] = {}
    try:
        ledger_sessions = con.execute(
            "SELECT session_key, harness, native_id FROM sessions").fetchall()
    except sqlite3.DatabaseError:
        return {root: set() for root in roots}
    known = {row["session_key"] for row in ledger_sessions}
    for root, members in roots.items():
        keys: set = set()
        for thread_id in members:
            for native_session in threads.get(thread_id, []):
                for sess_row in ledger_sessions:
                    harness, native_id = \
                        sess_row["harness"], sess_row["native_id"]
                    if harness == "claude" and (
                            native_id == native_session
                            or native_id.startswith(native_session + ":")):
                        keys.add(sess_row["session_key"])
                    elif harness in ("codex", "opencode") \
                            and native_id == native_session:
                        keys.add(sess_row["session_key"])
            try:
                for row in con.execute(
                        "SELECT DISTINCT s.session_key FROM submissions s"
                        " JOIN t3_turn_origins o ON s.native_id="
                        "'claude:' || o.message_id"
                        " WHERE o.thread_id=?", (thread_id,)):
                    keys.add(row["session_key"])
            except sqlite3.DatabaseError:
                pass
        # Only sessions the ledger knows; phantom bindings would read as
        # missing native usage instead of measured work.
        out[root] = keys & known
    return out


def _task_id(repository: str, number: int) -> str:
    return f"{repository}#{number}"


def _bind_trees(con: sqlite3.Connection, threads: dict,
                links: list) -> dict:
    """Create repo#N tasks and bind every tree session to every tree link."""
    roots: dict[str, list[str]] = {}
    for row in con.execute(
            "SELECT thread_id, root_thread_id FROM t3_threads").fetchall():
        roots.setdefault(row["root_thread_id"], []).append(row["thread_id"])
    # Threads that only appear in links (retired runtime rows) still
    # form trees so their links attribute any sessions found by uuid.
    for link in links:
        root = thread_root(link["thread_id"])
        roots.setdefault(root, [])
        if link["thread_id"] not in roots[root]:
            roots[root].append(link["thread_id"])
    tree_links: dict[str, list] = {}
    for link in links:
        tree_links.setdefault(thread_root(link["thread_id"]), []).append(link)
    sessions = _tree_sessions(con, threads, roots)
    tasks = 0
    bound = 0
    wanted: set = set()
    for root, root_links in tree_links.items():
        seen: dict[tuple, dict] = {}
        for link in root_links:
            seen.setdefault((link["repository"], link["number"]), link)
        for (repository, number), link in sorted(seen.items()):
            task_id = _task_id(repository, number)
            cur = con.execute(
                "INSERT OR IGNORE INTO tasks(task_id, project, title,"
                " issue_url, origin, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (task_id, repository, task_id, link["url"], HARNESS,
                 db.now()))
            tasks += cur.rowcount
            for session_key in sorted(sessions.get(root, set())):
                wanted.add((session_key, task_id))
                cur = con.execute(
                    "INSERT OR IGNORE INTO session_assignments(session_key,"
                    " task_id, evidence, created_at) VALUES(?,?,?,?)",
                    (session_key, task_id,
                     f"t3:{root}:{link['url']}", db.now()))
                bound += cur.rowcount
    # A tree still present in T3 state owns exactly its current links:
    # drop T3-made bindings its links no longer back (an unlinked or
    # changed PR). Trees absent from this read keep their history, and
    # bindings made outside this adapter are never touched.
    present = {thread_root(t) for t in threads} | set(tree_links)
    for root in sorted(present):
        for row in con.execute(
                "SELECT session_key, task_id FROM session_assignments"
                " WHERE substr(evidence, 1, ?)=?",
                (len(f"t3:{root}:"), f"t3:{root}:")).fetchall():
            if (row["session_key"], row["task_id"]) not in wanted:
                con.execute(
                    "DELETE FROM session_assignments"
                    " WHERE session_key=? AND task_id=?",
                    (row["session_key"], row["task_id"]))
    return {"tasks": tasks, "sessions_bound": bound}


def _ensure_t3_provenance(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS t3_outcome_provenance("
        " task_id TEXT PRIMARY KEY,"
        " acceptance_state TEXT, proof_ref TEXT)")


def _upsert_outcome(con: sqlite3.Connection, task_id: str,
                    acceptance: str, proof_ref: str | None) -> bool:
    """Task acceptance from T3 link evidence, never clobbering human rows.

    Only a matching ``t3_outcome_provenance`` snapshot marks a T3-owned
    row that may advance; any other pre-existing row (a human or capture
    verdict) survives re-import byte-for-byte.
    """
    _ensure_t3_provenance(con)
    row = con.execute(
        "SELECT * FROM outcomes WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO outcomes(task_id, candidate, proof_ref,"
            " acceptance_state, repairs, corrections, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (task_id, None, proof_ref, acceptance, None, None, db.now()))
        con.execute(
            "INSERT OR REPLACE INTO t3_outcome_provenance(task_id,"
            " acceptance_state, proof_ref) VALUES(?,?,?)",
            (task_id, acceptance, proof_ref))
        return True
    prov = con.execute(
        "SELECT * FROM t3_outcome_provenance WHERE task_id=?",
        (task_id,)).fetchone()
    if prov is None:
        return False
    current = (row["acceptance_state"], row["candidate"], row["proof_ref"],
               row["repairs"], row["corrections"])
    marked = (prov["acceptance_state"], None, prov["proof_ref"], None, None)
    if tuple(current) != tuple(marked):
        return False
    if row["acceptance_state"] == acceptance and row["proof_ref"] == proof_ref:
        return False
    con.execute(
        "UPDATE outcomes SET acceptance_state=?, proof_ref=?, updated_at=?"
        " WHERE task_id=?", (acceptance, proof_ref, db.now(), task_id))
    con.execute(
        "UPDATE t3_outcome_provenance SET acceptance_state=?, proof_ref=?"
        " WHERE task_id=?", (acceptance, proof_ref, task_id))
    return True


def _write_outcomes(con: sqlite3.Connection, links: list) -> int:
    """One outcome per T3 task: merged PR snapshots complete it.

    Every other PR state (open, closed, draft) and Issue-only links stay
    an explicit ``unknown`` with the link as proof; only tasks this
    adapter owns (``origin='t3'``) are written.
    """
    per_task: dict[str, list] = {}
    for link in links:
        per_task.setdefault(
            _task_id(link["repository"], link["number"]), []).append(link)
    written = 0
    for task_id in sorted(per_task):
        task = con.execute("SELECT origin FROM tasks WHERE task_id=?",
                           (task_id,)).fetchone()
        if task is None or task["origin"] != HARNESS:
            continue
        task_links = sorted(per_task[task_id],
                            key=lambda l: (l["kind"], l["url"]))
        merged = [l for l in task_links
                  if l["kind"] == "pr" and l["state"] == MERGED_STATE]
        if merged:
            acceptance, proof = "complete", merged[0]["url"]
        else:
            acceptance, proof = "unknown", task_links[0]["url"]
        if _upsert_outcome(con, task_id, acceptance, proof):
            written += 1
    return written
