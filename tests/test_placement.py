"""A session shared by several PRs splits per response by checkout (#43)."""

import hashlib
import io
import json
import os
import shutil
import subprocess
from contextlib import closing, redirect_stdout
from unittest.mock import patch

from agent_observer import cli, db, placement, report
from agent_observer.adapters import t3 as t3_adapter
from agent_observer.adapters.claude import import_claude_file
from agent_observer.adapters.codex import import_codex_file
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

    def session(self, harness, calls, *, tasks=("o/alpha#1", "o/beta#2"), missing=()):
        """calls: (response ordinal, tool-call ordinal, target) per response."""
        key = f"{harness}:s"
        db.upsert_session(self.con, key, harness, "s", None, project_dir="/nowhere")
        for task in tasks:
            self.con.execute("INSERT INTO session_assignments(session_key, task_id,"
                             " evidence, created_at) VALUES(?,?,'t3',0)", (key, task))
        new_events = []
        for i, (resp, call, target) in enumerate(calls):
            self.con.execute(
                "INSERT INTO responses(response_id, source_id, harness, session_key,"
                " ordinal_num, model, input_tokens, output_tokens, total_tokens)"
                " VALUES(?,?,?,?,?,'m',100,10,110)",
                (f"r{i}", self.source, harness, key, resp))
            if target is not None:
                event = self.con.execute(
                    "INSERT INTO events(source_id, session_key, ordinal_num, family,"
                    " native_id, name, target) VALUES(?,?,?,'tool_call',?,'Bash',?)",
                    (self.source, key, call, f"e{i}", target))
                if i not in missing and harness in placement.CALL_FOLLOWS_RESPONSE:
                    new_events.append((event.lastrowid, target))
        self.con.commit()
        placement.observe_new_checkouts(self.con, new_events)
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
        placement.record_shared_checkouts(self.con)
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
        placement.record_shared_checkouts(self.con)
        placed = placement.read_placements(self.con, "codex:s")
        self.assertEqual(placed, {"r0": "o/alpha#1", "r1": "o/beta#2"})

    def test_removed_worktree_still_places_from_recorded_checkouts(self):
        self.claude_session()
        self.assertEqual(placement.record_shared_checkouts(self.con), 1)
        for root in self.checkouts.values():
            shutil.rmtree(root)
        placed = placement.read_placements(self.con, "claude:s")
        self.assertEqual((placed["r0"], placed["r1"]), ("o/alpha#1", "o/beta#2"))

    def _append_claude_call(self, path, session, response, root):
        record = {"type": "assistant", "sessionId": session, "message": {
            "id": response, "model": "m", "usage": {"input_tokens": 10, "output_tokens": 2},
            "content": [{"type": "tool_use", "id": f"call-{response}", "name": "Bash",
                         "input": {"command": f"cat {root}/x.py"}}],
        }}
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")

    def _assign(self, key, task):
        self.con.execute(
            "INSERT INTO session_assignments(session_key,task_id,evidence,created_at)"
            " VALUES(?,?,'t3',0)", (key, task))

    def test_late_link_keeps_the_first_imports_checkout_after_path_reuse(self):
        for session, t3_first in (("t3-first", True), ("native-first", False)):
            with self.subTest(order=session):
                key = f"claude:{session}"
                path = os.path.join(self.tmp.name, f"{session}.jsonl")
                root = self.checkouts["alpha"]
                _git("-C", root, "checkout", "-q", "fix/a")
                _git("-C", root, "remote", "set-url", "origin", "git@github.com:o/alpha.git")
                self.con.execute("DELETE FROM t3_links WHERE number=2")
                if t3_first:
                    self._assign(key, "o/alpha#1")
                self._append_claude_call(path, session, f"{session}-1", root)
                import_claude_file(self.con, path)
                if not t3_first:
                    self._assign(key, "o/alpha#1")
                placement.record_shared_checkouts(self.con)
                _git("-C", root, "checkout", "-q", "-B", "fix/b")
                _git("-C", root, "remote", "set-url", "origin", "git@github.com:o/beta.git")
                self.con.execute(
                    "INSERT INTO t3_links(thread_id,kind,host,repository,number,url,head_branch)"
                    " VALUES('t','pr','github.com','o/beta',2,"
                    " 'https://github.com/o/beta/pull/2','fix/b')")
                self._assign(key, "o/beta#2")
                self._append_claude_call(path, session, f"{session}-2", root)
                self.assertTrue(import_claude_file(self.con, path)["incremental"])
                placement.record_shared_checkouts(self.con)
                self.assertEqual(placement.read_placements(self.con, key), {
                    f"claude:{session}-1": "o/alpha#1", f"claude:{session}-2": "o/beta#2"})

    def test_replay_of_old_ledger_captures_only_verified_new_records(self):
        key = "claude:old-ledger"
        path = os.path.join(self.tmp.name, "old-ledger.jsonl")
        root = self.checkouts["alpha"]
        self._append_claude_call(path, "old-ledger", "before", root)
        # Simulate an older writer that indexed the native rows without observing Git.
        with patch.object(placement, "observe_new_checkouts"):
            import_claude_file(self.con, path)
        # Its parser also missed the tool call. Recovering an event from the
        # old prefix still cannot make an observation of today's checkout timely.
        self.con.execute("DELETE FROM events WHERE native_id='call-before'")
        for task in ("o/alpha#1", "o/beta#2"):
            self._assign(key, task)
        _git("-C", root, "checkout", "-q", "-b", "fix/b")
        _git("-C", root, "remote", "set-url", "origin", "git@github.com:o/beta.git")
        with patch.object(placement, "_git", side_effect=AssertionError("old checkout scanned")):
            self.assertEqual(import_claude_file(self.con, path, full=True)["events_inserted"], 1)
            placement.record_shared_checkouts(self.con)
        self.assertIsNone(placement.read_placements(self.con, key).get("claude:before"))

        self._append_claude_call(path, "old-ledger", "after", root)
        # A parser-driven replay must still admit a provably appended record.
        self.con.execute("UPDATE sources SET claude_usage_version=0")
        self.assertFalse(import_claude_file(self.con, path)["incremental"])
        self.assertEqual(placement.read_placements(self.con, key), {
            "claude:before": None, "claude:after": "o/beta#2"})

    def test_codex_observes_call_before_usage_arrives(self):
        key = "codex:placement-review"
        path, _ = self._continuation_files("live")
        with open(path) as fh:
            records = fh.readlines()[:4]
        with open(path, "w") as fh:
            fh.writelines(records[:3])
            fh.write(records[3].rstrip("\n"))
        import_codex_file(self.con, path)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 0)
        self._bind_placement_tasks(self.con)
        shutil.rmtree(self.checkouts["alpha"])
        with open(path, "a") as fh:
            fh.write("\n")
        with patch.object(placement, "_git", side_effect=AssertionError("checkout scanned again")):
            self.assertTrue(import_codex_file(self.con, path)["incremental"])
            self.assertEqual(placement.read_placements(self.con, key),
                             {"codex:r1": "o/alpha#1"})

    def test_checkout_on_another_forge_does_not_credit_github(self):
        root = self.checkouts["alpha"]
        _git("-C", root, "remote", "set-url", "origin", "git@forge.example:o/alpha.git")
        key = self.claude_session()
        placement.record_shared_checkouts(self.con)
        placed = placement.read_placements(self.con, key)
        self.assertIsNone(placed["r0"])
        self.assertEqual(placed["r1"], "o/beta#2")

    def test_same_repository_and_head_on_two_forges(self):
        github = self.checkouts["alpha"]
        hosted = os.path.join(self.tmp.name, "hosted-alpha")
        shutil.copytree(github, hosted)
        _git("-C", hosted, "remote", "set-url", "origin", "https://FORGE.example/o/alpha.git")
        task = "forge.example/o/alpha#1"
        self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES(?,0)", (task,))
        self.con.execute(
            "INSERT INTO t3_links(thread_id,kind,host,repository,number,url,head_branch)"
            " VALUES('t','pr','Forge.Example','o/alpha',1,"
            " 'https://forge.example/o/alpha/pull/1','fix/a')")
        key = self.session("claude", [
            (1, 2, f"ls {github}"),
            (3, 4, f"ls {hosted}"),
            (5, 6, f"diff {github}/f {hosted}/f"),
        ], tasks=("o/alpha#1", task))
        expected = {"r0": "o/alpha#1", "r1": task, "r2": None}
        placement.record_shared_checkouts(self.con)
        self.assertEqual(placement.read_placements(self.con, key), expected)
        shutil.rmtree(github)
        shutil.rmtree(hosted)
        with closing(db.connect_read_only(self.db_path)) as ro, patch.object(
                placement, "_git", side_effect=AssertionError("report ran git")):
            self.assertEqual(placement.read_placements(ro, key), expected)
            for task_id, response in (("o/alpha#1", "r0"), (task, "r1")):
                measured = report.task_report(ro, task_id, schedule=SCHEDULE)["measured"]
                self.assertEqual(measured["attributed_responses"], [response])
                self.assertEqual(measured["unplaced_responses"], ["r2"])

    def test_pr_head_from_another_forge_cannot_supply_a_github_match(self):
        root = self.checkouts["alpha"]
        _git("-C", root, "checkout", "-q", "-b", "fix/hosted")
        self.con.execute(
            "INSERT INTO t3_links(thread_id,kind,host,repository,number,url,head_branch)"
            " VALUES('t','pr','forge.example','o/alpha',1,"
            " 'https://forge.example/o/alpha/pull/1','fix/hosted')")
        key = self.claude_session()
        placement.record_shared_checkouts(self.con)
        self.assertIsNone(placement.read_placements(self.con, key)["r0"])

    def test_historical_checkout_without_import_provenance_cannot_place(self):
        alpha = self.checkouts["alpha"]
        key = self.session("claude", [
            (1, 2, f"ls {alpha}/f"),
            (3, 4, f"ls {alpha}/f"),
        ], missing=(0,))
        self.con.execute(
            "CREATE TABLE response_checkouts("
            " event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,"
            " path_digest BLOB NOT NULL, response_id TEXT, host TEXT,"
            " repository TEXT, branch TEXT,"
            " PRIMARY KEY(event_id, path_digest))")
        event_id = self.con.execute("SELECT id FROM events WHERE native_id='e0'").fetchone()[0]
        digest = hashlib.sha256(f"{alpha}/f".encode()).digest()
        self.con.execute(
            "INSERT INTO response_checkouts VALUES(?,?,'r0','github.com','o/alpha','fix/a')",
            (event_id, digest))
        self.con.commit()
        with closing(db.connect_read_only(self.db_path)) as ro:
            self.assertIsNone(placement.read_placements(ro, key).get("r0"))
        for _ in range(2):
            placement.record_shared_checkouts(self.con)
            self.assertEqual(placement.read_placements(self.con, key),
                             {"r0": None, "r1": "o/alpha#1"})

    def test_without_head_branches_the_session_stays_shared_whole(self):
        self.claude_session()
        self.con.execute("UPDATE t3_links SET head_branch=NULL")
        placement.record_shared_checkouts(self.con)
        alpha = report.task_report(self.con, "o/alpha#1", schedule=SCHEDULE)
        self.assertEqual(alpha["shared_joint"]["responses"], 5)
        self.assertEqual(alpha["attributed"]["responses"], 0)

    def test_heads_linked_after_checkout_retirement_use_the_original_observation(self):
        key = self.claude_session()
        self.con.execute("UPDATE t3_links SET head_branch=NULL")
        placement.record_shared_checkouts(self.con)
        shutil.rmtree(self.checkouts["alpha"])
        self.con.execute("UPDATE t3_links SET head_branch='fix/a' WHERE number=1")
        self.assertEqual(placement.read_placements(self.con, key)["r0"], "o/alpha#1")

    def test_unsupported_harness_stays_shared_whole(self):
        self.session("opencode", [(1, 2, f"ls {self.checkouts['alpha']}")])
        self.assertIsNone(placement.read_placements(self.con, "opencode:s"))

    def test_detached_checkout_matches_no_pr(self):
        _git("-C", self.checkouts["alpha"], "checkout", "-q", "--detach")
        key = self.claude_session()
        placement.record_shared_checkouts(self.con)
        self.assertIsNone(placement.read_placements(self.con, key)["r0"])

    def test_reused_checkout_path_preserves_old_and_new_response_witnesses(self):
        key = self.claude_session()
        placement.record_shared_checkouts(self.con)
        root = self.checkouts["alpha"]
        _git("-C", root, "checkout", "-q", "-b", "fix/b")
        _git("-C", root, "remote", "set-url", "origin", "git@github.com:o/beta.git")
        self.con.execute("INSERT INTO responses(response_id,source_id,session_key,"
                         " ordinal_num) VALUES('new',?,?,10)", (self.source, key))
        event = self.con.execute(
            "INSERT INTO events(session_key,ordinal_num,family,native_id,target)"
            " VALUES(?,11,'read','new-call',?)", (key, root))
        self.assertIsNone(placement.read_placements(self.con, key)["new"])
        self.con.commit()
        placement.observe_new_checkouts(self.con, [(event.lastrowid, root)])
        shutil.rmtree(root)
        placement.record_shared_checkouts(self.con)
        placed = placement.read_placements(self.con, key)
        self.assertEqual((placed["r0"], placed["new"]), ("o/alpha#1", "o/beta#2"))

    def test_removed_checkout_without_response_evidence_does_not_place(self):
        key = self.session("claude", [(1, 2, f"ls {self.checkouts['alpha']}")], missing=(0,))
        root = self.checkouts["alpha"]
        self.con.execute("INSERT INTO checkouts(root,repository,branch,seen_at)"
                         " VALUES(?,'o/alpha','fix/a',0)", (root,))
        shutil.rmtree(root)
        self.assertEqual(placement.read_placements(self.con, key), {})
        placement.record_shared_checkouts(self.con)
        self.assertEqual(placement.read_placements(self.con, key), {})

    def test_checkout_without_origin_cannot_gain_a_retroactive_match(self):
        root = self.checkouts["alpha"]
        _git("-C", root, "remote", "remove", "origin")
        key = self.claude_session()
        placement.record_shared_checkouts(self.con)
        _git("-C", root, "remote", "add", "origin", "git@github.com:o/alpha.git")
        placement.record_shared_checkouts(self.con)
        self.assertIsNone(placement.read_placements(self.con, key)["r0"])

    def test_identical_heads_are_ambiguous_even_if_linked_after_sync(self):
        key = self.claude_session()
        placement.record_shared_checkouts(self.con)
        self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES('o/alpha#3',0)")
        self.con.execute("INSERT INTO session_assignments VALUES(?,'o/alpha#3','t3',0)", (key,))
        self.con.execute("INSERT INTO t3_links(thread_id,kind,host,repository,number,url,"
                         " head_branch) VALUES('t','pr','github.com','o/alpha',3,'url','fix/a')")
        placed = placement.read_placements(self.con, key)
        self.assertIsNone(placed["r0"])
        self.assertEqual(placed["r1"], "o/beta#2")

    def test_explicit_session_owner_preserves_whole_sharing(self):
        key = self.claude_session()
        placement.record_shared_checkouts(self.con)
        self.con.execute("UPDATE session_assignments SET evidence='router:job'"
                         " WHERE task_id='o/alpha#1'")
        self.assertIsNone(placement.read_placements(self.con, key))
        alpha = report.task_report(self.con, "o/alpha#1", schedule=SCHEDULE)
        self.assertEqual(alpha["shared_joint"]["responses"], 5)

    def test_read_only_ledger_still_splits(self):
        # publish and task open the ledger read-only (cli.py).
        self.claude_session()
        placement.record_shared_checkouts(self.con)
        self.con.commit()
        ro = db.connect_read_only(self.db_path)
        try:
            alpha = report.task_report(ro, "o/alpha#1", schedule=SCHEDULE)
        finally:
            ro.close()
        self.assertEqual(alpha["measured"]["attributed_responses"], ["r0"])

    def test_report_uses_only_ledger_reads_and_keeps_its_snapshot(self):
        self.claude_session()
        placement.record_shared_checkouts(self.con)
        ro = db.connect_read_only(self.db_path)
        self.addCleanup(ro.close)
        statements = []
        ro.set_trace_callback(statements.append)
        self.con.execute("UPDATE responses SET input_tokens=1100, total_tokens=1110"
                         " WHERE response_id='r0'")
        self.con.commit()
        with patch.object(placement, "_git", side_effect=AssertionError("report ran git")):
            alpha = report.task_report(ro, "o/alpha#1", schedule=SCHEDULE)
        self.assertTrue(ro.in_transaction)
        self.assertEqual({s.split()[0].upper() for s in statements} -
                         {"SELECT", "WITH", "PRAGMA"}, set())
        self.assertTrue(alpha["reconciles"])
        self.assertEqual(alpha["attributed"]["total_tokens"], 110)
        self.assertEqual(alpha["scope"]["total_tokens"], 550)

    def test_repeated_usage_keeps_the_first_response_on_its_task(self):
        # An exact later copy of the first response must not take its
        # chronology. Placement then assigns both calls to one response
        # and both task reports lose their attributed response.
        alpha = self.checkouts["alpha"]
        beta = self.checkouts["beta"]
        path = os.path.join(self.tmp.name, "placement.jsonl")

        def call(name, root):
            return {"type": "response_item", "payload": {
                "type": "function_call", "call_id": name, "name": "exec_command",
                "arguments": json.dumps({"cmd": "cat " + os.path.join(root, "x.py")})}}

        def usage(name, total):
            return {"type": "token_usage_record", "payload": {
                "response_id": name, "thread_id": "placement-review",
                "usage": {"input_tokens": total - 2, "output_tokens": 2,
                          "total_tokens": total}}}

        rows = [
            {"type": "session_meta", "payload": {
                "id": "placement-review", "thread_source": "user", "cwd": "/w"}},
            {"type": "turn_context", "payload": {"model": "model-a"}},
            call("call-a", alpha),
            usage("r1", 12),
            {"type": "turn_context", "payload": {"model": "model-b"}},
            call("call-b", beta),
            usage("r2", 22),
            usage("r1", 12),
        ]
        for ordinal, rec in enumerate(rows):
            rec["ordinal"] = ordinal
        with open(path, "w") as fh:
            for rec in rows:
                fh.write(json.dumps(rec) + "\n")
        key = "codex:placement-review"
        for task in ("o/alpha#1", "o/beta#2"):
            self.con.execute(
                "INSERT INTO session_assignments(session_key, task_id,"
                " evidence, created_at) VALUES(?,?,'t3',0)", (key, task))
        for full in (False, True, False):
            import_codex_file(self.con, path, full=full)
            stored = dict(self.con.execute(
                "SELECT response_id, ordinal_num FROM responses"))
            self.assertEqual(stored, {"codex:r1": 3, "codex:r2": 6})
            placement.record_shared_checkouts(self.con)
            self.assertEqual(placement.read_placements(self.con, key), {
                "codex:r1": "o/alpha#1", "codex:r2": "o/beta#2"})
            alpha_report = report.task_report(self.con, "o/alpha#1", schedule=SCHEDULE)
            beta_report = report.task_report(self.con, "o/beta#2", schedule=SCHEDULE)
            self.assertEqual(alpha_report["measured"]["attributed_responses"],
                             ["codex:r1"])
            self.assertEqual(beta_report["measured"]["attributed_responses"],
                             ["codex:r2"])
        # A privacy reimport still adopts the original file position once,
        # and the exact repeat later in the same file does not move it again.
        self.con.execute("UPDATE sources SET privacy_version=8")
        self.con.execute(
            "UPDATE responses SET ordinal_num=50 WHERE response_id='codex:r1'")
        import_codex_file(self.con, path, full=True)
        self.assertEqual(self.con.execute(
            "SELECT ordinal_num FROM responses WHERE response_id='codex:r1'"
        ).fetchone()[0], 3)
        placement.record_shared_checkouts(self.con)
        self.assertEqual(placement.read_placements(self.con, key), {
            "codex:r1": "o/alpha#1", "codex:r2": "o/beta#2"})

    def test_repeated_fallback_snapshot_keeps_its_first_position(self):
        path = os.path.join(self.tmp.name, "fallback.jsonl")
        buckets = {"input_tokens": 90, "cached_input_tokens": 0,
                   "cache_write_input_tokens": 0, "output_tokens": 10,
                   "reasoning_output_tokens": 0, "total_tokens": 100}
        snapshot = {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": buckets, "total_token_usage": buckets}}}
        rows = [
            {"type": "session_meta", "payload": {
                "id": "fallback-review", "thread_source": "user", "cwd": "/w"}},
            {"type": "turn_context", "payload": {"model": "model-a"}},
            snapshot,
            {"type": "turn_context", "payload": {"model": "model-b"}},
            snapshot,
        ]
        for ordinal, rec in enumerate(rows):
            rec["ordinal"] = ordinal
        with open(path, "w") as fh:
            for rec in rows:
                fh.write(json.dumps(rec) + "\n")
        for full in (False, True, False):
            import_codex_file(self.con, path, full=full)
            row = self.con.execute(
                "SELECT ordinal_num, model FROM responses WHERE response_id=?",
                ("codex:fallback-review:tc:100",)).fetchone()
            self.assertEqual(tuple(row), (2, "model-a"))

    def test_repeated_tool_call_keeps_the_first_call_on_its_task(self):
        # A brand-new source is privacy-stale. An exact later copy of the
        # same tool call must not take the original call's chronology, or
        # the call falls after every response and the task report loses it.
        alpha = self.checkouts["alpha"]
        beta = self.checkouts["beta"]
        path = os.path.join(self.tmp.name, "calls.jsonl")

        def call(name, root):
            return {"type": "response_item", "payload": {
                "type": "function_call", "call_id": name, "name": "exec_command",
                "arguments": json.dumps({"cmd": "cat " + os.path.join(root, "x.py")})}}

        def usage(name, total):
            return {"type": "token_usage_record", "payload": {
                "response_id": name, "thread_id": "placement-review",
                "usage": {"input_tokens": total - 2, "output_tokens": 2,
                          "total_tokens": total}}}

        rows = [
            {"type": "session_meta", "payload": {
                "id": "placement-review", "thread_source": "user", "cwd": "/w"}},
            {"type": "turn_context", "payload": {"model": "model-a"}},
            call("call-a", alpha),
            usage("r1", 12),
            {"type": "turn_context", "payload": {"model": "model-b"}},
            call("call-b", beta),
            usage("r2", 22),
            call("call-a", alpha),
        ]
        for ordinal, rec in enumerate(rows):
            rec["ordinal"] = ordinal
        rows[-1] = dict(rows[2])
        with open(path, "w") as fh:
            for rec in rows:
                fh.write(json.dumps(rec) + "\n")
        key = "codex:placement-review"
        for task in ("o/alpha#1", "o/beta#2"):
            self.con.execute(
                "INSERT INTO session_assignments(session_key, task_id,"
                " evidence, created_at) VALUES(?,?,'t3',0)", (key, task))
        for full in (False, True, False):
            import_codex_file(self.con, path, full=full)
            stored = dict(self.con.execute(
                "SELECT native_id, ordinal_num FROM events"))
            self.assertEqual(stored, {"call-a": 2, "call-b": 5})
            placement.record_shared_checkouts(self.con)
            self.assertEqual(placement.read_placements(self.con, key), {
                "codex:r1": "o/alpha#1", "codex:r2": "o/beta#2"})
            alpha_report = report.task_report(self.con, "o/alpha#1", schedule=SCHEDULE)
            beta_report = report.task_report(self.con, "o/beta#2", schedule=SCHEDULE)
            self.assertEqual(alpha_report["measured"]["attributed_responses"],
                             ["codex:r1"])
            self.assertEqual(beta_report["measured"]["attributed_responses"],
                             ["codex:r2"])
        event = self.con.execute(
            "SELECT id, source_id FROM events WHERE native_id='call-a'").fetchone()
        self.con.execute(
            "INSERT INTO verdicts(intervention_id, behavior, judge, verdict,"
            " recorded_at) VALUES(?, 'correction', 'judge', 'yes', 0)",
            (f"ev:{key}/tool_call/call-a",))
        self.con.execute("UPDATE sources SET privacy_version=8")
        self.con.commit()
        import_codex_file(self.con, path, full=True)
        again = self.con.execute(
            "SELECT id, source_id, ordinal_num FROM events WHERE native_id='call-a'"
        ).fetchone()
        self.assertEqual(tuple(again), (event["id"], event["source_id"], 2))
        self.assertEqual(self.con.execute(
            "SELECT intervention_id FROM verdicts").fetchone()[0],
            f"ev:{key}/tool_call/call-a")
        placement.record_shared_checkouts(self.con)
        self.assertEqual(placement.read_placements(self.con, key)["codex:r1"],
                         "o/alpha#1")

    def test_continuation_keeps_the_original_call_and_response(self):
        # The same native call in a second rollout of this session is not
        # a new occurrence. Its file position must not replace the call
        # that already owns the first response.
        alpha = self.checkouts["alpha"]
        beta = self.checkouts["beta"]
        path = os.path.join(self.tmp.name, "calls.jsonl")
        continuation = os.path.join(self.tmp.name, "calls-continuation.jsonl")

        def call(name, root):
            return {"type": "response_item", "payload": {
                "type": "function_call", "call_id": name, "name": "exec_command",
                "arguments": json.dumps({"cmd": "cat " + os.path.join(root, "x.py")})}}

        def usage(name, total):
            return {"type": "token_usage_record", "payload": {
                "response_id": name, "thread_id": "placement-review",
                "usage": {"input_tokens": total - 2, "output_tokens": 2,
                          "total_tokens": total}}}

        rows = [
            {"type": "session_meta", "payload": {
                "id": "placement-review", "thread_source": "user", "cwd": "/w"}},
            {"type": "turn_context", "payload": {"model": "model-a"}},
            call("call-a", alpha),
            usage("r1", 12),
            {"type": "turn_context", "payload": {"model": "model-b"}},
            call("call-b", beta),
            usage("r2", 22),
        ]
        for ordinal, rec in enumerate(rows):
            rec["ordinal"] = ordinal
        with open(path, "w") as fh:
            for rec in rows:
                fh.write(json.dumps(rec) + "\n")
        continued = [rows[0]] + [
            {"type": "world_state", "ordinal": 20 + i, "payload": {}}
            for i in range(8)] + [dict(rows[2])]
        with open(continuation, "w") as fh:
            for rec in continued:
                fh.write(json.dumps(rec) + "\n")
        key = "codex:placement-review"
        for task in ("o/alpha#1", "o/beta#2"):
            self.con.execute(
                "INSERT INTO session_assignments(session_key, task_id,"
                " evidence, created_at) VALUES(?,?,'t3',0)", (key, task))

        def owned(table, key_column, native):
            row = self.con.execute(
                f"SELECT t.ordinal_num, s.path FROM {table} t"
                " JOIN sources s ON s.id=t.source_id"
                f" WHERE t.{key_column}=?", (native,)).fetchone()
            return (row["ordinal_num"], os.path.basename(row["path"]))

        for full in (False, True, False):
            import_codex_file(self.con, path, full=full)
            import_codex_file(self.con, continuation, full=full)
            self.assertEqual(owned("events", "native_id", "call-a"),
                             (2, "calls.jsonl"))
            self.assertEqual(owned("events", "native_id", "call-b"),
                             (5, "calls.jsonl"))
            self.assertEqual(owned("responses", "response_id", "codex:r1"),
                             (3, "calls.jsonl"))
            self.assertEqual(owned("responses", "response_id", "codex:r2"),
                             (6, "calls.jsonl"))
            placement.record_shared_checkouts(self.con)
            self.assertEqual(placement.read_placements(self.con, key), {
                "codex:r1": "o/alpha#1", "codex:r2": "o/beta#2"})
            alpha_report = report.task_report(
                self.con, "o/alpha#1", schedule=SCHEDULE)
            beta_report = report.task_report(
                self.con, "o/beta#2", schedule=SCHEDULE)
            self.assertEqual(alpha_report["measured"]["attributed_responses"],
                             ["codex:r1"])
            self.assertEqual(beta_report["measured"]["attributed_responses"],
                             ["codex:r2"])
            self.assertEqual(self.con.execute(
                "SELECT COUNT(*) FROM import_errors").fetchone()[0], 0)

    def _bind_placement_tasks(self, con):
        key = "codex:placement-review"
        for task in ("o/alpha#1", "o/beta#2"):
            con.execute(
                "INSERT INTO session_assignments(session_key, task_id,"
                " evidence, created_at) VALUES(?,?,'t3',0)", (key, task))
        return key

    def _continuation_files(self, label):
        alpha = self.checkouts["alpha"]
        beta = self.checkouts["beta"]
        path = os.path.join(self.tmp.name, f"calls-{label}.jsonl")
        continuation = os.path.join(
            self.tmp.name, f"calls-{label}-continuation.jsonl")

        def call(name, root):
            return {"type": "response_item", "payload": {
                "type": "function_call", "call_id": name, "name": "exec_command",
                "arguments": json.dumps({"cmd": "cat " + os.path.join(root, "x.py")})}}

        def usage(name, total):
            return {"type": "token_usage_record", "payload": {
                "response_id": name, "thread_id": "placement-review",
                "usage": {"input_tokens": total - 2, "output_tokens": 2,
                          "total_tokens": total}}}

        rows = [
            {"type": "session_meta", "payload": {
                "id": "placement-review", "thread_source": "user", "cwd": "/w"}},
            {"type": "turn_context", "payload": {"model": "model-a"}},
            call("call-a", alpha),
            usage("r1", 12),
            {"type": "turn_context", "payload": {"model": "model-b"}},
            call("call-b", beta),
            usage("r2", 22),
        ]
        for ordinal, rec in enumerate(rows):
            rec["ordinal"] = ordinal
        with open(path, "w") as fh:
            for rec in rows:
                fh.write(json.dumps(rec) + "\n")
        continued = [rows[0]] + [
            {"type": "world_state", "ordinal": 20 + i, "payload": {}}
            for i in range(8)] + [dict(rows[2])]
        with open(continuation, "w") as fh:
            for rec in continued:
                fh.write(json.dumps(rec) + "\n")
        return path, continuation

    def _assert_public_placement(self, con):
        key = "codex:placement-review"
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM events WHERE native_id='call-a'").fetchone()[0], 1)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM responses WHERE is_overlap=0").fetchone()[0], 2)
        placement.record_shared_checkouts(con)
        self.assertEqual(placement.read_placements(con, key), {
            "codex:r1": "o/alpha#1", "codex:r2": "o/beta#2"})
        alpha_report = report.task_report(con, "o/alpha#1", schedule=SCHEDULE)
        beta_report = report.task_report(con, "o/beta#2", schedule=SCHEDULE)
        self.assertEqual(alpha_report["measured"]["attributed_responses"],
                         ["codex:r1"])
        self.assertEqual(beta_report["measured"]["attributed_responses"],
                         ["codex:r2"])
        for table, column, native in (
                ("events", "native_id", "call-a"),
                ("events", "native_id", "call-b"),
                ("responses", "response_id", "codex:r1"),
                ("responses", "response_id", "codex:r2")):
            row = con.execute(
                f"SELECT source_id, ordinal_num FROM {table} WHERE {column}=?",
                (native,)).fetchone()
            kind = "event" if table == "events" else "response"
            if kind == "event":
                key_row = con.execute(
                    "SELECT id FROM events WHERE native_id=?", (native,)).fetchone()
                row_key = str(key_row["id"])
            else:
                row_key = native
            seen = con.execute(
                "SELECT 1 FROM codex_occurrences WHERE source_id=? AND row_kind=?"
                " AND row_key=? AND record_position=?",
                (row["source_id"], kind, row_key, row["ordinal_num"])).fetchone()
            self.assertIsNotNone(seen)

    def test_reverse_continuation_still_places_the_original_response(self):
        # The duplicate-only file is imported first. Its call has no
        # response after it, so it cannot take the original response.
        path, continuation = self._continuation_files("reverse")
        key = self._bind_placement_tasks(self.con)
        for full in (False, True, False):
            import_codex_file(self.con, continuation, full=full)
            import_codex_file(self.con, path, full=full)
            self._assert_public_placement(self.con)
            calls = report.timeline(
                self.con, session_key=key, family="tool_call", limit=1)
            self.assertEqual(
                [event["native_id"] for event in calls["events"]], ["call-a"])
            self.assertEqual(self.con.execute(
                "SELECT COUNT(*) FROM import_errors").fetchone()[0], 0)
        self.assertTrue(key)

    def test_competing_response_witnesses_leave_the_call_unplaced(self):
        # Each file pairs the same call with a different response. Neither
        # import order picks one of those responses.
        alpha = self.checkouts["alpha"]

        def call():
            return {"type": "response_item", "payload": {
                "type": "function_call", "call_id": "call-a", "name": "exec_command",
                "arguments": json.dumps({"cmd": "cat " + os.path.join(alpha, "x.py")})}}

        def usage(name, total):
            return {"type": "token_usage_record", "payload": {
                "response_id": name, "thread_id": "placement-review",
                "usage": {"input_tokens": total - 2, "output_tokens": 2,
                          "total_tokens": total}}}

        def write(name, response, total):
            path = os.path.join(self.tmp.name, name)
            rows = [
                {"type": "session_meta", "payload": {
                    "id": "placement-review", "thread_source": "user", "cwd": "/w"}},
                {"type": "turn_context", "payload": {"model": "model-a"}},
                call(),
                usage(response, total),
            ]
            for ordinal, rec in enumerate(rows):
                rec["ordinal"] = ordinal
            with open(path, "w") as fh:
                for rec in rows:
                    fh.write(json.dumps(rec) + "\n")
            return path

        first = write("compete-a.jsonl", "r1", 12)
        second = write("compete-b.jsonl", "r2", 22)
        key = self._bind_placement_tasks(self.con)
        other = db.connect(os.path.join(self.tmp.name, "compete-reverse.db"))
        self.addCleanup(other.close)
        db.init_db(other)
        t3_adapter._ensure_tables(other)
        for n, (repo, branch) in enumerate((("alpha", "fix/a"), ("beta", "fix/b")), 1):
            task = f"o/{repo}#{n}"
            other.execute("INSERT INTO tasks(task_id, project, title, created_at)"
                          " VALUES(?,?,?,0)", (task, f"o/{repo}", task))
            other.execute(
                "INSERT INTO t3_links(thread_id, kind, host, repository, number, url,"
                " head_branch) VALUES('t','pr','github.com',?,?,?,?)",
                (f"o/{repo}", n, f"https://github.com/o/{repo}/pull/{n}", branch))
        self._bind_placement_tasks(other)
        for con, order in ((self.con, (first, second)), (other, (second, first))):
            import_codex_file(con, order[0])
            placement.record_shared_checkouts(con)
            self.assertEqual(list(placement.read_placements(con, key).values()), ["o/alpha#1"])
            import_codex_file(con, order[1])
            # A Codex-only import can invalidate a sync's witness before
            # the placement recorder next runs.
            ambiguous = {"codex:r1": None, "codex:r2": None}
            self.assertEqual(placement.read_placements(con, key), ambiguous)
            for full in (False, True, False):
                for path in order:
                    import_codex_file(con, path, full=full)
                placement.record_shared_checkouts(con)
                self.assertEqual(placement.read_placements(con, key), ambiguous)

    def test_checkout_is_resolved_once_for_new_events_in_one_import(self):
        root = self.checkouts["alpha"]
        path = os.path.join(self.tmp.name, "same-root.jsonl")
        for n in (1, 2):
            self._append_claude_call(path, "same-root", f"r{n}", root)
        with patch.object(placement, "_git", wraps=placement._git) as git:
            import_claude_file(self.con, path)
        self.assertEqual([call.args for call in git.call_args_list], [
            (root, "remote", "get-url", "origin"),
            (root, "rev-parse", "--abbrev-ref", "HEAD")])

    def test_live_git_runs_outside_an_open_write(self):
        key = "claude:transaction"
        path = os.path.join(self.tmp.name, "transaction.jsonl")
        self._append_claude_call(path, "transaction", "r0", self.checkouts["alpha"])
        for task in ("o/alpha#1", "o/beta#2"):
            self._assign(key, task)
        self.assertTrue(self.con.in_transaction)
        real = placement._git

        def check_transaction(root, *args):
            self.assertFalse(self.con.in_transaction)
            return real(root, *args)

        with patch.object(placement, "_git", side_effect=check_transaction) as git:
            import_claude_file(self.con, path)
        self.assertTrue(git.called)
        other = db.connect_read_only(self.db_path)
        self.addCleanup(other.close)
        self.assertEqual(placement.read_placements(other, key)["claude:r0"], "o/alpha#1")

    def test_sync_records_shared_sessions_once_after_refresh(self):
        calls = []

        def record(con):
            calls.append(1)
            return 4

        class Harness:
            def __init__(self, name, skipped=None):
                self.name = name
                self.skipped = skipped

            def sync(self, con, root=None, full=False, source=None):
                row = {"harness": self.name, "sources": 1, "shared_sessions": 9}
                if self.skipped:
                    row["skipped"] = self.skipped
                return row

        real_available = cli._adapters.available
        real_record = placement.record_shared_checkouts
        cli._adapters.available = lambda: {
            "t3": Harness("t3"), "codex": Harness("codex")}
        placement.record_shared_checkouts = record
        ns = type("NS", (), {})()
        ns.harness = ["t3", "codex"]
        ns.source = None
        ns.root = self.tmp.name
        ns.full = False
        ns.agentsmd_repo = os.path.join(self.tmp.name, "no-such-repo")
        ns.as_json = True
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli._sync(self.con, ns)
        finally:
            cli._adapters.available = real_available
            placement.record_shared_checkouts = real_record
        self.assertEqual(code, 0)
        self.assertEqual(calls, [1])
        payload = json.loads(buf.getvalue())
        t3 = next(row for row in payload["harnesses"] if row["harness"] == "t3")
        self.assertEqual(t3["shared_sessions"], 4)

        calls.clear()
        cli._adapters.available = lambda: {"t3": Harness("t3", "no T3 state")}
        placement.record_shared_checkouts = record
        ns.harness = ["t3"]
        try:
            with redirect_stdout(io.StringIO()):
                cli._sync(self.con, ns)
        finally:
            cli._adapters.available = real_available
            placement.record_shared_checkouts = real_record
        self.assertEqual(calls, [])

        cli._adapters.available = lambda: {"codex": Harness("codex")}
        placement.record_shared_checkouts = record
        ns.harness = ["codex"]
        try:
            with redirect_stdout(io.StringIO()):
                cli._sync(self.con, ns)
        finally:
            cli._adapters.available = real_available
            placement.record_shared_checkouts = real_record
        self.assertEqual(calls, [])
