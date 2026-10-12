"""Fleet evidence across machines (#56): export, pull and combined report.

Each machine keeps its own ledger and transcripts; only aggregates leave
it. Fixtures are fabricated ledgers; the "secret" strings stand for the
free text, paths and branch names that must never reach an export.
"""

import json
import os
import stat
import subprocess
import sys

from agent_observer import db, fleet, report
from tests.helpers import LedgerCase

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS = ("secret-title", "/Users/secret", "feat/secret-branch",
           "secret prompt", "secret task title", "file:///secret")


def add_session(con, harness, native_id, tokens, model="fixture-model",
                semantics="codex:input_includes_cached,output_includes_reasoning"):
    con.execute(
        "INSERT INTO sources(harness, path, sha256, imported_at)"
        " VALUES(?,?,?,?)", (harness, f"/Users/secret/{native_id}.jsonl",
                             "x", 1791600000.0))
    source_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    key = f"{harness}:{native_id}"
    db.upsert_session(con, key, harness, native_id, source_id,
                      project_dir="/Users/secret/proj",
                      git_branch="feat/secret-branch", title="secret-title",
                      started_at=1791600000.0, ended_at=1791600100.0)
    con.execute(
        "INSERT INTO responses(response_id, source_id, harness, session_key,"
        " ordinal_num, model, semantics, input_tokens, cached_input_tokens,"
        " output_tokens, reasoning_output_tokens, total_tokens)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{key}:r1", source_id, harness, key, 1, model, semantics,
         tokens, 0, 10, 0, tokens + 10))
    con.execute(
        "INSERT INTO user_messages(native_id, source_id, session_key, kind,"
        " text_hash, text_excerpt, is_genuine) VALUES(?,?,?,?,?,?,?)",
        (f"{key}:s1", source_id, key, "genuine", "abc", "secret prompt", 1))
    return key


def add_task(con, task_id, key, url, project, state="complete"):
    con.execute(
        "INSERT INTO tasks(task_id, project, title, issue_url, origin,"
        " created_at) VALUES(?,?,?,?,?,?)",
        (task_id, project, "secret task title", url, "t3", 1791600000.0))
    con.execute(
        "INSERT INTO session_assignments(session_key, task_id, evidence,"
        " created_at) VALUES(?,?,?,?)", (key, task_id, "fixture", 1.0))
    con.execute(
        "INSERT INTO outcomes(task_id, acceptance_state, proof_ref,"
        " updated_at) VALUES(?,?,?,?)", (task_id, state, url, 1.0))


class ExportTest(LedgerCase):
    def test_export_holds_aggregates_only(self):
        key = add_session(self.con, "codex", "s-one", 100)
        add_task(self.con, "o/r#1", key, "https://github.com/o/r/pull/1",
                 "o/r")
        other = add_session(self.con, "claude", "s-two", 50,
                            semantics="claude:input_excludes_cache")
        add_task(self.con, "local-task", other, "file:///secret",
                 "not a repo", state="unknown")
        self.con.commit()
        doc = fleet.export(self.con, machine="rocky")
        dumped = json.dumps(doc)
        for secret in SECRETS:
            self.assertNotIn(secret, dumped)
        self.assertEqual(doc["schema"], fleet.SCHEMA)
        self.assertEqual(doc["machine"], "rocky")
        sessions = {s["session_key"]: s for s in doc["sessions"]}
        self.assertEqual(sessions[key]["usage"][0]["total_tokens"], 110)
        self.assertEqual(sessions[key]["harness"], "codex")
        tasks = {t["task_id"]: t for t in doc["tasks"]}
        self.assertEqual(tasks["o/r#1"]["issue_url"],
                         "https://github.com/o/r/pull/1")
        self.assertEqual(tasks["o/r#1"]["project"], "o/r")
        self.assertEqual(tasks["o/r#1"]["acceptance_state"], "complete")
        self.assertEqual(tasks["o/r#1"]["sessions"], [key])
        self.assertIsNone(tasks["local-task"]["issue_url"])
        self.assertIsNone(tasks["local-task"]["project"])

    def test_export_drops_project_and_url_with_trailing_newline(self):
        key = add_session(self.con, "codex", "s-four", 5)
        add_task(self.con, "T-50", key, "https://github.com/o/r/pull/1\n",
                 "o/r\n")
        self.con.commit()
        task = fleet.export(self.con, machine="rocky")["tasks"][0]
        self.assertIsNone(task["project"])
        self.assertIsNone(task["issue_url"])

    def test_free_text_task_ids_and_session_fields_stay_home(self):
        key = add_session(self.con, "codex", "s-three", 5)
        add_task(self.con, "fix the secret login bug", key, None, None)
        add_task(self.con, "T-42", key, None, None)
        add_task(self.con, "T-43\n", key, None, None)
        self.con.execute("UPDATE sessions SET parent_session_key=?,"
                         " agentsmd_version=? WHERE session_key=?",
                         ("codex:parent", "16.1.1", key))
        self.con.commit()
        doc = fleet.export(self.con, machine="rocky")
        self.assertNotIn("secret login", json.dumps(doc))
        self.assertEqual([t["task_id"] for t in doc["tasks"]], ["T-42"])
        self.assertEqual(doc["withheld_tasks"], 2)
        session = doc["sessions"][0]
        self.assertEqual(session["parent_session_key"], "codex:parent")
        self.assertEqual(session["agentsmd_version"], "16.1.1")

    def test_machine_names_are_slugs(self):
        with self.assertRaises(ValueError):
            fleet.export(self.con, machine="../etc")
        self.assertEqual(fleet.machine_name("Rocky.local"), "rocky")


def _doc(machine, sessions, tasks=()):
    """A minimal valid export document."""
    return {"schema": fleet.SCHEMA, "machine": machine,
            "observer_version": "0.0.0", "exported_at": "2026-10-12T12:00:00Z",
            "ledger_synced_at": "2026-10-12T11:00:00Z",
            "sessions": [{"session_key": k, "harness": "codex",
                          "usage": [{"harness": "codex", "model": "m",
                                     "effort": None, "semantics": "s1",
                                     "responses": 1, "total_tokens": t}]}
                         for k, t in sessions],
            "tasks": list(tasks), "withheld_tasks": 0}


def _task(task_id, sessions, state="complete"):
    return {"task_id": task_id, "project": "o/r", "issue_url": None,
            "origin": "t3", "acceptance_state": state, "sessions": sessions}


class ReportTest(LedgerCase):
    def test_combines_machines_counting_each_session_once(self):
        rocky = _doc("rocky", [("codex:a", 100), ("codex:dup", 40)],
                     [_task("o/r#1", ["codex:a", "codex:dup"]),
                      _task("o/r#9", ["codex:a"])])
        bestia = _doc("bestia", [("codex:b", 7), ("codex:dup", 40)],
                      [_task("o/r#1", ["codex:b", "codex:dup"],
                             state="unknown")])
        result = fleet.combine([rocky, bestia],
                               expect=["rocky", "cavallo", "bestia"])
        self.assertEqual(result["missing"], ["cavallo"])
        self.assertEqual(result["duplicate_sessions"],
                         [{"session_key": "codex:dup",
                           "machines": ["bestia", "rocky"]}])
        # 100 + 40 + 7: the duplicated session counts once.
        self.assertEqual(result["totals"]["s1"]["total_tokens"], 147)
        self.assertEqual(result["totals"]["s1"]["sessions"], 3)
        task = result["tasks"][0]
        self.assertEqual(task["task_id"], "o/r#1")
        # Whole sessions touching the task, each once: 100 + 40 + 7.
        self.assertEqual(task["session_usage"]["s1"]["total_tokens"], 147)
        self.assertEqual(task["sessions"], 3)
        # codex:a also belongs to o/r#9, so it is shared, not exclusive.
        self.assertEqual(task["shared_sessions"], 1)
        self.assertEqual(task["missing_sessions"], [])
        self.assertEqual(result["withheld_tasks"], {"bestia": 0, "rocky": 0})
        self.assertEqual(sorted(task["machines"]),
                         ["bestia", "rocky"])
        self.assertEqual(task["acceptance_states"],
                         {"bestia": "unknown", "rocky": "complete"})

    def test_task_sessions_missing_from_every_export_stay_visible(self):
        rocky = _doc("rocky", [("codex:a", 5)],
                     [_task("o/r#2", ["codex:a", "codex:gone"])])
        task = fleet.combine([rocky])["tasks"][0]
        self.assertEqual(task["session_usage"]["s1"]["total_tokens"], 5)
        self.assertEqual(task["missing_sessions"], ["codex:gone"])

    def test_text_report_shows_withheld_task_counts(self):
        doc = {**_doc("cavallo", []), "withheld_tasks": 4}
        text = fleet.render(fleet.combine([doc, _doc("rocky", [])]))
        self.assertIn("tasks: 0; free-text ids kept home: cavallo 4",
                      text.splitlines())

    def test_real_unknown_counts_are_carried_not_recounted(self):
        rows = [{"harness": "codex", "model": "m", "effort": None,
                 "semantics": "s1", "is_overlap": 0,
                 **{b: (None if b == "total_tokens" and i else 5)
                    for b in report.BUCKETS},
                 "reasoning_output_tokens": 0, "output_tokens": 5}
                for i in range(3)]
        usage = fleet._usage(report.model_usage(rows))
        doc = {**_doc("rocky", []), "sessions": [
            {"session_key": "codex:a", "harness": "codex", "usage": usage}]}
        totals = fleet.combine([doc])["totals"]["s1"]
        # One known total of 5 (a lower bound), two unknown responses.
        self.assertEqual(totals["total_tokens"], 5)
        self.assertEqual(totals["unknown_counts"], {"total_tokens": 2})
        self.assertEqual(totals["responses"], 3)

    def test_unknown_tokens_stay_visible(self):
        doc = _doc("rocky", [("codex:a", None), ("codex:b", 5)])
        totals = fleet.combine([doc])["totals"]["s1"]
        self.assertEqual(totals["total_tokens"], 5)
        self.assertEqual(totals["unknown_counts"], {"total_tokens": 1})

    def test_rejects_invalid_documents(self):
        good = _doc("rocky", [("codex:a", 5)], [_task("T-1", ["codex:a"])])
        session = good["sessions"][0]
        group = session["usage"][0]
        for bad in ({"schema": "other"}, {**good, "machine": "a/b"},
                    {**good, "machine": "rocky\n"},
                    {**good, "withheld_tasks": "3\n- fake line"},
                    {**good, "withheld_tasks": -1},
                    {**good, "observer_version": "1\n- fake"},
                    {**good, "sessions": [{**session, "session_key": "a\nb"}]},
                    {**good, "sessions": [{**session, "usage": [
                        {**group, "semantics": "s\n- fake"}]}]},
                    {**good, "tasks": [{**good["tasks"][0], "task_id": "T-1\n"}]},
                    {**good, "tasks": [{**good["tasks"][0],
                                        "acceptance_state": "ok\n- fake"}]},
                    {**good, "sessions": "x"},
                    {**good, "sessions": [{**session, "usage": "x"}]},
                    {**good, "sessions": [{**session, "session_key": ["x"]}]},
                    {**good, "sessions": [{**session, "usage": [
                        {**group, "total_tokens": "5"}]}]},
                    {**good, "sessions": [{**session, "usage": [
                        {**group, "semantics": ["s1"]}]}]},
                    {**good, "sessions": [{**session, "usage": [
                        {**group, "unknown_counts": ["x"]}]}]},
                    {**good, "tasks": [{**good["tasks"][0], "sessions": "abc"}]},
                    {**good, "tasks": [{**good["tasks"][0], "task_id": 7}]},
                    # A stored null or negative count must never reach _add.
                    {**good, "sessions": [{**session, "usage": [
                        {**group, "unknown_counts": {"total_tokens": None}}]}]},
                    {**good, "sessions": [{**session, "usage": [
                        {**group, "unknown_counts": {"total_tokens": -1}}]}]},
                    {**good, "sessions": [{**session, "usage": [
                        {**group, "total_tokens": -5}]}]},
                    # Line, format and C1 control characters, not just ASCII.
                    *({**good, "sessions": [{**session, "usage": [
                        {**group, "semantics": "s" + ch + "x"}]}]}
                      for ch in ("\x7f", "\x85", "\u2028", "\u202e",
                                 "\ud800")),
                    {**good, "sessions": [{**session, "usage": [
                        {**group, "total_tokens": fleet.MAX_COUNT + 1}]}]},
                    {**good, "withheld_tasks": fleet.MAX_COUNT + 1},
                    # Too many counter semantics would make reports quadratic.
                    {**good, "sessions": [{**session, "usage": [
                        {**group, "semantics": f"s{i}"}
                        for i in range(fleet.MAX_SEMANTICS + 1)]}]}):
            with self.subTest(bad=json.dumps(bad, ensure_ascii=True)[:300]):
                with self.assertRaises(ValueError):
                    fleet.validate(bad)

    def test_largest_accepted_counts_still_report(self):
        # The promise behind validate: anything it accepts reports, even
        # summed at the cap across many sessions and machines.
        top = fleet.MAX_COUNT
        docs = [fleet.validate(_doc(m, [(f"codex:{m}{i}", top)
                                        for i in range(50)]))
                for m in ("rocky", "bestia", "cavallo")]
        result = fleet.combine(docs)
        self.assertEqual(result["totals"]["s1"]["total_tokens"], 150 * top)
        fleet.render(result)
        json.dumps(result)


class PullTest(LedgerCase):
    def run_fake(self, stdout, code=0):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, code, stdout, "boom")
        return run, calls

    def test_pull_stores_a_validated_export(self):
        store = os.path.join(self.tmp.name, "fleet")
        run, calls = self.run_fake(json.dumps(_doc("bestia", [("codex:b", 7)])))
        result = fleet.pull("bestia", store, run=run)
        self.assertEqual(result["machine"], "bestia")
        self.assertEqual(calls[0][0], "ssh")
        self.assertEqual(calls[0][-3:], ["--", "bestia", "agent-observer export"])
        self.assertNotIn("warning", result)
        # This machine's own name setting must not leak into the check.
        from unittest import mock
        with mock.patch.dict(os.environ, {"AGENT_OBSERVER_MACHINE": "rocky"}):
            run, _ = self.run_fake(json.dumps(_doc("bestia", [])))
            self.assertNotIn("warning",
                             fleet.pull("bestia.tailnet.ts.net", store,
                                        run=run))
            run, _ = self.run_fake(json.dumps(_doc("bestia", [])))
            self.assertNotIn("warning",
                             fleet.pull("luke@bestia", store, run=run))
        path = os.path.join(store, "bestia.json")
        self.assertEqual(json.load(open(path))["machine"], "bestia")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual([d["machine"] for d in fleet.stored(store)],
                         ["bestia"])

    def test_pull_refuses_an_export_that_cannot_be_reported(self):
        from unittest import mock
        store = os.path.join(self.tmp.name, "fleet")
        run, _ = self.run_fake(json.dumps(_doc("bestia", [("codex:b", 7)])))
        with mock.patch.object(fleet, "render",
                               side_effect=ValueError("unprintable")):
            with self.assertRaises(fleet.PullError):
                fleet.pull("bestia", store, run=run)
        self.assertEqual(fleet.stored(store), [])

    def test_deeply_nested_export_fails_only_its_host(self):
        store = os.path.join(self.tmp.name, "fleet")
        deep = json.dumps(_doc("bestia", []))[:-1] + ', "x": ' \
            + "[" * 5000 + "]" * 5000 + "}"
        run, _ = self.run_fake(deep)
        try:
            fleet.pull("bestia", store, run=run)
        except fleet.PullError:
            pass  # Refused where the parser's recursion limit is lower.
        # Either way nothing outside the export schema is ever stored.
        for doc in fleet.stored(store):
            self.assertNotIn("x", doc)

    def test_pull_stores_only_schema_fields(self):
        store = os.path.join(self.tmp.name, "fleet")
        doc = _doc("bestia", [("codex:b", 7)], [_task("T-1", ["codex:b"])])
        doc["extra"] = "drop me"
        doc["sessions"][0]["title"] = "secret-title"
        doc["sessions"][0]["usage"][0]["note"] = "drop me"
        doc["tasks"][0]["title"] = "secret task title"
        run, _ = self.run_fake(json.dumps(doc))
        fleet.pull("bestia", store, run=run)
        text = open(os.path.join(store, "bestia.json")).read()
        for word in ("drop me", "secret-title", "secret task title"):
            self.assertNotIn(word, text)

    def test_pull_warns_when_the_host_names_another_machine(self):
        store = os.path.join(self.tmp.name, "fleet")
        run, _ = self.run_fake(json.dumps(_doc("cavallo", [])))
        result = fleet.pull("bestia", store, run=run)
        self.assertIn("cavallo", result["warning"])

    def test_failed_or_invalid_pull_writes_nothing(self):
        store = os.path.join(self.tmp.name, "fleet")
        malformed = {**_doc("cavallo", []), "sessions": [{"usage": "x"}]}
        for stdout, code in (("", 255), ("not json", 0),
                             (json.dumps({"schema": "other"}), 0),
                             (json.dumps(malformed), 0)):
            run, _ = self.run_fake(stdout, code)
            with self.assertRaises(fleet.PullError):
                fleet.pull("cavallo", store, run=run)
        self.assertEqual(fleet.stored(store), [])


class CliTest(LedgerCase):
    def test_export_and_report_commands(self):
        add_session(self.con, "codex", "s-one", 100)
        self.con.commit()
        env = {**os.environ, "AGENT_OBSERVER_MACHINE": "rocky"}
        out = subprocess.run(
            [sys.executable, "-m", "agent_observer", "--db", self.db_path,
             "export"], cwd=REPO, env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["machine"], "rocky")
        report = subprocess.run(
            [sys.executable, "-m", "agent_observer", "--db", self.db_path,
             "--json", "fleet", "report", "--expect", "rocky,bestia"],
            cwd=REPO, env=env, capture_output=True, text=True)
        # A missing machine is a visible, non-zero result.
        self.assertEqual(report.returncode, 3, report.stderr)
        self.assertEqual(json.loads(report.stdout)["missing"], ["bestia"])

    def test_report_names_a_stored_export_it_skips(self):
        add_session(self.con, "codex", "s-one", 100)
        self.con.commit()
        store = fleet.store_dir(self.db_path)
        os.makedirs(store)
        with open(os.path.join(store, "rocky.json"), "w") as fh:
            json.dump(_doc("rocky", [("codex:other", 1)]), fh)
        env = {**os.environ, "AGENT_OBSERVER_MACHINE": "rocky"}
        out = subprocess.run(
            [sys.executable, "-m", "agent_observer", "--db", self.db_path,
             "--json", "fleet", "report"], cwd=REPO, env=env,
            capture_output=True, text=True)
        self.assertEqual(json.loads(out.stdout)["skipped_stored"], ["rocky"])

    def test_pull_needs_no_ledger_and_report_creates_none(self):
        missing = os.path.join(self.tmp.name, "none", "observer.db")
        out = subprocess.run(
            [sys.executable, "-m", "agent_observer", "--db", missing,
             "fleet", "report"], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)
        self.assertFalse(os.path.exists(missing))
        pull = subprocess.run(
            [sys.executable, "-m", "agent_observer", "--db", missing,
             "fleet", "pull", "nohost.invalid",
             "--remote-command", "true"], cwd=REPO, capture_output=True,
            text=True, timeout=120)
        # The ssh failure is reported per host; no ledger is needed or made.
        self.assertEqual(pull.returncode, 1)
        self.assertIn("nohost.invalid", pull.stdout)
        self.assertFalse(os.path.exists(missing))


# Cross-machine recurrence (#59 review, after #61): each machine judges its
# own interventions; the fleet report combines judge groups without intervention text.
from agent_observer import verdicts  # noqa: E402
from agent_observer.adapters import claude  # noqa: E402
from tests.test_interventions import _line, _write  # noqa: E402

JUDGE = "opus-5.5/correction-v1"
PROBLEM = "Deleted files without asking"


class FleetRecurrenceTest(LedgerCase):
    def machine(self, name, session, judge=JUDGE, group="destructive"):
        """One isolated ledger with one human correction judged yes."""
        root = os.path.join(self.tmp.name, name)
        _write(os.path.join(root, "proj", f"{session}.jsonl"), [
            _line(session, "user", f"{session}-u1", "please add the cache", 0),
            _line(session, "assistant", f"{session}-a1",
                  [{"type": "text", "text": "Done, I deleted build."}], 1),
            _line(session, "user", f"{session}-u2",
                  "no, secret prompt: never delete build", 2)])
        con = db.connect(os.path.join(root, "observer.db"))
        db.init_db(con)
        claude.sync(con, root=root)
        con.commit()
        verdicts.add(con, [
            {"intervention": f"sub:claude:{session}-u2", "behavior": "correction",
             "judge": judge, "verdict": "yes", "problem": PROBLEM,
             "group": group},
            {"intervention": f"sub:claude:{session}-u1", "behavior": "correction",
             "judge": judge, "verdict": "no"}])
        local = verdicts.recurrence(con, "correction")
        self.assertEqual((local["occurrences"], local["recurring_problems"]),
                         (1, 0))
        doc = fleet.export(con, machine=name)
        con.close()
        return json.loads(json.dumps(doc))

    def test_a_problem_recurs_only_across_machines(self):
        rocky = self.machine("rocky", "sess-r")
        bestia = self.machine("bestia", "sess-b")
        text = json.dumps([rocky, bestia])
        self.assertNotIn("secret prompt", text)
        self.assertNotIn("please add the cache", text)
        self.assertIn(PROBLEM, text)  # the judge's line travels
        result = fleet.combine([rocky, bestia])
        [judged] = result["recurrence"]
        self.assertEqual((judged["behavior"], judged["judge"]),
                         ("correction", JUDGE))
        self.assertEqual(judged["machines"], ["bestia", "rocky"])
        self.assertEqual(
            (judged["occurrences"], judged["problems"],
             judged["recurring_problems"], judged["repeats"]), (2, 1, 1, 1))
        [group] = judged["groups"]
        self.assertEqual(group["group"], "destructive")
        self.assertEqual(group["problem"], PROBLEM)
        self.assertEqual(group["by_created_by"], {"user": 2})
        self.assertEqual(group["sessions"], 2)
        self.assertEqual(group["machines"], ["bestia", "rocky"])
        self.assertEqual(judged["coverage"]["judged_by_created_by"]["user"], 4)
        self.assertEqual(result["interventions"]["by_created_by"]["user"], 4)
        self.assertIn("destructive", fleet.render(result))

    def test_a_session_on_two_machines_counts_once(self):
        # One rule for every count: a session reported by several machines
        # contributes its evidence once, so coverage matches recurrence.
        rocky = self.machine("rocky", "sess-x")
        copy = self.machine("bestia", "sess-x")
        alone = fleet.combine([rocky])
        result = fleet.combine([rocky, copy])
        [judged] = result["recurrence"]
        self.assertEqual((judged["occurrences"], judged["recurring_problems"]),
                         (1, 0))
        self.assertEqual(result["interventions"], alone["interventions"])
        self.assertEqual(result["interventions"]["by_created_by"]["user"], 2)
        self.assertEqual(result["interventions"]["by_kind"]["prompt"], 2)
        self.assertEqual(judged["coverage"],
                         alone["recurrence"][0]["coverage"])
        self.assertEqual(judged["coverage"]["verdicts"],
                         {"yes": 1, "no": 1, "unclear": 0})
        self.assertEqual(judged["coverage"]["judged_by_created_by"]["user"], 2)

    def test_different_judges_are_reported_apart_and_flagged(self):
        rocky = self.machine("rocky", "sess-r")
        bestia = self.machine("bestia", "sess-b", judge="sol/correction-v1")
        result = fleet.combine([rocky, bestia])
        self.assertEqual([(j["judge"], j["machines"], j["recurring_problems"])
                          for j in result["recurrence"]],
                         [(JUDGE, ["rocky"], 0),
                          ("sol/correction-v1", ["bestia"], 0)])
        self.assertEqual(result["unmerged_behaviors"],
                         [{"behavior": "correction",
                           "judges": [JUDGE, "sol/correction-v1"]}])

    def test_pulled_judgments_survive_storage_and_are_validated(self):
        doc = self.machine("rocky", "sess-r")
        kept = fleet._schema_only(fleet.validate(doc))
        self.assertEqual(kept["judgments"], doc["judgments"])
        bad = json.loads(json.dumps(doc))
        bad["judgments"][0]["groups"][0]["problem"] = "two\nlines"
        with self.assertRaises(ValueError):
            fleet.validate(bad)
        bad = json.loads(json.dumps(doc))
        bad["judgments"][0]["groups"][0]["interventions"][0]["created_by"] = "robot"
        with self.assertRaises(ValueError):
            fleet.validate(bad)
        for field in ("behavior", "judge"):
            bad = json.loads(json.dumps(doc))
            bad["judgments"][0][field] += "\n"
            with self.assertRaises(ValueError):
                fleet.validate(bad)
        bad = json.loads(json.dumps(doc))
        bad["judgments"][0]["groups"][0]["group"] += "\n"
        with self.assertRaises(ValueError):
            fleet.validate(bad)


def to_schema_1(doc):
    """The same evidence as a schema-2 machine (Observer 0.15.x) exported it."""
    old = json.loads(json.dumps(doc))
    old["schema"] = "agent-observer.fleet-export/1"
    rename = {"user": "human"}

    def counts(parts):
        out = {}
        for part, c in parts.items():
            key = {"by_created_by": "by_origin",
                   "judged_by_created_by": "judged_by_origin"}.get(part, part)
            out[key] = {rename.get(k, k): v for k, v in c.items()}
        return out
    if "interventions" in old:
        old["moments"] = {"sessions": {k: counts(v) for k, v in
                                       old.pop("interventions")["sessions"].items()}}
    for j in old.get("judgments", []):
        j["sessions"] = {k: counts(v) for k, v in j["sessions"].items()}
        for g in j["groups"]:
            g["moments"] = [{"id": p["id"], "session": p["session"],
                             "origin": rename.get(p["created_by"], p["created_by"]),
                             "at": p["at"]} for p in g.pop("interventions")]
        j["ungrouped"] = [{"id": p["id"], "session": p["session"],
                           "origin": rename.get(p["created_by"], p["created_by"]),
                           "at": p["at"]} for p in j["ungrouped"]]
    return old


class SchemaSwitchTest(FleetRecurrenceTest):
    """During the switch some machines still export schema 1 (#65)."""

    def test_exports_carry_the_new_schema(self):
        doc = self.machine("rocky", "sess-r")
        self.assertEqual(doc["schema"], "agent-observer.fleet-export/2")
        self.assertIn("interventions", doc)

    def test_a_schema_1_export_combines_like_its_schema_2_twin(self):
        rocky = self.machine("rocky", "sess-r")
        bestia = self.machine("bestia", "sess-b")
        new = fleet.combine([rocky, bestia])
        mixed = fleet.combine([rocky, to_schema_1(bestia)])
        for key in ("interventions", "recurrence", "unmerged_behaviors"):
            self.assertEqual(mixed[key], new[key], key)
        old = to_schema_1(rocky)
        self.assertNotIn("interventions", json.dumps(old))
        stored = fleet._schema_only(fleet.validate(old))
        self.assertEqual(stored["schema"], "agent-observer.fleet-export/2")
        self.assertEqual(stored["judgments"], rocky["judgments"])
