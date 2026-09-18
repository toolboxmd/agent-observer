"""Shared test helpers."""

import os
import sqlite3
import tempfile
import unittest

from agent_observer import db
from agent_observer.codex import import_codex_file

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    return os.path.join(FIXTURES, name)


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.con = db.connect(self.db_path)
        db.init_db(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def sync(self, name):
        return import_codex_file(self.con, fixture(name))

    def query(self, sql, args=()):
        return self.con.execute(sql, args).fetchall()
