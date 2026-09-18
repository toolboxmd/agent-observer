"""Workload capture: explicit bindings, missing/conflicting/joint ownership."""

from agent_observer import db, report
from tests.helpers import LedgerCase


def _setup_tasks(case, *task_ids):
    for t in task_ids:
        case.con.execute(
            "INSERT INTO tasks(task_id, project, family, title, created_at)"
            " VALUES(?,?,?,?,?)", (t, "observer", "research", t, db.now()))
    case.con.commit()


def _assign(case, sub, task, shared=0):
    case.con.execute(
        "INSERT INTO assignments(submission_native_id, task_id, attempt,"
        " phase, evidence, shared, created_at) VALUES(?,?,?,?,?,?,?)",
        (sub, task, "a1", "research", "test binding", shared, db.now()))
    case.con.commit()


class CaptureTest(LedgerCase):
    def test_multiple_tasks_in_one_conversation(self):
        self.sync("codex-mini.jsonl")
        _setup_tasks(self, "T-A", "T-B")
        _assign(self, "msg-mini-sub-01", "T-A")
        _assign(self, "msg-mini-sub-02", "T-B")
        rep_a = report.task_report(self.con, "T-A")
        rep_b = report.task_report(self.con, "T-B")
        # Turn aaa holds two responses; turn bbb holds one.
        self.assertEqual(rep_a["attributed"]["responses"], 2)
        self.assertEqual(rep_a["attributed"]["total_tokens"], 3350)
        self.assertEqual(rep_b["attributed"]["responses"], 1)
        self.assertEqual(rep_b["attributed"]["total_tokens"], 2150)
        self.assertTrue(rep_a["reconciles"])
        self.assertEqual(rep_a["missing_assignments"], [])
        self.assertEqual(rep_b["missing_assignments"], [])

    def test_missing_assignment_never_inherits_prior_task(self):
        self.sync("codex-mini.jsonl")
        _setup_tasks(self, "T-A")
        _assign(self, "msg-mini-sub-01", "T-A")
        rep = report.task_report(self.con, "T-A")
        # The second submission has no binding: its turn stays unassigned
        # even though an earlier marker exists.
        self.assertEqual(rep["missing_assignments"], ["msg-mini-sub-02"])
        self.assertEqual(rep["unassigned_in_scope"]["responses"], 1)
        self.assertEqual(rep["unassigned_in_scope"]["total_tokens"], 2150)
        self.assertFalse(rep["complete"])

    def test_conflicting_assignment_stays_visible(self):
        self.sync("codex-mini.jsonl")
        _setup_tasks(self, "T-A", "T-B")
        _assign(self, "msg-mini-sub-01", "T-A", shared=0)
        _assign(self, "msg-mini-sub-01", "T-B", shared=0)
        rep = report.task_report(self.con, "T-A")
        self.assertEqual(len(rep["conflicting_assignments"]), 1)
        self.assertEqual(rep["conflicting_assignments"][0]["submission"],
                         "msg-mini-sub-01")
        self.assertFalse(rep["conflicting_assignments"][0]["all_shared"])
        self.assertFalse(rep["complete"])

    def test_joint_shared_response_is_not_divided(self):
        self.sync("codex-mini.jsonl")
        _setup_tasks(self, "T-A", "T-B")
        _assign(self, "msg-mini-sub-01", "T-A", shared=1)
        _assign(self, "msg-mini-sub-01", "T-B", shared=1)
        rep = report.task_report(self.con, "T-A")
        self.assertEqual(rep["shared_joint"]["responses"], 2)
        self.assertEqual(rep["shared_joint"]["total_tokens"], 3350)
        self.assertEqual(rep["attributed"]["responses"], 0)
        self.assertEqual(len(rep["joint_assignments"]), 1)
        self.assertTrue(rep["joint_assignments"][0]["all_shared"])

    def test_outcome_states_and_crash_separation(self):
        self.sync("codex-mini.jsonl")
        _setup_tasks(self, "T-A")
        _assign(self, "msg-mini-sub-01", "T-A")
        self.con.execute(
            "INSERT INTO attempts(task_id, turn_id, role, harness, state,"
            " usable_output) VALUES(?,?,?,?,?,?)",
            ("T-A", "turn-mini-aaa", "parent", "codex", "crashed", 0))
        self.con.execute(
            "INSERT INTO outcomes(task_id, candidate, proof_ref,"
            " acceptance_state, updated_at) VALUES(?,?,?,?,?)",
            ("T-A", "candidate-1", "proof-1", "unknown", db.now()))
        self.con.commit()
        rep = report.task_report(self.con, "T-A")
        self.assertEqual(rep["crashes_counted_separately"], 1)
        self.assertEqual(rep["outcome"]["acceptance_state"], "unknown")
