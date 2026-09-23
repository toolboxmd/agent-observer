"""Detectors and comparisons: each candidate with its counterexamples."""

import json

from agent_observer import analysis, db
from tests.helpers import LedgerCase


class AnalysisCase(LedgerCase):
    def session(self, key, version="12.1.0", project="/p/app", started=1000.0):
        db.upsert_session(self.con, key, key.split(":")[0], key.split(":", 1)[1],
                          None, project_dir=project, agentsmd_version=version,
                          started_at=started, ended_at=started + 60)
        self.seq = getattr(self, "seq", 0)

    def event(self, key, family, name=None, target=None, status=None, ts=None,
              detail=None):
        self.seq += 1
        self.con.execute(
            "INSERT INTO events(session_key, ts, family, native_id, name, target,"
            " status, detail_json) VALUES(?,?,?,?,?,?,?,?)",
            (key, ts if ts is not None else float(self.seq), family, f"e{self.seq}",
             name, target, status, json.dumps(detail) if detail else None))

    def prompt(self, key, text, kind="genuine"):
        self.seq += 1
        self.con.execute(
            "INSERT INTO submissions(native_id, session_key, ts, kind, text_hash,"
            " text_excerpt, is_genuine) VALUES(?,?,?,?,?,?,?)",
            (f"s{self.seq}", key, float(self.seq), kind, "h", text,
             1 if kind == "genuine" else 0))

    def found(self, key, detector):
        return [i for i in analysis.diagnose(self.con, session=key)["incidents"]
                if i["detector"] == detector]


class RepeatedReadTest(AnalysisCase):
    def test_same_range_twice_is_a_candidate(self):
        self.session("claude:a")
        for _ in range(2):
            self.event("claude:a", "read", target="/p/x.py", detail={"start_line": 1, "num_lines": 50})
        self.assertEqual(len(self.found("claude:a", "repeated_read")), 1)

    def test_edit_compaction_or_other_range_explain_the_reread(self):
        self.session("claude:b")
        self.event("claude:b", "read", target="/p/x.py", detail={"start_line": 1, "num_lines": 50})
        self.event("claude:b", "file_change", target="/p/x.py")
        self.event("claude:b", "read", target="/p/x.py", detail={"start_line": 1, "num_lines": 50})
        self.event("claude:b", "compaction", name="compact_boundary")
        self.event("claude:b", "read", target="/p/x.py", detail={"start_line": 1, "num_lines": 50})
        self.event("claude:b", "read", target="/p/x.py", detail={"start_line": 51, "num_lines": 50})
        self.assertEqual(self.found("claude:b", "repeated_read"), [])


class TestEditAfterFailureTest(AnalysisCase):
    def run_tests(self, key, ok):
        self.event(key, "tool_result", name="Bash", target="python3 -m unittest discover -s tests",
                   status="ok" if ok else "error")

    def test_only_tests_edited_between_failure_and_pass(self):
        self.session("claude:c")
        self.run_tests("claude:c", ok=False)
        self.event("claude:c", "file_change", target="/p/tests/test_policy.py")
        self.run_tests("claude:c", ok=True)
        found = self.found("claude:c", "test_edit_after_failure")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["only_tests_changed"])
        self.assertEqual(found[0]["test_files"], ["/p/tests/test_policy.py"])

    def test_code_and_test_edits_are_reported_with_both(self):
        self.session("claude:d")
        self.run_tests("claude:d", ok=False)
        self.event("claude:d", "file_change", target="/p/src/policy.py")
        self.event("claude:d", "file_change", target="/p/tests/test_policy.py")
        self.run_tests("claude:d", ok=True)
        found = self.found("claude:d", "test_edit_after_failure")
        self.assertFalse(found[0]["only_tests_changed"])

    def test_code_fix_alone_or_no_failure_is_not_a_candidate(self):
        self.session("claude:e")
        self.run_tests("claude:e", ok=False)
        self.event("claude:e", "file_change", target="/p/src/policy.py")
        self.run_tests("claude:e", ok=True)
        self.event("claude:e", "file_change", target="/p/tests/test_policy.py")
        self.run_tests("claude:e", ok=True)
        self.assertEqual(self.found("claude:e", "test_edit_after_failure"), [])


class HumanSignalsTest(AnalysisCase):
    def test_permission_questions_and_corrections(self):
        self.session("claude:f")
        self.event("claude:f", "assistant_message",
                   detail={"excerpt": "Done. Should I also update the changelog?"})
        self.event("claude:f", "assistant_message", detail={"excerpt": "Done. Tests pass."})
        self.event("claude:f", "assistant_message", detail={"excerpt": "Which file do you mean?"})
        self.prompt("claude:f", "no, that's wrong, revert it")
        self.prompt("claude:f", "please continue with the next step")
        self.prompt("claude:f", "[Request interrupted by user]", kind="interrupt")
        self.event("claude:f", "permission", name="Bash", status="denied")
        asks = self.found("claude:f", "permission_seeking")
        self.assertEqual(len(asks), 1)
        self.assertTrue(asks[0]["heuristic"])
        signals = sorted(i["signal"] for i in self.found("claude:f", "human_correction"))
        self.assertEqual(signals, ["correction wording", "denial", "interrupt"])


class CompareTest(AnalysisCase):
    def test_groups_by_version_with_sample_sizes_and_skip_subagents(self):
        self.session("claude:g", version="12.0.1", project="/p/one")
        self.session("claude:h", version="12.1.0", project="/p/two")
        self.session("claude:i", version="12.1.0", project="/p/three")
        db.upsert_session(self.con, "claude:h:agent:x", "claude", "h:agent:x", None,
                          role="subagent", agentsmd_version="12.1.0")
        self.prompt("claude:h", "do the thing")
        result = analysis.compare(self.con, by="agentsmd")
        groups = {g["group"]: g for g in result["groups"]}
        self.assertEqual([g["group"] for g in result["groups"]], ["12.0.1", "12.1.0"])
        self.assertEqual(groups["12.1.0"]["sessions"], 2)
        self.assertEqual(groups["12.1.0"]["projects"], 2)
        self.assertEqual(groups["12.1.0"]["genuine_prompts_per_session"]["n"], 1)
        self.assertIn("not causal", result["note"])
