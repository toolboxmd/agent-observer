"""Ledger writers take one exclusive lock and release it (#78).

A second process is the holder. The waiting stderr line, or the holder's
pipe, is the milestone: tests do not sleep to decide ordering. The repeated
in-process case uses a short patched wait so a lock that is never released
fails here instead of hanging for the real 600 s budget.

Plain ``sync --root <empty>`` exits 1 because OpenCode and Router report
missing sources. These tests name one harness and an empty Codex root,
which is a valid fixture with nothing to import.
"""

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_observer import cli, db

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Holds the writer lock until stdin closes the wait. Prints one milestone.
_HOLDER = """
import sys
from agent_observer.db import acquire_writer_lock, release_writer_lock
fd = acquire_writer_lock(sys.argv[1])
sys.stdout.write("held\\n")
sys.stdout.flush()
sys.stdin.readline()
release_writer_lock(fd)
"""


class _WaitStderr(io.TextIOBase):
    """Stderr that signals once the writer announces it is waiting."""

    def __init__(self):
        self.parts = []
        self.seen = threading.Event()

    def write(self, text):
        self.parts.append(text)
        if "waiting for another writer" in "".join(self.parts):
            self.seen.set()
        return len(text)

    def flush(self):
        return None

    def text(self):
        return "".join(self.parts)


class WriterLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "observer.db")
        self.empty = os.path.join(self.tmp.name, "codex-empty")
        os.makedirs(self.empty)
        self.holders = []

    def tearDown(self):
        for proc in self.holders:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
        self.tmp.cleanup()

    def run_cli(self, *argv, db_path=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--db", db_path or self.db_path, *argv])
        return code, out.getvalue(), err.getvalue()

    def writer(self, db_path=None):
        return self.run_cli("sync", "--harness", "codex", "--root", self.empty,
                            db_path=db_path)

    def hold(self, db_path=None):
        env = os.environ.copy()
        env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", _HOLDER, db_path or self.db_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=REPO, env=env)
        self.holders.append(proc)
        line = proc.stdout.readline()
        if line != b"held\n":
            err = proc.stderr.read()
            self.fail(f"holder did not take the lock: {line!r} {err!r}")
        return proc

    def release(self, proc):
        if proc.poll() is None:
            proc.stdin.write(b"\n")
            proc.stdin.flush()
            proc.wait(timeout=5)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()

    def test_holder_blocks_writer_until_release(self):
        holder = self.hold()
        err = _WaitStderr()
        code = []

        def run():
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code.append(cli.main([
                    "--db", self.db_path, "sync", "--harness", "codex",
                    "--root", self.empty]))

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(err.seen.wait(5), err.text())
        real = os.path.realpath(self.db_path)
        self.assertIn(f"waiting for another writer of {real}", err.text())
        self.release(holder)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(code, [0], err.text())

    def test_bounded_wait_exits_busy(self):
        holder = self.hold()
        try:
            with mock.patch.object(db, "WRITER_LOCK_WAIT_S", 0.2):
                code, _out, err = self.writer()
        finally:
            self.release(holder)
        real = os.path.realpath(self.db_path)
        self.assertEqual(code, 2, err)
        self.assertIn(
            f"agent-observer: ledger {real} is busy "
            "(another writer held it for 0.2 s)", err)
        self.assertEqual(err.count("waiting for another writer"), 1)

    def test_repeated_inprocess_writers_succeed(self):
        # A leaked lock makes the second call hit this short budget and
        # exit 2. Releasing the lock lets it take the lock at once.
        with mock.patch.object(db, "WRITER_LOCK_WAIT_S", 0.25):
            first, _out, err = self.writer()
            self.assertEqual(first, 0, err)
            second, _out, err = self.writer()
        self.assertEqual(second, 0, err)
        self.assertNotIn("waiting for another writer", err)

    def test_lock_released_after_migration_failure(self):
        with mock.patch.object(db, "WRITER_LOCK_WAIT_S", 0.25):
            with mock.patch.object(
                    db, "init_db",
                    side_effect=sqlite3.DatabaseError("migration failed")):
                code, _out, err = self.writer()
            self.assertEqual(code, 2, err)
            self.assertIn("migration failed", err)
            self.assertNotIn("is busy", err)
            code, _out, err = self.writer()
        self.assertEqual(code, 0, err)

    def test_symlink_contends_with_the_real_path(self):
        link = os.path.join(self.tmp.name, "links", "alias.db")
        os.makedirs(os.path.dirname(link))
        os.symlink(self.db_path, link)
        holder = self.hold(self.db_path)
        err = _WaitStderr()
        code = []

        def run():
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code.append(cli.main([
                    "--db", link, "sync", "--harness", "codex",
                    "--root", self.empty]))

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(err.seen.wait(5), err.text())
        real = os.path.realpath(self.db_path)
        self.assertIn(f"waiting for another writer of {real}", err.text())
        self.assertTrue(os.path.exists(real + ".lock"))
        self.assertFalse(os.path.exists(link + ".lock"))
        self.release(holder)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(code, [0], err.text())

    def test_read_only_runs_while_the_lock_is_held(self):
        code, _out, err = self.writer()
        self.assertEqual(code, 0, err)
        holder = self.hold()
        done = threading.Event()
        box = {}

        def run():
            box["result"] = self.run_cli("sessions", "list")
            done.set()

        thread = threading.Thread(target=run)
        thread.start()
        finished = done.wait(2)
        self.release(holder)
        thread.join(5)
        self.assertTrue(finished, box.get("result"))
        code, out, err = box["result"]
        self.assertEqual(code, 0, err)
        self.assertNotIn("waiting for another writer", err)
        self.assertIn("no sessions", out)

    def test_text_sync_names_each_failed_source(self):
        root = os.path.join(self.tmp.name, "empty-root")
        os.makedirs(root)
        code, out, err = self.run_cli(
            "sync", "--harness", "opencode", "--root", root)
        path = os.path.join(root, "opencode.db")
        self.assertEqual(code, 1, err + out)
        self.assertIn("1 sources failed", out)
        self.assertIn(f"  failed opencode {path}: not found", out)
        code, out, err = self.run_cli(
            "sync", "--harness", "opencode", "--root", root, "--json")
        self.assertEqual(code, 1, err + out)
        payload = json.loads(out)
        self.assertEqual(payload["harnesses"][0]["failed"],
                         [{"path": path, "error": "not found"}])
        self.assertNotIn("  failed ", out)


if __name__ == "__main__":
    unittest.main()
