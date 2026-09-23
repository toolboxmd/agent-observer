"""Parent/worker scope: disjoint responses, dispatch join, no double counting."""

from agent_observer import db, report
from tests.helpers import LedgerCase


class ScopeTest(LedgerCase):
    def test_parent_child_responses_are_disjoint(self):
        self.sync("codex-parent.jsonl")
        self.sync("codex-child.jsonl")
        rows = self.query(
            "SELECT response_id, COUNT(*) n FROM responses "
            "GROUP BY 1 HAVING n > 1")
        self.assertEqual(list(rows), [])
        parent = self.query(
            "SELECT SUM(total_tokens) t FROM responses "
            "WHERE response_id LIKE 'codex:resp-scope-p-%'")[0]["t"]
        child = self.query(
            "SELECT SUM(total_tokens) t FROM responses "
            "WHERE response_id LIKE 'codex:resp-scope-c-%'")[0]["t"]
        self.assertEqual(parent, 3300)
        self.assertEqual(child, 1650)
        self.assertEqual(report.scope_totals(self.con)["total_tokens"], 4950)

    def test_dispatch_links_parent_submission_to_worker(self):
        self.sync("codex-parent.jsonl")
        self.sync("codex-child.jsonl")
        life = self.query(
            "SELECT native_id, name FROM events WHERE family='lifecycle'"
            " ORDER BY ordinal_num")
        names = [r["name"] for r in life]
        self.assertIn("subagent_activity", names)
        calls = self.query(
            "SELECT native_id, name FROM events WHERE family='tool_call'")
        # The spawn edge is joinable on its native call id; the native tool
        # name is an instance value, not a canonical kind, so rule 6 keeps
        # no name.
        spawned = [r for r in calls
                   if r["native_id"] == "call-scope-spawn-01"]
        self.assertEqual(len(spawned), 1)
        self.assertIsNone(spawned[0]["name"])
        # Explicit dispatch edge connects ownership; the worker turn holds
        # only the child responses, never a copy of the parent total.
        self.con.execute(
            "INSERT INTO tasks(task_id, project, family, title, created_at)"
            " VALUES(?,?,?,?,?)", ("T-P", "observer", "research", "p", db.now()))
        child_session = self.query(
            "SELECT session_key FROM responses WHERE turn_id=?",
            ("codex:turn-scope-child",))[0]["session_key"]
        self.con.execute(
            "INSERT INTO submissions(native_id, source_id, session_key,"
            " turn_id, ordinal_num, text_hash, text_excerpt, is_genuine)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("worker-sub-scope-c1", None, child_session,
             "codex:turn-scope-child", 3, "hash", "worker submission", 1))
        self.con.execute(
            "INSERT INTO assignments(submission_native_id, task_id,"
            " created_at) VALUES(?,?,?)",
            ("codex:msg-scope-sub-p1", "T-P", db.now()))
        self.con.execute(
            "INSERT INTO assignments(submission_native_id, task_id,"
            " created_at) VALUES(?,?,?)",
            ("worker-sub-scope-c1", "T-P", db.now()))
        self.con.execute(
            "INSERT INTO dispatches(owning_submission, worker_thread,"
            " worker_turn, requested_model, created_at)"
            " VALUES(?,?,?,?,?)",
            ("codex:msg-scope-sub-p1", "thread-fixture-scope-child",
             "turn-scope-child", "gpt-6-fixture", db.now()))
        self.con.commit()
        rep = report.task_report(self.con, "T-P")
        self.assertEqual(rep["attributed"]["total_tokens"], 4950)
        self.assertEqual(rep["attributed"]["responses"], 4)
        self.assertEqual(len(rep["dispatches"]), 1)
