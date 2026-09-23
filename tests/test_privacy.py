"""Private ledger stays private: permissions, location, no secret leakage."""

import os
import stat
import tempfile
import unittest

from agent_observer import db
from tests.helpers import LedgerCase, fixture
from agent_observer.adapters.codex import import_codex_file


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class LedgerPermissionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "ledger")
        self.db_path = os.path.join(self.dir, "observer.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_ledger_is_owner_only_under_a_permissive_umask(self):
        old = os.umask(0o022)
        try:
            con = db.connect(self.db_path)
            db.init_db(con)
            con.close()
        finally:
            os.umask(old)
        self.assertEqual(mode(self.dir), 0o700)
        self.assertEqual(mode(self.db_path), 0o600)

    def test_existing_loose_ledger_is_tightened_and_stays_usable(self):
        os.makedirs(self.dir)
        os.chmod(self.dir, 0o755)
        with open(self.db_path, "w"):
            pass
        os.chmod(self.db_path, 0o644)
        con = db.connect(self.db_path)
        db.init_db(con)
        db.upsert_session(con, "codex:s", "codex", "s", None)
        con.commit()
        con.close()
        self.assertEqual(mode(self.dir), 0o700)
        self.assertEqual(mode(self.db_path), 0o600)
        again = db.connect(self.db_path)
        row = again.execute(
            "SELECT session_key FROM sessions WHERE session_key='codex:s'").fetchone()
        again.close()
        self.assertIsNotNone(row)

    def test_live_sidecars_are_owner_only(self):
        con = db.connect(self.db_path)
        db.init_db(con)
        con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES('codex','p','x',0)")
        for i in range(50):
            con.execute(
                "INSERT INTO responses(response_id, source_id, harness,"
                " session_key, total_tokens) VALUES(?,?,?,?,?)",
                (f"codex:r{i}", 1, "codex", "codex:s", i))
        con.commit()
        sidecars = [self.db_path + "-wal", self.db_path + "-shm"]
        self.assertTrue(any(os.path.exists(p) for p in sidecars),
                        "expected live SQLite sidecars while connected")
        for path in sidecars:
            if os.path.exists(path):
                self.assertEqual(mode(path), 0o600, path)
        con.close()


class LedgerLocationTest(unittest.TestCase):
    def test_default_ledger_lives_outside_any_repo(self):
        home = os.path.expanduser("~")
        self.assertTrue(db.default_path().startswith(
            os.path.join(home, ".local", "state", "agent-observer")))


class LedgerSecretScanTest(LedgerCase):
    def test_quarantine_fixture_leaks_no_secret_anywhere(self):
        import_codex_file(self.con, fixture("codex-secrets-quarantine.jsonl"))
        secret = "SECRET-QUARANTINE-9f8e7d6c"
        text_columns = {
            "import_errors": ("error", "line_excerpt"),
            "submissions": ("text_excerpt",),
            "events": ("name", "target", "detail_json"),
            "responses": ("model", "semantics"),
            "sessions": ("project_dir", "identity_json"),
            "sources": ("path",),
        }
        for table, columns in text_columns.items():
            for column in columns:
                for row in self.query(f"SELECT {column} FROM {table}"):
                    self.assertNotIn(secret, row[column] or "",
                                     f"{table}.{column}")
