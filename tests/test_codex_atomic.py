"""Task accounting for separate copies of a Codex atomic response."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from agent_observer import report
from agent_observer.adapters import codex, t3_purpose
from tests.helpers import LedgerCase, fixture
from tests.test_native_response import write_records


class CodexAtomicTest(LedgerCase):
    def setUp(self):
        super().setUp()
        rows = [json.loads(s) for s in Path(fixture("codex-mini.jsonl")).read_text().splitlines()]
        self.meta = rows[0]
        self.context = next(r for r in rows if r["type"] == "turn_context")
        self.atom = next(r for r in rows if r["type"] == "token_usage_record")
        self.key = "codex:sess-fixture-mini-01"
        with closing(sqlite3.connect(":memory:")) as src:
            t3_purpose.refresh(self.con, src, set())
        for task in ("A", "B"):
            self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES(?,0)", (task,))
            self.con.execute("INSERT INTO t3_purpose_runs(run_id,thread_id,task_id,scope) VALUES(?,'planner',?,'single')",
                             (task, task))
            self.con.execute("INSERT INTO t3_purpose_sessions VALUES(?,?)", (task, self.key))
            self.con.execute("INSERT INTO t3_purpose_inputs(run_id,session_key,native_id,kind,validation) "
                             "VALUES(?,?,?,'turn','codex_turn_started')", (task, self.key, task))

    def source(self, name, turn, total, *, torn=False):
        path = Path(self.tmp.name, name + ".jsonl")
        self.context["payload"].update(turn_id=turn, model="gpt-5.6-luna")
        self.atom["payload"].update(turn_id=turn, root_turn_id=turn)
        output = total - 1000 if isinstance(total, int) else total
        self.atom["payload"]["usage"].update(total_tokens=total, output_tokens=output)
        write_records(path, [self.meta, self.context])
        with path.open("a") as fh:
            line = json.dumps(self.atom)
            fh.write(line[:24] if torn else line + "\n")
        codex.import_codex_file(self.con, str(path))
        return path

    def check_owners(self, a, b):
        for task, expected in (("A", a), ("B", b)):
            rep = report.task_report(self.con, task)
            self.assertEqual(rep["attributed"]["responses"], int(expected is not None), task)
            if expected is None:
                self.assertIsNone(rep["total_cost"]["usd"], task)
                self.assertFalse(rep["total_cost"]["estimate"]["priced_responses"], task)
            else:
                self.assertEqual(rep["attributed"]["total_tokens"], expected, task)

    def test_torn_and_invalid_copy_cannot_withdraw_a_competing_turn(self):
        self.source("first", "A", 1100)
        self.source("second", "B", 1100)
        self.check_owners(None, None)
        for _ in range(2):
            self.source("second", "B", 1100, torn=True)
            self.check_owners(None, None)
        self.source("second", "B", "bad")
        self.check_owners(None, None)
        self.source("second", "A", 1100)
        self.check_owners(1100, None)

    def check_conflict(self, reverse):
        copies = [("first", "A", 1100), ("second", "B", 91100)]
        for args in (reversed(copies) if reverse else copies):
            self.source(*args)
        self.check_owners(None, None)
        # Correct the first source to the second. Its prior winner must not
        # decide who receives the independently corroborated response.
        self.source("first", "B", 91100)
        self.check_owners(None, 91100)

    def test_low_count_copy_first(self):
        self.check_conflict(False)

    def test_high_count_copy_first(self):
        self.check_conflict(True)

    def test_readable_removal_can_restore_other_sources_turn_and_counters(self):
        first = self.source("first", "A", 1100)
        self.source("second", "B", 91100)
        self.check_owners(None, None)
        write_records(first, [self.meta])
        codex.import_codex_file(self.con, str(first))
        self.check_owners(None, 91100)

    def check_repeat_context(self, append):
        first = self.source("first", "A", 1100)
        original = report.task_report(self.con, "A")["total_cost"]["estimate"]["estimated_cost_usd_partial"]
        self.assertGreater(original, 0)
        if append:
            copy = first
            self.context["payload"]["model"] = "gpt-6-astra"
            write_records(first, [self.context, self.atom], mode="a")
        else:
            copy = first.with_name("no-model.jsonl")
            write_records(copy, [self.meta, self.atom])
        for full in (False, True):
            codex.import_codex_file(self.con, str(copy), full=full)
            self.check_owners(1100, None)
            estimate = report.task_report(self.con, "A")["total_cost"]["estimate"]
            self.assertEqual(estimate["estimated_cost_usd_partial"], original)

    def test_repeated_atom_after_model_switch_keeps_its_original_charge(self):
        self.check_repeat_context(True)

    def test_contextless_duplicate_keeps_its_original_charge(self):
        self.check_repeat_context(False)

    def test_finishing_unrelated_tail_reconciles_an_earlier_response(self):
        retired = self.source("first", "A", 1100)
        surviving = self.source("second", "B", 91100)
        trailing = json.dumps(self.context) + "\n"
        with surviving.open("a") as fh:
            fh.write(trailing[:20])
        codex.import_codex_file(self.con, str(surviving))
        write_records(retired, [self.meta])
        codex.import_codex_file(self.con, str(retired))
        self.check_owners(None, None)

        with surviving.open("a") as fh:
            fh.write(trailing[20:])
        result = codex.import_codex_file(self.con, str(surviving))
        self.assertTrue(result["incremental"])
        self.assertEqual(result["lines"], 1)
        self.check_owners(None, 91100)
