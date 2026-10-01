"""Chromeria thread misuse report (#42) on a fabricated thread tree.

Fixture threads and messages mirror the real ``projection_threads`` and
``projection_thread_messages`` shapes and Chromeria's attribution line
(``[Message from <title> (thread <id>)]``, added only when the target is
not the sender's child). Every id, title and body is fabricated.
"""

import json
import os
import sqlite3
import subprocess
import sys

from agent_observer import misuse
from agent_observer.adapters import t3 as t3_adapter
from agent_observer.ingest import iso_ts
from tests.helpers import LedgerCase

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROOT = "r0000000-0000-4000-8000-000000000001"
DISP = f"sub.{ROOT}.dispatcher-aaaa00000001"
WORK = f"sub.{DISP}.worker-bbbb00000002"
DEEP3 = f"sub.{WORK}.cccc00000003"
DEEP4 = f"sub.{DEEP3}.reviewer-dddd00000004"
OTHER = "o0000000-0000-4000-8000-000000000002"
OTHER_WORKER = f"sub.{OTHER}.worker-eeee00000005"
SECRET = "fixture-body-never-stored"


def write_state(path, threads=(), messages=()):
    native = sqlite3.connect(path)
    native.executescript(
        "CREATE TABLE projection_threads(thread_id TEXT PRIMARY KEY,"
        " project_id TEXT, title TEXT, created_at TEXT, updated_at TEXT);"
        "CREATE TABLE projection_thread_messages(message_id TEXT PRIMARY KEY,"
        " thread_id TEXT, turn_id TEXT, role TEXT, text TEXT,"
        " is_streaming INTEGER, created_at TEXT, updated_at TEXT);")
    for thread_id, at in threads:
        native.execute(
            "INSERT INTO projection_threads VALUES(?,?,?,?,?)",
            (thread_id, "p1", "Fixture title " + SECRET, at, at))
    for message_id, target, role, text, at in messages:
        native.execute(
            "INSERT INTO projection_thread_messages(message_id, thread_id,"
            " role, text, is_streaming, created_at, updated_at)"
            " VALUES(?,?,?,?,0,?,?)", (message_id, target, role, text, at, at))
    native.commit()
    native.close()
    return path


def attributed(title, sender):
    return f"[Message from {title} (thread {sender})]\n\n{SECRET}"


THREADS = [
    (ROOT, "2026-09-30T09:00:00Z"),
    (DISP, "2026-09-30T09:01:00Z"),
    (WORK, "2026-09-30T09:02:00Z"),
    (DEEP3, "2026-09-30T09:03:00Z"),
    (DEEP4, "2026-10-01T09:04:00Z"),
    (OTHER, "2026-09-30T08:00:00Z"),
    (OTHER_WORKER, "2026-09-30T08:01:00Z"),
]

MESSAGES = [
    ("m1", DISP, "user", attributed("Worker: fix", WORK),
     "2026-09-30T10:00:00Z"),
    ("m2", OTHER, "user", attributed("Dispatcher (v2) [draft]", DISP),
     "2026-09-30T10:01:00Z"),
    ("m3", WORK, "user", attributed("Planner", ROOT),
     "2026-10-01T10:02:00Z"),
    ("m4", WORK, "user", attributed("Other worker", OTHER_WORKER),
     "2026-10-01T10:03:00Z"),
    ("m5", DISP, "user", attributed("Sibling", OTHER_WORKER.replace(
        OTHER, ROOT)), "2026-10-01T10:04:00Z"),
    # A very long title still yields its sender.
    ("m9", OTHER, "user", attributed("T" * 2000, DISP),
     "2026-09-30T10:08:00Z"),
    # Parent to own child: Chromeria adds no attribution, nothing listed.
    ("m6", WORK, "user", SECRET, "2026-09-30T10:05:00Z"),
    # An assistant quoting the line is not a delivery.
    ("m7", DISP, "assistant", attributed("Quoted", ROOT),
     "2026-09-30T10:06:00Z"),
    # A broken attribution line is malformed, never guessed.
    ("m8", DISP, "user", "[Message from nobody]\n\nx",
     "2026-09-30T10:07:00Z"),
]


class ThreadMisuseTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.state = write_state(os.path.join(self.tmp.name, "state.sqlite"),
                                 THREADS, MESSAGES)
        self.totals = t3_adapter.sync(self.con, source=self.state)

    def test_sync_mirrors_spawns_and_messages_without_text(self):
        self.assertEqual(self.totals["failed"], [])
        self.assertEqual(self.totals["spawns"], 5)
        self.assertEqual(self.totals["messages"], 6)
        self.assertEqual(self.totals["malformed"], 1)
        depths = {r["thread_id"]: r["depth"] for r in self.query(
            "SELECT thread_id, depth FROM t3_spawns")}
        self.assertEqual(depths, {DISP: 1, WORK: 2, DEEP3: 3, DEEP4: 4,
                                  OTHER_WORKER: 1})
        dump = "\n".join(self.con.iterdump())
        self.assertNotIn(SECRET, dump)
        self.assertNotIn("Dispatcher (v2)", dump)

    def test_lists_only_chains_deeper_than_two(self):
        result = misuse.report(self.con)
        spawns = result["deep_spawns"]
        self.assertEqual([s["thread_id"] for s in spawns], [DEEP3, DEEP4])
        self.assertEqual([s["depth"] for s in spawns], [3, 4])
        self.assertEqual(spawns[1]["role"], "reviewer")
        self.assertIsNone(spawns[0]["role"])
        self.assertEqual(spawns[1]["chain"], [
            {"thread_id": ROOT, "role": None},
            {"thread_id": DISP, "role": "dispatcher"},
            {"thread_id": WORK, "role": "worker"},
            {"thread_id": DEEP3, "role": None},
            {"thread_id": DEEP4, "role": "reviewer"}])
        self.assertEqual(spawns[1]["created_at"], "2026-10-01T09:04:00Z")

    def test_lists_messages_outside_own_children(self):
        listed = [(m["sender"], m["target"], m["relation"], m["sent_at"])
                  for m in misuse.report(self.con)["cross_messages"]]
        self.assertEqual(listed, [
            (WORK, DISP, "child to parent", "2026-09-30T10:00:00Z"),
            (DISP, OTHER, "across trees", "2026-09-30T10:01:00Z"),
            (DISP, OTHER, "across trees", "2026-09-30T10:08:00Z"),
            (ROOT, WORK, "to grandchild or deeper", "2026-10-01T10:02:00Z"),
            (OTHER_WORKER, WORK, "across trees", "2026-10-01T10:03:00Z"),
            (f"sub.{ROOT}.worker-eeee00000005", DISP, "within tree",
             "2026-10-01T10:04:00Z")])
        first = misuse.report(self.con)["cross_messages"][0]
        self.assertEqual((first["sender_role"], first["target_role"]),
                         ("worker", "dispatcher"))

    def test_window_selects_by_time(self):
        since = iso_ts("2026-10-01T00:00:00Z")
        result = misuse.report(self.con, since=since)
        self.assertEqual([s["thread_id"] for s in result["deep_spawns"]],
                         [DEEP4])
        self.assertEqual([m["sent_at"] for m in result["cross_messages"]],
                         ["2026-10-01T10:02:00Z", "2026-10-01T10:03:00Z",
                          "2026-10-01T10:04:00Z"])
        result = misuse.report(self.con, until=since)
        self.assertEqual([s["thread_id"] for s in result["deep_spawns"]],
                         [DEEP3])
        self.assertEqual(len(result["cross_messages"]), 3)

    def test_render_names_chain_and_messages(self):
        text = misuse.render(misuse.report(self.con))
        self.assertIn("spawn chains deeper than 2 levels: 2", text)
        self.assertIn(f"{WORK} (worker) > {DEEP3} (unknown)", text)
        self.assertIn(f"child to parent: {WORK} (worker) -> {DISP}"
                      " (dispatcher)", text)


class EmptyThreadMisuseTest(LedgerCase):
    def test_empty_results_say_none(self):
        state = write_state(os.path.join(self.tmp.name, "state.sqlite"),
                            [(ROOT, "2026-09-30T09:00:00Z"),
                             (DISP, "2026-09-30T09:01:00Z")],
                            [("m1", DISP, "user", SECRET,
                              "2026-09-30T10:00:00Z")])
        t3_adapter.sync(self.con, source=state)
        result = misuse.report(self.con)
        self.assertEqual((result["deep_spawns"], result["cross_messages"]),
                         ([], []))
        self.assertEqual(misuse.render(result),
                         "spawn chains deeper than 2 levels: 0\n  none\n"
                         "messages into a thread other than the sender's own"
                         " child: 0\n  none")

    def test_ledger_without_t3_import_says_so(self):
        result = misuse.report(self.con)
        self.assertFalse(result["imported"])
        self.assertIn("run agent-observer sync --harness t3",
                      misuse.render(result))


class ThreadMisuseCliTest(LedgerCase):
    def test_cli_reports_json_over_window(self):
        state = write_state(os.path.join(self.tmp.name, "state.sqlite"),
                            THREADS, MESSAGES)
        t3_adapter.sync(self.con, source=state)
        self.con.commit()
        proc = subprocess.run(
            [sys.executable, "-m", "agent_observer", "--db", self.db_path,
             "thread-misuse", "--since", "2026-10-01", "--json"],
            cwd=REPO, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual([s["thread_id"] for s in payload["deep_spawns"]],
                         [DEEP4])
        self.assertEqual(len(payload["cross_messages"]), 3)
