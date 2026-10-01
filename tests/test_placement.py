"""A session shared by several PRs splits per response by checkout (#43)."""

import os
import shutil
import subprocess

from agent_observer import db, placement, report
from agent_observer.adapters import t3 as t3_adapter
from tests.helpers import LedgerCase

SCHEDULE = {"models": {"m": {"input": 1.0, "output": 1.0, "cached_input": 1.0,
                              "cache_write": 1.0}}}


def _git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)


class PlacementTest(LedgerCase):
    def setUp(self):
        super().setUp()
        t3_adapter._ensure_tables(self.con)
        self.checkouts = {}
        for repo, branch in (("alpha", "fix/a"), ("beta", "fix/b")):
            root = os.path.join(self.tmp.name, f"{repo}-wt")
            _git("init", "-q", "-b", branch, root)
            _git("-C", root, "remote", "add", "origin", f"git@github.com:o/{repo}.git")
            _git("-C", root, "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-q", "--allow-empty", "-m", "start")
            self.checkouts[repo] = root
        self.source = self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at) VALUES('x','p','s',0)"
        ).lastrowid
        for n, (repo, branch) in enumerate((("alpha", "fix/a"), ("beta", "fix/b")), 1):
            task = f"o/{repo}#{n}"
            self.con.execute("INSERT INTO tasks(task_id, project, title, created_at)"
                             " VALUES(?,?,?,0)", (task, f"o/{repo}", task))
            self.con.execute(
                "INSERT INTO t3_links(thread_id, kind, host, repository, number, url,"
                " head_branch) VALUES('t','pr','github.com',?,?,?,?)",
                (f"o/{repo}", n, f"https://github.com/o/{repo}/pull/{n}", branch))

    def session(self, harness, calls):
        """calls: (response ordinal, tool-call ordinal, target) per response."""
        key = f"{harness}:s"
        db.upsert_session(self.con, key, harness, "s", None, project_dir="/nowhere")
        for task in ("o/alpha#1", "o/beta#2"):
            self.con.execute("INSERT INTO session_assignments(session_key, task_id,"
                             " evidence, created_at) VALUES(?,?,'t3',0)", (key, task))
        for i, (resp, call, target) in enumerate(calls):
            self.con.execute(
                "INSERT INTO responses(response_id, source_id, harness, session_key,"
                " ordinal_num, model, input_tokens, output_tokens, total_tokens)"
                " VALUES(?,?,?,?,?,'m',100,10,110)",
                (f"r{i}", self.source, harness, key, resp))
            if target is not None:
                self.con.execute(
                    "INSERT INTO events(source_id, session_key, ordinal_num, family,"
                    " native_id, name, target) VALUES(?,?,?,'tool_call',?,'Bash',?)",
                    (self.source, key, call, f"e{i}", target))
        return key

    def claude_session(self):
        a, b = self.checkouts["alpha"], self.checkouts["beta"]
        return self.session("claude", [
            (1, 2, f"cd {a} && git status"),        # alpha
            (3, 4, f"python3 {b}/x.py"),             # beta
            (5, 6, f"diff {a}/f {b}/f"),             # both: unplaced
            (7, 8, "gh pr list"),                    # none: unplaced
            (9, None, None),                         # no tool call: unplaced
        ])

    def test_each_pr_counts_only_its_own_responses_and_totals_reconcile(self):
        self.claude_session()
        alpha = report.task_report(self.con, "o/alpha#1", schedule=SCHEDULE)
        beta = report.task_report(self.con, "o/beta#2", schedule=SCHEDULE)
        self.assertEqual(alpha["measured"]["attributed_responses"], ["r0"])
        self.assertEqual(beta["measured"]["attributed_responses"], ["r1"])
        self.assertEqual(alpha["measured"]["unplaced_responses"], ["r2", "r3", "r4"])
        self.assertEqual(alpha["measured"]["other_task_responses"], ["r1"])
        self.assertEqual(alpha["shared_joint"]["responses"], 0)
        self.assertEqual(alpha["total_cost"]["responses"], 1)
        self.assertEqual(alpha["total_cost"]["unplaced"]["responses"], 3)
        self.assertTrue(alpha["reconciles"])
        self.assertTrue(beta["reconciles"])

    def test_codex_calls_belong_to_the_next_response(self):
        a, b = self.checkouts["alpha"], self.checkouts["beta"]
        self.session("codex", [(2, 1, f"ls {a}"), (4, 3, f"ls {b}")])
        placed = placement.place_session(self.con, "codex:s")
        self.assertEqual(placed, {"r0": "o/alpha#1", "r1": "o/beta#2"})

    def test_removed_worktree_still_places_from_recorded_checkouts(self):
        self.claude_session()
        self.assertEqual(placement.record_shared_checkouts(self.con), 1)
        for root in self.checkouts.values():
            shutil.rmtree(root)
        placed = placement.place_session(self.con, "claude:s")
        self.assertEqual((placed["r0"], placed["r1"]), ("o/alpha#1", "o/beta#2"))

    def test_without_head_branches_the_session_stays_shared_whole(self):
        self.claude_session()
        self.con.execute("UPDATE t3_links SET head_branch=NULL")
        alpha = report.task_report(self.con, "o/alpha#1", schedule=SCHEDULE)
        self.assertEqual(alpha["shared_joint"]["responses"], 5)
        self.assertEqual(alpha["attributed"]["responses"], 0)

    def test_unsupported_harness_stays_shared_whole(self):
        self.session("opencode", [(1, 2, f"ls {self.checkouts['alpha']}")])
        self.assertIsNone(placement.place_session(self.con, "opencode:s"))

    def test_detached_checkout_matches_no_pr(self):
        root = self.checkouts["alpha"]
        _git("-C", root, "checkout", "-q", "--detach")
        self.assertEqual(placement.checkout_of(self.con, root), ("o/alpha", None))
