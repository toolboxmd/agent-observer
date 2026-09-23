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


class RepeatedSkillTest(AnalysisCase):
    def load(self, key, skill="wayfinder", target="skills/wayfinder/SKILL.md",
             family="skill_read"):
        detail = {"skill": skill} if family == "skill_read" else None
        self.event(key, family, name=skill, target=target if family == "skill_read" else skill,
                   detail=detail)

    def test_second_load_without_change_is_a_candidate(self):
        self.session("claude:sk1")
        self.load("claude:sk1")
        self.load("claude:sk1")
        self.assertEqual(len(self.found("claude:sk1", "repeated_skill_load")), 1)

    def test_edit_of_the_skill_file_resets_seen_state(self):
        self.session("claude:sk2")
        self.load("claude:sk2")
        self.event("claude:sk2", "file_change", target="skills/wayfinder/SKILL.md")
        self.load("claude:sk2")
        self.assertEqual(self.found("claude:sk2", "repeated_skill_load"), [])

    def test_edit_in_multi_path_detail_resets_seen_state(self):
        self.session("codex:sk3")
        self.load("codex:sk3")
        self.event("codex:sk3", "file_change", target="/p/other.py",
                   detail={"paths": {"/p/other.py": {"type": "edit"},
                                      "skills/wayfinder/SKILL.md": {"type": "edit"}}})
        self.load("codex:sk3")
        self.assertEqual(self.found("codex:sk3", "repeated_skill_load"), [])

    def test_unrelated_edit_does_not_reset(self):
        self.session("claude:sk4")
        self.load("claude:sk4")
        self.event("claude:sk4", "file_change", target="/p/unrelated.py")
        self.load("claude:sk4")
        self.assertEqual(len(self.found("claude:sk4", "repeated_skill_load")), 1)

    def test_compaction_still_breaks_the_run(self):
        self.session("claude:sk5")
        self.load("claude:sk5")
        self.event("claude:sk5", "compaction", name="compact_boundary")
        self.load("claude:sk5")
        self.assertEqual(self.found("claude:sk5", "repeated_skill_load"), [])

    def test_invoke_after_skill_dir_edit_is_not_flagged(self):
        self.session("claude:sk6")
        self.event("claude:sk6", "skill_invoke", name="wayfinder", target="wayfinder")
        self.event("claude:sk6", "file_change",
                   target="/opt/skills/wayfinder/SKILL.md")
        self.event("claude:sk6", "skill_invoke", name="wayfinder", target="wayfinder")
        self.assertEqual(self.found("claude:sk6", "repeated_skill_load"), [])


class MultiPathChangeTest(AnalysisCase):
    def test_reread_after_multi_path_edit_is_explained(self):
        self.session("codex:mp1")
        self.event("codex:mp1", "read", target="/p/a.py")
        self.event("codex:mp1", "file_change", target="/p/b.py",
                   detail={"paths": {"/p/b.py": {"type": "edit"},
                                      "/p/a.py": {"type": "edit"}}})
        self.event("codex:mp1", "read", target="/p/a.py")
        self.assertEqual(self.found("codex:mp1", "repeated_read"), [])

    def test_reread_after_edit_elsewhere_is_still_a_candidate(self):
        self.session("codex:mp2")
        self.event("codex:mp2", "read", target="/p/a.py")
        self.event("codex:mp2", "file_change", target="/p/b.py",
                   detail={"paths": {"/p/b.py": {"type": "edit"},
                                      "/p/c.py": {"type": "edit"}}})
        self.event("codex:mp2", "read", target="/p/a.py")
        self.assertEqual(len(self.found("codex:mp2", "repeated_read")), 1)

    def test_test_edit_after_failure_sees_every_changed_path(self):
        self.session("codex:mp3")
        self.event("codex:mp3", "tool_result", name="exec",
                   target="python3 -m unittest discover -s tests", status="error")
        self.event("codex:mp3", "file_change", target="/p/src/policy.py",
                   detail={"paths": {"/p/src/policy.py": {"type": "edit"},
                                      "/p/tests/test_policy.py": {"type": "edit"}}})
        self.event("codex:mp3", "tool_result", name="exec",
                   target="python3 -m unittest discover -s tests", status="ok")
        found = self.found("codex:mp3", "test_edit_after_failure")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["test_files"], ["/p/tests/test_policy.py"])
        self.assertEqual(found[0]["code_files"], ["/p/src/policy.py"])
        self.assertFalse(found[0]["only_tests_changed"])


class CompareByModelTest(AnalysisCase):
    def setUp(self):
        super().setUp()
        self.session("codex:m1", project="/p/app")
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES('codex','p','x',0)")
        # One session, two models: model-a owns turn t1, model-b owns t2.
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness, session_key,"
            " turn_id, model, total_tokens) VALUES"
            " ('codex:ra1', 1, 'codex', 'codex:m1', 't1', 'model-a', 100),"
            " ('codex:ra2', 1, 'codex', 'codex:m1', 't1', 'model-a', 200),"
            " ('codex:ra3', 1, 'codex', 'codex:m1', 't1', 'model-a', NULL),"
            " ('codex:rb1', 1, 'codex', 'codex:m1', 't2', 'model-b', 300)")
        # A reread on turn t1 and a turn-less reread elsewhere.
        self.con.execute(
            "INSERT INTO events(session_key, ts, family, native_id, name, target,"
            " turn_id, detail_json) VALUES"
            " ('codex:m1', 1, 'read', 'r1', 'a.py', '/p/a.py', 't1', NULL),"
            " ('codex:m1', 2, 'read', 'r2', 'a.py', '/p/a.py', 't1', NULL),"
            " ('codex:m1', 3, 'read', 'r3', 'b.py', '/p/b.py', NULL, NULL),"
            " ('codex:m1', 4, 'read', 'r4', 'b.py', '/p/b.py', NULL, NULL)")
        self.con.commit()

    def test_tokens_attribute_per_response_and_session_counts_twice(self):
        groups = {g["group"]: g
                  for g in analysis.compare(self.con, by="model")["groups"]}
        self.assertEqual(sorted(groups), ["mixed", "model-a", "model-b"])
        # The session used both models, so it counts under each.
        self.assertEqual(groups["model-a"]["sessions"], 1)
        self.assertEqual(groups["model-b"]["sessions"], 1)
        # Tokens follow the response's own model, with the NULL total kept
        # visible instead of zeroed.
        self.assertEqual(groups["model-a"]["tokens_per_session"]["total"], 300)
        self.assertEqual(groups["model-a"]["unknown_token_responses"], 1)
        self.assertEqual(groups["model-b"]["tokens_per_session"]["total"], 300)
        self.assertEqual(groups["model-b"]["unknown_token_responses"], 0)

    def test_incidents_follow_the_turn_model_or_mixed(self):
        groups = {g["group"]: g
                  for g in analysis.compare(self.con, by="model")["groups"]}
        self.assertEqual(groups["model-a"]["repeated_read"]["incidents"], 1)
        self.assertEqual(groups["model-b"]["repeated_read"]["incidents"], 0)
        self.assertEqual(groups["mixed"]["repeated_read"]["incidents"], 1)
        self.assertEqual(groups["mixed"]["sessions"], 1)


class MixedTurnModelTest(AnalysisCase):
    """A turn with live responses from two models is mixed, not majority."""

    def setUp(self):
        super().setUp()
        self.session("codex:mix1", project="/p/app")
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES('codex','p','x',0)")
        # Turn t-mix has two model-a responses and one model-b response:
        # the majority would be model-a, but the turn is mixed.
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness, session_key,"
            " turn_id, model, total_tokens) VALUES"
            " ('codex:ma1', 1, 'codex', 'codex:mix1', 't-mix', 'model-a', 100),"
            " ('codex:ma2', 1, 'codex', 'codex:mix1', 't-mix', 'model-a', 100),"
            " ('codex:mb1', 1, 'codex', 'codex:mix1', 't-mix', 'model-b', 100)")
        self.con.execute(
            "INSERT INTO events(session_key, ts, family, native_id, name, target,"
            " turn_id, detail_json) VALUES"
            " ('codex:mix1', 1, 'read', 'r1', 'a.py', '/p/a.py', 't-mix', NULL),"
            " ('codex:mix1', 2, 'read', 'r2', 'a.py', '/p/a.py', 't-mix', NULL)")
        self.con.commit()

    def test_same_turn_mixed_model_incident_is_not_majority(self):
        turn_models = analysis._turn_models(self.con, "codex:mix1")
        self.assertEqual(turn_models.get("t-mix"), "mixed")
        groups = {g["group"]: g
                  for g in analysis.compare(self.con, by="model")["groups"]}
        self.assertEqual(groups["model-a"]["repeated_read"]["incidents"], 0)
        self.assertEqual(groups["model-b"]["repeated_read"]["incidents"], 0)
        self.assertEqual(groups["mixed"]["repeated_read"]["incidents"], 1)

    def test_overlap_responses_do_not_decide_the_turn(self):
        # An overlap model-b row on the same turn changes nothing: the
        # turn stays mixed from its live responses, and an overlap-only
        # turn stays unmapped.
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness, session_key,"
            " turn_id, model, total_tokens, is_overlap) VALUES"
            " ('codex:ov1', 1, 'codex', 'codex:mix1', 't-mix', 'model-b', 500, 1),"
            " ('codex:ov2', 1, 'codex', 'codex:mix1', 't-only', 'model-c', 500, 1)")
        self.con.commit()
        turn_models = analysis._turn_models(self.con, "codex:mix1")
        self.assertEqual(turn_models.get("t-mix"), "mixed")
        self.assertNotIn("t-only", turn_models)


class UnknownModelMembershipTest(AnalysisCase):
    """Live unknown-model responses group under unknown; overlap and
    fallback-only models never create groups."""

    def setUp(self):
        super().setUp()
        self.session("codex:u1", project="/p/app")
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES('codex','p','x',0)")
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness, session_key,"
            " turn_id, model, total_tokens, is_overlap) VALUES"
            " ('codex:ua1', 1, 'codex', 'codex:u1', 't1', 'model-a', 100, 0),"
            " ('codex:uu1', 1, 'codex', 'codex:u1', 't2', NULL, 200, 0),"
            " ('codex:uu2', 1, 'codex', 'codex:u1', 't2', NULL, NULL, 0),"
            " ('codex:ov1', 1, 'codex', 'codex:u1', 't3', 'overlap-only', 999, 1)")
        self.con.execute(
            "INSERT INTO turns(turn_id, source_id, session_key, model_observed)"
            " VALUES('t-fallback', 1, 'codex:u1', 'fallback-only')")
        self.con.commit()

    def test_unknown_group_exists_and_overlap_fallback_do_not(self):
        groups = {g["group"]: g
                  for g in analysis.compare(self.con, by="model")["groups"]}
        self.assertIn("model-a", groups)
        self.assertIn("unknown", groups)
        self.assertNotIn("overlap-only", groups)
        self.assertNotIn("fallback-only", groups)
        self.assertEqual(groups["unknown"]["sessions"], 1)
        self.assertEqual(groups["unknown"]["tokens_per_session"]["total"], 200)
        self.assertEqual(groups["unknown"]["unknown_token_responses"], 1)
        self.assertEqual(groups["model-a"]["tokens_per_session"]["total"], 100)


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
