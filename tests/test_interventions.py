"""Judge interventions (#60): deterministic selection, context, verdicts, recurrence.

Observer runs no model. It selects the interventions where a person acted,
hands a judge each intervention in context from the native logs, stores the
judge's verdicts and reports recurrence from them.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from agent_observer import cli, db, interventions, verdicts
from agent_observer.adapters import claude, codex
from agent_observer.adapters import t3 as t3_adapter
from agent_observer.ingest import text_hash

TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
LONG = ("no, you deleted the build directory without asking me first. "
        + "It held the only copy of the benchmark outputs. " * 8
        + f"use GH_TOKEN={TOKEN} for the restore")
WRAPPED = ("<t3_code_instructions>\nmode: default\n</t3_code_instructions>\n"
           "stop starting new agents, use the one you have")
BRIEF = "Read the brief at /tmp/brief.md and do exactly what it says."


def _write(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def _line(session, kind, uuid, content, second, entrypoint="cli", **extra):
    record = {"sessionId": session, "cwd": "/work/alpha", "version": "2.1.280",
              "entrypoint": entrypoint, "type": kind, "uuid": uuid,
              "timestamp": f"2026-10-01T10:{second // 60:02d}:{second % 60:02d}Z",
              "message": {"role": kind, "content": content}}
    if kind == "assistant":
        record["message"].update({"id": f"m-{uuid}", "model": "claude-opus-5-5"})
    record.update(extra)
    return record


def _tool_use(call, name, value):
    return [{"type": "tool_use", "id": call, "name": name, "input": value}]


def _tool_result(call, content, error=False):
    return [{"type": "tool_result", "tool_use_id": call, "content": content,
             "is_error": error}]


def claude_tree(root):
    """Three Claude sessions: interactive, headless and Chromeria."""
    s1 = "sess-one"
    _write(os.path.join(root, "proj", f"{s1}.jsonl"), [
        _line(s1, "user", "u1", "please add the cache", 0),
        _line(s1, "assistant", "a1",
              [{"type": "text", "text": "Adding the cache now."}], 1),
        _line(s1, "assistant", "a2",
              _tool_use("toolu_rm", "Bash", {"command": "rm -rf build"}), 2),
        _line(s1, "user", "r1", _tool_result("toolu_rm", "removed build/"), 3),
        _line(s1, "assistant", "a3",
              [{"type": "text", "text": "Done, I also deleted build."}], 4),
        _line(s1, "user", "u2", LONG, 5),
        _line(s1, "assistant", "a4",
              [{"type": "text", "text": "Sorry, restoring build now."}], 6),
        _line(s1, "user", "u3", "[Request interrupted by user]", 7),
        _line(s1, "user", "u4", "keep going", 8),
        _line(s1, "assistant", "a5",
              _tool_use("toolu_hook", "Bash", {"command": "git push"}), 9),
        _line(s1, "user", "r2",
              _tool_result("toolu_hook", "PreToolUse:Bash hook error: read X",
                           error=True), 10,
              toolDenialKind="permission-rule"),
        _line(s1, "assistant", "a6",
              _tool_use("toolu_edit", "Edit", {"file_path": "/work/a.py"}), 11),
        _line(s1, "user", "r3", _tool_result("toolu_edit", "rejected", True),
              12, toolDenialKind="user-rejected"),
        # Claude records every rejection a second time as an interrupt;
        # it is the same act and never a second intervention.
        _line(s1, "user", "u5", "[Request interrupted by user for tool use]",
              12),
        _line(s1, "assistant", "a7",
              [{"type": "text", "text": "Understood, leaving a.py alone."}], 13),
    ])
    s2 = "sess-two"
    _write(os.path.join(root, "proj", f"{s2}.jsonl"), [
        _line(s2, "user", "v1", "Benchmark task. Read case 7.", 60,
              entrypoint="sdk-cli"),
        _line(s2, "assistant", "b1",
              [{"type": "text", "text": "Reading case 7."}], 61,
              entrypoint="sdk-cli"),
    ])
    s3 = "sess-three"
    _write(os.path.join(root, "proj", f"{s3}.jsonl"), [
        _line(s3, "user", "w1", WRAPPED, 120, entrypoint="sdk-ts",
              promptSource="sdk"),
        _line(s3, "assistant", "c1",
              [{"type": "text", "text": "Stopping the extra agents."}], 121,
              entrypoint="sdk-ts"),
        _line(s3, "user", "w2", "Delegated task finished.", 122,
              entrypoint="sdk-ts", promptSource="sdk"),
        # A stop in a top-level Chromeria thread is a person.
        _line(s3, "user", "w3", "[Request interrupted by user]", 123,
              entrypoint="sdk-ts"),
        # Synthetic input with no agent evidence (a reminder): no intervention.
        _line(s3, "user", "w4", "Background task finished.", 124,
              entrypoint="sdk-ts", promptSource="sdk"),
    ])
    s4 = "sess-four"
    _write(os.path.join(root, "proj", f"{s4}.jsonl"), [
        _line(s4, "assistant", "d1",
              _tool_use("toolu_cp", "Bash", {"command": "scp a bestia:"}), 180,
              entrypoint="sdk-ts"),
        # In a child thread an agent can stop or refuse too: unknown.
        _line(s4, "user", "x1", _tool_result("toolu_cp", "rejected", True),
              181, entrypoint="sdk-ts", toolDenialKind="user-rejected"),
        _line(s4, "user", "x2", "[Request interrupted by user for tool use]",
              181, entrypoint="sdk-ts"),
        # Two seconds later is no longer the rejection's echo.
        _line(s4, "user", "x3", "[Request interrupted by user]", 183,
              entrypoint="sdk-ts"),
    ])
    s5 = "sess-five"
    _write(os.path.join(root, "proj", f"{s5}.jsonl"), [
        _line(s5, "assistant", "e1",
              _tool_use("toolu_lx", "Bash", {"command": "make deploy"}), 240,
              entrypoint="sdk-ts"),
        _line(s5, "user", "y1", _tool_result("toolu_lx", "rejected", True),
              241, entrypoint="sdk-ts", toolDenialKind="user-rejected"),
        _line(s5, "user", "y2", "[Request interrupted by user]", 245,
              entrypoint="sdk-ts"),
    ])


def codex_rollout(path):
    def line(ordinal, rtype, payload):
        return {"ordinal": ordinal, "type": rtype, "payload": payload,
                "timestamp": f"2026-10-01T11:00:{ordinal:02d}.000Z"}
    _write(path, [
        line(0, "session_meta", {"session_id": "cx-1", "id": "cx-1",
                                 "cli_version": "0.155.0", "cwd": "/work/beta",
                                 "originator": "t3code_desktop"}),
        line(1, "event_msg", {"type": "task_started", "turn_id": "t1"}),
        line(2, "turn_context", {"turn_id": "t1", "model": "gpt-6",
                                 "effort": "high", "cwd": "/work/beta"}),
        line(3, "response_item", {
            "type": "message", "role": "user", "id": "msg-u1",
            "content": [{"type": "input_text", "text": BRIEF}],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["user.text"]}}),
        line(4, "response_item", {"type": "message", "role": "assistant",
                                  "id": "msg-a1", "content": [
                                      {"type": "output_text",
                                       "text": "Reading the brief."}]}),
        line(5, "event_msg", {"type": "turn_aborted", "turn_id": "t1",
                              "reason": "interrupted"}),
    ])


class InterventionsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.con = db.connect(os.path.join(self.root, "observer.db"))
        db.init_db(self.con)
        t3_adapter._ensure_tables(self.con)
        # Chromeria evidence: a person started sess-three's first turn;
        # an agent sent the Codex brief; cx-1 is a child thread.
        self.con.execute("INSERT INTO t3_turn_origins VALUES(?,?,?,?)",
                         ("w1", "th-root", "client", None))
        self.con.execute("INSERT INTO t3_turn_origins VALUES(?,?,?,?)",
                         ("w2", "th-root", "server", None))
        self.con.execute("INSERT INTO t3_native_prompt_origins VALUES(?,?,?)",
                         ("codex:cx-1", "codex:msg-u1", "server"))
        threads = "INSERT INTO t3_threads(thread_id, root_thread_id,"\
            " provider, native_session, opened_by) VALUES(?,?,?,?,?)"
        self.con.execute(threads, ("th-root", "th-root", "claudeAgent",
                                   "sess-three", "client"))
        self.con.execute(threads, ("th-child", "th-root", "codex", "cx-1",
                                   "server"))
        self.con.execute(threads, ("th-child2", "th-root", "claudeAgent",
                                   "sess-four", "server"))
        # A top-level thread an agent launched: its stops prove no person.
        self.con.execute(threads, ("th-launch", "th-launch", "claudeAgent",
                                   "sess-five", "server"))
        claude_tree(os.path.join(self.root, "claude"))
        claude.sync(self.con, root=os.path.join(self.root, "claude"))
        rollout = os.path.join(self.root, "codex", "rollout-cx-1.jsonl")
        codex_rollout(rollout)
        codex.import_codex_file(self.con, rollout)
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def select(self, **kw):
        return interventions.select(self.con, **kw)

    def ids(self, result):
        return {m["id"]: m for m in result["interventions"]}


class SelectTest(InterventionsCase):
    def test_every_origin_is_classified_from_evidence(self):
        found = self.ids(self.select(created_by=None))
        expected = {
            "sub:claude:u1": ("prompt", "user"),
            "sub:claude:u2": ("prompt", "user"),
            "sub:claude:u3": ("interrupt", "user"),
            "sub:claude:u4": ("prompt", "user"),
            "ev:claude:sess-one/permission/toolu_edit": ("rejection", "user"),
            "sub:claude:v1": ("prompt", "agent"),
            "sub:claude:w1": ("prompt", "user"),
            # A Chromeria server turn: another agent wrote to this session.
            "sub:claude:w2": ("prompt", "agent"),
            "sub:claude:w3": ("interrupt", "user"),
            "sub:claude:w4": ("prompt", "unknown"),
            "ev:claude:sess-four/permission/toolu_cp": ("rejection", "unknown"),
            "sub:claude:x3": ("interrupt", "unknown"),
            "ev:claude:sess-five/permission/toolu_lx": ("rejection", "unknown"),
            "sub:claude:y2": ("interrupt", "unknown"),
            "sub:codex:msg-u1": ("prompt", "agent"),
            "ev:codex:cx-1/lifecycle/turn_aborted:t1": ("interrupt", "unknown"),
        }
        self.assertEqual({k: (v["kind"], v["created_by"]) for k, v in found.items()},
                         expected)
        self.assertEqual(found["sub:claude:w1"]["thread"], "root")
        self.assertEqual(found["ev:codex:cx-1/lifecycle/turn_aborted:t1"]["thread"],
                         "child")
        self.assertEqual(found["sub:claude:u1"]["thread"], "none")
        self.assertEqual(found["sub:claude:y2"]["thread"], "launched")
        self.assertEqual(found["sub:claude:w4"]["created_by"], "unknown")

    def test_ledger_without_opened_by_stays_conservative(self):
        # A ledger synced before opened_by existed: top-level threads read
        # as launched, children stay children, nothing turns human.
        self.con.execute("ALTER TABLE t3_threads DROP COLUMN opened_by")
        found = self.ids(self.select(created_by=None))
        self.assertEqual(found["sub:claude:w3"]["thread"], "launched")
        self.assertEqual(found["sub:claude:w3"]["created_by"], "unknown")
        self.assertEqual(
            found["ev:claude:sess-four/permission/toolu_cp"]["created_by"],
            "unknown")

    def test_opening_marks_moments_before_any_agent_activity(self):
        found = self.ids(self.select(created_by=None))
        self.assertTrue(found["sub:claude:u1"]["opening"])
        self.assertTrue(found["sub:codex:msg-u1"]["opening"])
        self.assertFalse(found["sub:claude:u2"]["opening"])
        self.assertFalse(found["sub:claude:w2"]["opening"])
        self.assertTrue(interventions.show(self.con, ["sub:claude:u1"])[0]["opening"])
        self.assertFalse(interventions.show(self.con, ["sub:claude:u2"])[0]["opening"])

    def test_default_selects_human_and_agent_moments_and_counts_the_rest(self):
        # Corrections from a person and from another agent both count
        # (user decision, 2026-10-12); unknown origin stays out.
        result = self.select()
        self.assertEqual({m["created_by"] for m in result["interventions"]},
                         {"user", "agent"})
        self.assertEqual(len(result["interventions"]), 10)
        self.assertEqual(result["counts"]["by_created_by"],
                         {"user": 7, "agent": 3, "unknown": 6})

    def test_hook_and_rule_denials_are_counted_never_selected(self):
        result = self.select(created_by=None)
        self.assertNotIn("ev:claude:sess-one/permission/toolu_hook",
                         self.ids(result))
        self.assertEqual(result["policy_denials"],
                         {"hook_or_rule": 1, "automode": 0})

    def test_scope_filters(self):
        self.assertEqual(
            set(self.ids(self.select(created_by=None, harness="codex"))),
            {"sub:codex:msg-u1", "ev:codex:cx-1/lifecycle/turn_aborted:t1"})
        self.assertEqual(
            set(self.ids(self.select(project="beta", created_by=None))),
            {"sub:codex:msg-u1", "ev:codex:cx-1/lifecycle/turn_aborted:t1"})
        since = interventions.iso_to_ts("2026-10-01T10:00:06Z")
        until = interventions.iso_to_ts("2026-10-01T10:00:09Z")
        self.assertEqual(set(self.ids(self.select(since=since, until=until,
                                                  created_by=("user",)))),
                         {"sub:claude:u3", "sub:claude:u4"})
        self.assertEqual(set(self.ids(self.select(kinds=["interrupt"],
                                                  created_by=("user",)))),
                         {"sub:claude:u3", "sub:claude:w3"})

    def test_session_attribute_filter(self):
        verdicts.add_session_attributes(self.con, [
            {"session": "claude:sess-three", "name": "arm", "value": "holdout"},
            {"session": "claude:sess-one", "name": "arm", "value": "served"}])
        self.assertEqual(set(self.ids(self.select(attrs={"arm": "holdout"}))),
                         {"sub:claude:w1", "sub:claude:w2", "sub:claude:w3"})


class ShowTest(InterventionsCase):
    def show(self, *ids, **kw):
        return interventions.show(self.con, list(ids), **kw)

    def test_correction_in_context_with_full_redacted_text(self):
        [item] = self.show("sub:claude:u2")
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["kind"], "prompt")
        self.assertTrue(item["text"].startswith("no, you deleted the build"))
        self.assertGreater(len(item["text"]), 300)
        self.assertNotIn(TOKEN, item["text"])
        [turn] = item["before"]
        self.assertEqual(turn["prompt"]["text"], "please add the cache")
        self.assertEqual(turn["prompt"]["id"], "sub:claude:u1")
        self.assertEqual(turn["prompt"]["created_by"], "user")
        activity = turn["activity"]
        self.assertEqual([b["type"] for b in activity],
                         ["assistant", "tool", "assistant"])
        self.assertEqual(activity[1]["tool"], "Bash")
        self.assertIn("rm -rf build", activity[1]["input"])
        self.assertIn("removed build/", activity[1]["output"])
        self.assertEqual(activity[2]["text"], "Done, I also deleted build.")
        [after] = item["after"]
        self.assertEqual([a["text"] for a in after["activity"]],
                         ["Sorry, restoring build now."])
        self.assertEqual(after["prompt"]["id"], "sub:claude:u3")
        self.assertEqual(after["prompt"]["kind"], "interrupt")

    def test_context_window_is_a_parameter(self):
        [bare] = self.show("sub:claude:u2", turns_before=0, turns_after=0)
        self.assertEqual((bare["before"], bare["after"]), ([], []))
        self.assertTrue(bare["text"].startswith("no, you deleted"))
        self.assertEqual(bare["window"]["turns_before"], 0)
        [wide] = self.show("sub:claude:u4", turns_before=3, turns_after=2)
        self.assertEqual([t["prompt"]["id"] for t in wide["before"]],
                         ["sub:claude:u1", "sub:claude:u2", "sub:claude:u3"])
        self.assertEqual([a["text"] for a in wide["before"][1]["activity"]],
                         ["Sorry, restoring build now."])
        self.assertEqual(len(wide["after"]), 1)  # the session ends
        [two] = self.show("sub:claude:u2", turns_after=2)
        self.assertEqual([t["prompt"]["id"] for t in two["after"]],
                         ["sub:claude:u3", "sub:claude:u4"])
        [few] = self.show("sub:claude:u2", items=1)
        self.assertEqual([a["text"] for a in few["before"][0]["activity"]],
                         ["Done, I also deleted build."])

    def test_window_edges(self):
        [rejection] = self.show("ev:claude:sess-four/permission/toolu_cp")
        [turn] = rejection["before"]
        self.assertIsNone(turn["prompt"])  # activity before any prompt
        self.assertEqual(turn["activity"][0]["tool"], "Bash")
        [brief] = self.show("sub:codex:msg-u1")
        self.assertEqual(brief["after"][0]["prompt"]["kind"], "interrupt")
        self.assertEqual(brief["after"][0]["prompt"]["created_by"], "unknown")
        [few] = self.show("sub:claude:u2", items=1)
        self.assertEqual(few["before"][0]["omitted"], 2)
        self.assertEqual(few["after"][0]["omitted"], 0)
        with self.assertRaises(ValueError):
            self.show("sub:claude:u2", turns_before=-1)

    def test_agent_message_is_a_moment_with_its_source(self):
        [item] = self.show("sub:claude:w2")
        self.assertEqual(item["created_by"], "agent")
        self.assertEqual(item["text"], "Delegated task finished.")
        self.assertEqual(item["before"][0]["prompt"]["created_by"], "user")

    def test_wrapped_prompt_keeps_the_text_inside_chromeria_tags(self):
        [item] = self.show("sub:claude:w1")
        self.assertIn("<t3_code_instructions>", item["text"])
        self.assertIn("stop starting new agents", item["text"])
        self.assertEqual(item["created_by"], "user")

    def test_rejection_and_codex_interrupt(self):
        [rejection] = self.show("ev:claude:sess-one/permission/toolu_edit")
        self.assertEqual(rejection["status"], "ok")
        self.assertEqual(rejection["kind"], "rejection")
        self.assertEqual(rejection["tool"], "Edit")
        self.assertEqual([a["text"] for a in rejection["after"][0]["activity"]],
                         ["Understood, leaving a.py alone."])
        [interrupt] = self.show("ev:codex:cx-1/lifecycle/turn_aborted:t1")
        self.assertEqual(interrupt["status"], "ok")
        [turn] = interrupt["before"]
        self.assertEqual(turn["prompt"]["text"], BRIEF)
        self.assertEqual(turn["prompt"]["created_by"], "agent")
        self.assertEqual([b["text"] for b in turn["activity"]],
                         ["Reading the brief."])
        [own] = interrupt["after"]  # the session ends after the interrupt
        self.assertEqual((own["activity"], own["prompt"]), ([], None))

    def test_limits_clip_long_fields_and_item_counts(self):
        [item] = self.show("sub:claude:u2", items=1, chars=10)
        self.assertEqual(len(item["before"][0]["activity"]), 1)
        self.assertIn("chars cut", item["before"][0]["activity"][0]["text"])
        # The person's own words are never cut by the context limit.
        self.assertGreater(len(item["text"]), 300)
        self.assertNotIn("chars cut", item["text"])
        [item] = self.show("sub:claude:u2", text_chars=40)
        self.assertLessEqual(len(item["text"]), 40 + 40)
        self.assertIn("chars cut", item["text"])

    def test_unknown_and_malformed_ids(self):
        self.assertEqual(self.show("sub:claude:nope")[0]["status"],
                         "no_such_intervention")
        self.assertEqual(self.show("bogus")[0]["status"], "no_such_intervention")

    def test_missing_log_reports_source_missing(self):
        os.remove(os.path.join(self.root, "claude", "proj", "sess-one.jsonl"))
        [item] = self.show("sub:claude:u2")
        self.assertEqual(item["status"], "source_missing")
        self.assertNotIn("text", item)


JUDGE = "opus-5.5/correction-v1"


def verdict(intervention, value="yes", problem=None, group=None, judge=JUDGE,
            behavior="correction"):
    record = {"intervention": intervention, "behavior": behavior, "judge": judge,
              "verdict": value}
    if problem is not None:
        record["problem"] = problem
    if group is not None:
        record["group"] = group
    return record


class VerdictTest(InterventionsCase):
    def test_batch_is_stored_and_regrouped_in_place(self):
        out = verdicts.add(self.con, [
            verdict("sub:claude:u2", problem="Deleted files without asking"),
            verdict("sub:claude:u4", "no")])
        self.assertEqual(out, {"stored": 2, "updated": 0})
        out = verdicts.add(self.con, [
            verdict("sub:claude:u2", problem="Deleted files without asking",
                    group="destructive-without-approval")])
        self.assertEqual(out, {"stored": 0, "updated": 1})
        [group] = verdicts.groups(self.con, "correction")
        self.assertEqual(group["group"], "destructive-without-approval")
        self.assertEqual(group["occurrences"], 1)

    def test_invalid_line_rejects_the_whole_batch(self):
        bad = [verdict("sub:claude:u2", problem="ok"),
               verdict("sub:claude:u4", "yes"),
               verdict("sub:claude:u1", "no", problem="x"),
               verdict("sub:claude:zz", "no"),
               verdict("sub:claude:u1", "maybe"),
               {**verdict("sub:claude:u1", "no"), "extra": 1},
               verdict("sub:claude:u3", problem="two\nlines"),
               verdict("sub:claude:u3", problem="ok", group="Bad Group"),
               verdict("sub:claude:u2", "no")]
        with self.assertRaises(verdicts.VerdictError) as caught:
            verdicts.add(self.con, bad)
        self.assertEqual([e["line"] for e in caught.exception.errors],
                         [2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM verdicts").fetchone()[0], 0)

    def test_list_shows_each_verdict_with_its_moment(self):
        verdicts.add(self.con, [
            verdict("sub:claude:u2", problem="Deleted files without asking"),
            verdict("sub:claude:u4", "no")])
        listed = verdicts.listing(self.con, "correction", verdict="yes")
        self.assertEqual([(v["intervention"], v["problem"], v["group"], v["session"])
                          for v in listed],
                         [("sub:claude:u2", "Deleted files without asking",
                           None, "claude:sess-one")])
        self.assertEqual(len(verdicts.listing(self.con, "correction")), 2)

    def test_problem_lines_are_redacted(self):
        verdicts.add(self.con, [verdict("sub:claude:u2",
                                        problem=f"pasted {TOKEN}")])
        stored = self.con.execute("SELECT problem FROM verdicts").fetchone()[0]
        self.assertNotIn(TOKEN, stored)

    def test_unjudged_lists_only_moments_without_a_verdict(self):
        verdicts.add(self.con, [verdict("sub:claude:u2", "no")])
        left = self.ids(interventions.select(self.con, unjudged="correction"))
        self.assertNotIn("sub:claude:u2", left)
        self.assertIn("sub:claude:u1", left)
        self.assertIn("sub:claude:u2",
                      self.ids(interventions.select(self.con, unjudged="praise")))
        # A second judge still gets the intervention as work.
        self.assertIn("sub:claude:u2", self.ids(interventions.select(
            self.con, unjudged="correction", judge="sol/v1")))
        self.assertNotIn("sub:claude:u2", self.ids(interventions.select(
            self.con, unjudged="correction", judge=JUDGE)))


class RecurrenceTest(InterventionsCase):
    def setUp(self):
        super().setUp()
        verdicts.add(self.con, [
            verdict("sub:claude:u2", problem="Deleted files without asking",
                    group="destructive-without-approval"),
            verdict("ev:claude:sess-one/permission/toolu_edit",
                    problem="Edited a file it was told to leave alone",
                    group="destructive-without-approval"),
            verdict("sub:claude:w1", problem="Started extra agents",
                    group="too-many-agents"),
            verdict("sub:claude:u3", "unclear"),
            verdict("sub:claude:u4", "no"),
            verdict("sub:claude:u1", "no"),
            verdict("ev:codex:cx-1/lifecycle/turn_aborted:t1",
                    problem="Kept reading after the brief", group=None)])

    def test_groups_repeats_sessions_and_hosts(self):
        report = verdicts.recurrence(self.con, "correction")
        self.assertEqual(report["judge"], JUDGE)
        # The Codex interrupt is unknown origin: outside the default.
        self.assertEqual(report["occurrences"], 3)
        self.assertEqual(report["ungrouped"], 0)
        self.assertEqual(report["problems"], 2)
        self.assertEqual(report["recurring_problems"], 0)
        first = report["groups"][0]
        self.assertEqual(first["group"], "destructive-without-approval")
        self.assertEqual(first["occurrences"], 2)
        self.assertEqual(first["repeats"], 1)
        self.assertEqual(first["sessions"], 1)
        self.assertEqual(first["hosts"], ["claude"])
        self.assertEqual(first["problem"], "Deleted files without asking")
        self.assertEqual(report["policy_denials"],
                         {"hook_or_rule": 1, "automode": 0})
        coverage = report["coverage"]
        self.assertEqual(coverage["judged"], 6)
        self.assertEqual(coverage["unjudged_by_created_by"],
                         {"user": 1, "agent": 3, "unknown": 0})
        self.assertEqual(report["by_created_by"]["user"]["occurrences"], 3)
        self.assertEqual(report["by_created_by"]["agent"]["occurrences"], 0)
        self.assertEqual(report["other_created_by"]["unknown"]["occurrences"], 1)

    def test_problem_recurs_when_seen_in_two_sessions(self):
        verdicts.add(self.con, [verdict(
            "sub:claude:w1", problem="Started extra agents",
            group="destructive-without-approval")])
        report = verdicts.recurrence(self.con, "correction")
        self.assertEqual(report["recurring_problems"], 1)
        self.assertEqual(report["groups"][0]["sessions"], 2)

    def test_human_and_agent_corrections_combine_with_a_breakdown(self):
        # An agent correcting a worker in the same problem group as a
        # person's correction: one problem, recurring across sessions.
        verdicts.add(self.con, [verdict(
            "sub:claude:v1", problem="Deleted files without asking",
            group="destructive-without-approval")])
        report = verdicts.recurrence(self.con, "correction")
        self.assertEqual(report["occurrences"], 4)
        self.assertEqual(report["by_created_by"]["user"]["occurrences"], 3)
        self.assertEqual(report["by_created_by"]["agent"]["occurrences"], 1)
        self.assertEqual(report["by_created_by"]["agent"]["problems"], 1)
        first = report["groups"][0]
        self.assertEqual(first["by_created_by"], {"user": 2, "agent": 1})
        self.assertEqual(first["sessions"], 2)
        self.assertEqual(report["recurring_problems"], 1)
        self.assertEqual(report["by_created_by"]["user"]["recurring_problems"], 0)
        human = verdicts.recurrence(self.con, "correction", created_by=("user",))
        self.assertEqual(human["occurrences"], 3)
        self.assertEqual(human["other_created_by"]["agent"]["occurrences"], 1)
        every = verdicts.recurrence(self.con, "correction", created_by=None)
        self.assertEqual(every["occurrences"], 5)

    def test_session_attribute_and_period_filters(self):
        verdicts.add_session_attributes(self.con, [
            {"session": "claude:sess-three", "name": "arm", "value": "holdout"}])
        report = verdicts.recurrence(self.con, "correction",
                                     attrs={"arm": "holdout"})
        self.assertEqual(report["occurrences"], 1)
        self.assertEqual([g["group"] for g in report["groups"]],
                         ["too-many-agents"])

    def test_two_judges_require_a_choice(self):
        verdicts.add(self.con, [verdict("sub:claude:u4", judge="sol/v1",
                                        problem="Other view", group="x")])
        with self.assertRaises(verdicts.VerdictError):
            verdicts.recurrence(self.con, "correction")
        report = verdicts.recurrence(self.con, "correction", judge="sol/v1")
        self.assertEqual(report["occurrences"], 1)


class CliTest(InterventionsCase):
    def run_cli(self, *argv, stdin=None):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin",
                                              io.StringIO(stdin or "")):
            code = cli.main(["--db", os.path.join(self.root, "observer.db"),
                             *argv])
        return code, out.getvalue()

    def test_judge_round_trip_through_json_commands(self):
        self.con.commit()
        code, out = self.run_cli("interventions", "--json")
        self.assertEqual(code, 0)
        listed = json.loads(out)
        self.assertEqual(len(listed["interventions"]), 10)
        code, out = self.run_cli("intervention", "show", "sub:claude:u2", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["interventions"][0]["status"], "ok")
        lines = "\n".join(json.dumps(v) for v in [
            verdict("sub:claude:u2", problem="Deleted files without asking",
                    group="destructive-without-approval")])
        code, out = self.run_cli("verdict", "add", "--json", stdin=lines)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["stored"], 1)
        code, out = self.run_cli("verdict", "add", "--json",
                                 stdin='{"intervention": "x"}')
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["errors"][0]["line"], 1)
        code, out = self.run_cli("verdict", "list", "--behavior",
                                 "correction", "--verdict", "yes", "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["verdicts"][0]["intervention"],
                         "sub:claude:u2")
        code, out = self.run_cli("recurrence", "--behavior", "correction",
                                 "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["occurrences"], 1)
        code, out = self.run_cli("recurrence", "--behavior", "correction")
        self.assertIn("destructive-without-approval", out)
        code, _ = self.run_cli("interventions", "--judge", JUDGE, "--json")
        self.assertEqual(code, 2)
        code, out = self.run_cli(
            "session-attr", "add", "--json",
            stdin='{"session": "claude:sess-one", "name": "arm", "value": "served"}')
        self.assertEqual(code, 0, out)


class CopiedContextOrderTest(unittest.TestCase):
    def test_copied_answer_is_not_a_before_window(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            meta = {"type": "session_meta", "payload": {
                "id": "copied-context", "thread_source": "user"}}

            def msg(role, name, text):
                payload = {"type": "message", "role": role, "id": name,
                           "content": [{"type": "text", "text": text}]}
                if role == "user":
                    payload["internal_chat_message_metadata_passthrough"] = {
                        "content_item_kinds": ["user.text"]}
                return {"type": "response_item", "payload": payload}

            original = os.path.join(tmp.name, "copied-a.jsonl")
            copy = os.path.join(tmp.name, "copied-b.jsonl")
            _write(original, [meta, msg("user", "u1", "original question"),
                              msg("assistant", "a1", "original answer")])
            _write(copy, [meta, msg("user", "u2", "new question"),
                          msg("assistant", "a2", "new answer"),
                          msg("user", "u1", "original question"),
                          msg("assistant", "a1", "original answer")])
            for reverse in (False, True):
                con = db.connect(os.path.join(tmp.name, f"copied-{reverse}.db"))
                db.init_db(con)
                try:
                    for full in (False, True, False):
                        for path in ((copy, original) if reverse else (original, copy)):
                            codex.import_codex_file(con, path, full=full)
                        shown = interventions.show(
                            con, ["sub:codex:u1", "sub:codex:u2"],
                            turns_before=1, turns_after=1)
                        for item in shown:
                            self.assertEqual(item["status"], "order_unknown", item)
                            self.assertNotIn("before", item)
                        sequences = shown[0]["source_sequences"]
                        orders = []
                        for sequence in sequences:
                            users = []
                            for group in sequence:
                                for ident in group:
                                    if ident[0] == "user":
                                        users.append(ident[1])
                            orders.append(users)
                        self.assertIn(["codex:u1"], orders)
                        self.assertTrue(any(
                            order.index("codex:u2") < order.index("codex:u1")
                            for order in orders
                            if "codex:u2" in order and "codex:u1" in order))
                finally:
                    con.close()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
