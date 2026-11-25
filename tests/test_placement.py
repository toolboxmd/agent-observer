"""Legacy T3 checkout gaps and native Codex occurrence retention."""

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

    def _assign(self, key, task, evidence="t3"):
        self.con.execute(
            "INSERT INTO session_assignments(session_key,task_id,evidence,created_at)"
            " VALUES(?,?,?,0)", (key, task, evidence))

    def _assert_unplaced(self, con, responses):
        alpha = report.task_report(con, "o/alpha#1", schedule=SCHEDULE)
        beta = report.task_report(con, "o/beta#2", schedule=SCHEDULE)
        for task in (alpha, beta):
            self.assertEqual(task["measured"]["unplaced_responses"], responses)
            self.assertEqual(task["measured"]["attributed_responses"], [])
            self.assertEqual(task["total_cost"]["responses"], 0)
            self.assertTrue(task["reconciles"])

    def test_first_import_after_path_reuse_leaves_claude_call_unplaced(self):
        key = "claude:delayed"
        path = os.path.join(self.tmp.name, "delayed.jsonl")
        root = self.checkouts["alpha"]
        native = [
            {"type": "assistant", "sessionId": "delayed", "cwd": root, "gitBranch": "fix/a",
             "message": {"id": "before", "model": "m", "usage": {"input_tokens": 10,
                                                                "output_tokens": 2},
                         "content": [{"type": "tool_use", "id": "call-before", "name": "Bash",
                                      "input": {"command": f"cd {root} && gh pr edit 1"
                                                           " --repo o/alpha --title Revised"}}]}},
            {"type": "user", "sessionId": "delayed", "uuid": "result",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "call-before",
                                      "content": "https://github.com/o/alpha/pull/1"}]},
             "toolUseResult": {"gitOperation": {"pr": {"action": "edited", "number": 1,
                                                        "url": "https://github.com/o/alpha/pull/1"}}}},
            {"type": "pr-link", "sessionId": "delayed", "prNumber": 1,
             "prRepository": "o/alpha", "prUrl": "https://github.com/o/alpha/pull/1"},
        ]
        with open(path, "w") as stream:
            for row in native:
                stream.write(json.dumps(row) + "\n")

        _git("-C", root, "checkout", "-q", "-b", "fix/b")
        _git("-C", root, "remote", "set-url", "origin", "git@github.com:o/beta.git")
        self._assign(key, "o/alpha#1")
        self.assertEqual(import_claude_file(self.con, path)["malformed"], 0)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM events WHERE family='tool_call'").fetchone()[0], 1)
        observed = {"claude:before": [{"host": "github.com", "repository": "o/beta",
                                      "branch": "fix/b", "candidate_tasks": []}]}
        self.assertEqual(placement.checkout_observations(self.con, key), observed)
        self._assign(key, "o/beta#2")
        self.assertEqual(placement.record_shared_checkouts(self.con), 1)
        _git("-C", root, "checkout", "-q", "fix/a")
        _git("-C", root, "remote", "set-url", "origin", "git@github.com:o/alpha.git")
        # Replaying after reuse never changes what was first observed.
        with patch.object(subprocess, "run", side_effect=AssertionError("replay ran Git")):
            self.assertEqual(import_claude_file(self.con, path, full=True)["malformed"], 0)
            self._assert_unplaced(self.con, ["claude:before"])
        observed["claude:before"][0]["candidate_tasks"] = ["o/beta#2"]
        shutil.rmtree(root)
        with closing(db.connect_read_only(self.db_path)) as ro, patch.object(
                subprocess, "run", side_effect=AssertionError("report ran Git")):
            self.assertEqual(placement.checkout_observations(ro, key), observed)
            self._assert_unplaced(ro, ["claude:before"])

    def test_codex_session_git_and_literal_pr_target_leave_call_unplaced(self):
        key = "codex:target"
        root = self.checkouts["alpha"]
        path = os.path.join(self.tmp.name, "target.jsonl")
        command = "gh pr view 1 --repo github.com/o/alpha"
        rows = [
            {"type": "session_meta", "payload": {
                "id": "target", "cwd": root, "thread_source": "user", "git": {
                    "repository_url": "https://github.com/o/alpha.git",
                    "branch": "fix/a", "commit_hash": "a" * 40}}},
            {"type": "turn_context", "payload": {"cwd": root, "model": "m"}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "CommandExecution", "id": "call-target", "cwd": root,
                "command": ["/bin/bash", "-lc", command], "exit_code": 0,
                "status": "completed", "parsed_cmd": [],
            }}},
            {"type": "token_usage_record", "payload": {
                "response_id": "target-r", "thread_id": "target",
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}},
        ]
        with open(path, "w") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        self._assign(key, "o/alpha#1")
        self._assign(key, "o/beta#2")
        with patch.object(subprocess, "run", side_effect=AssertionError("import ran Git")):
            self.assertEqual(import_codex_file(self.con, path)["events_inserted"], 1)
            self._assert_unplaced(self.con, ["codex:target-r"])

    def test_old_and_hostless_observations_stay_out_of_diagnostics_and_totals(self):
        key = "claude:s"
        db.upsert_session(self.con, key, "claude", "s", self.source)
        for task in ("o/alpha#1", "o/beta#2"):
            self._assign(key, task)
        self.con.execute(
            "INSERT INTO responses(response_id,source_id,harness,session_key,ordinal_num,"
            " model,input_tokens,output_tokens,total_tokens) VALUES('r0',?,'claude',?,1,'m',10,2,12)",
            (self.source, key))
        root = self.checkouts["alpha"]
        target = root + "/file"
        event = self.con.execute(
            "INSERT INTO events(source_id,session_key,ordinal_num,family,native_id,name,target)"
            " VALUES(?,?,2,'tool_call','call-a','Bash',?)", (self.source, key, "cat " + target))
        digest = hashlib.sha256(target.encode()).digest()
        self.con.execute("INSERT INTO checkouts(root,repository,branch,seen_at)"
                         " VALUES(?,'o/alpha','fix/a',0)", (root,))
        self.con.execute("CREATE TABLE response_checkouts(event_id,path_digest,response_id,"
                         " host,repository,branch)")
        self.con.execute("INSERT INTO response_checkouts VALUES(?,?,'r0','github.com','o/alpha','fix/a')",
                         (event.lastrowid, digest))
        self.con.execute("CREATE TABLE event_checkouts(event_id,path_digest,host,repository,branch)")
        self.con.execute("INSERT INTO event_checkouts VALUES(?,?,NULL,'o/alpha','fix/a')",
                         (event.lastrowid, digest))
        self.con.commit()
        shutil.rmtree(root)
        with closing(db.connect_read_only(self.db_path)) as ro, patch.object(
                subprocess, "run", side_effect=AssertionError("report ran Git")):
            statements = []
            ro.set_trace_callback(statements.append)
            self.assertEqual(placement.checkout_observations(ro, key), {})
            self._assert_unplaced(ro, ["r0"])
            self.assertTrue(ro.in_transaction)
            self.assertEqual({sql.split()[0].upper() for sql in statements}
                             - {"SELECT", "WITH", "PRAGMA"}, set())

    def test_non_legacy_assignments_and_unsupported_harness_keep_caller_behavior(self):
        for harness, evidence in (("claude", "router:job"), ("opencode", "t3")):
            key = f"{harness}:s"
            db.upsert_session(self.con, key, harness, "s", self.source)
            self._assign(key, "o/alpha#1", evidence)
            self._assign(key, "o/beta#2")
            self.assertIsNone(placement.read_placements(self.con, key))

    def test_shared_links_without_pr_heads_stay_context(self):
        key = "claude:no-head"
        db.upsert_session(self.con, key, "claude", "no-head", self.source)
        for task in ("o/alpha#1", "o/beta#2"):
            self._assign(key, task)
        self.con.execute("UPDATE t3_links SET head_branch=NULL")
        self.assertIsNone(placement.read_placements(self.con, key))

    def test_diagnostics_keep_same_head_candidates_separate_by_forge(self):
        key = "codex:placement-review"
        hosted_root = self.checkouts["beta"]
        _git("-C", hosted_root, "checkout", "-q", "-b", "fix/a")
        _git("-C", hosted_root, "remote", "set-url", "origin",
             "https://forge.example/o/alpha.git")
        path, _ = self._continuation_files("forge")
        import_codex_file(self.con, path)
        # PR links can arrive after the native import. Same-head PRs on one
        # forge remain visible as multiple candidates, without task credit.
        self._assign(key, "o/alpha#1")
        for host, number, task in (("github.com", 3, "o/alpha#3"),
                                   ("forge.example", 1, "forge.example/o/alpha#1")):
            self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES(?,0)", (task,))
            self._assign(key, task)
            self.con.execute(
                "INSERT INTO t3_links(thread_id,kind,host,repository,number,url,head_branch)"
                " VALUES('t','pr',?,'o/alpha',?,?,'fix/a')",
                (host, number, f"https://{host}/o/alpha/pull/{number}"))
        self.assertEqual(placement.checkout_observations(self.con, key), {
            "codex:r1": [{"host": "github.com", "repository": "o/alpha",
                          "branch": "fix/a", "candidate_tasks": ["o/alpha#1", "o/alpha#3"]}],
            "codex:r2": [{"host": "forge.example", "repository": "o/alpha",
                          "branch": "fix/a", "candidate_tasks": ["forge.example/o/alpha#1"]}],
        })
        for task in ("o/alpha#1", "o/alpha#3", "forge.example/o/alpha#1"):
            result = report.task_report(self.con, task, schedule=SCHEDULE)
            self.assertEqual(result["measured"]["unplaced_responses"], ["codex:r1", "codex:r2"])
            self.assertEqual(result["total_cost"]["responses"], 0)

    def test_repeated_usage_keeps_the_first_response_position(self):
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
        # A privacy reimport still adopts the original file position once,
        # and the exact repeat later in the same file does not move it again.
        self.con.execute("UPDATE sources SET privacy_version=8")
        self.con.execute(
            "UPDATE responses SET ordinal_num=50 WHERE response_id='codex:r1'")
        import_codex_file(self.con, path, full=True)
        self.assertEqual(self.con.execute(
            "SELECT ordinal_num FROM responses WHERE response_id='codex:r1'"
        ).fetchone()[0], 3)

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

    def test_repeated_tool_call_keeps_the_first_call_position(self):
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

    def test_continuation_keeps_the_original_call_and_response(self):
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

    def _assert_occurrences(self, con):
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM events WHERE native_id='call-a'").fetchone()[0], 1)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM responses WHERE is_overlap=0").fetchone()[0], 2)
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

    def test_reverse_continuation_preserves_native_occurrences(self):
        # The duplicate-only file is imported first. Its call has no
        # response after it, so it cannot take the original response.
        path, continuation = self._continuation_files("reverse")
        key = self._bind_placement_tasks(self.con)
        for full in (False, True, False):
            import_codex_file(self.con, continuation, full=full)
            import_codex_file(self.con, path, full=full)
            self._assert_occurrences(self.con)
            calls = report.timeline(
                self.con, session_key=key, family="tool_call", limit=1)
            self.assertEqual(
                [event["native_id"] for event in calls["events"]], ["call-a"])
            self.assertEqual(self.con.execute(
                "SELECT COUNT(*) FROM import_errors").fetchone()[0], 0)

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
