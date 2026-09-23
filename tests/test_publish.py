"""GitHub summary: offline render, one Observer comment created then updated."""

from unittest import mock

from agent_observer import db, publish
from tests.helpers import LedgerCase


class PublishTest(LedgerCase):
    def setUp(self):
        super().setUp()
        db.upsert_session(self.con, "claude:s1", "claude", "s1", None,
                          started_at=100.0, ended_at=160.0, agentsmd_version="12.1.0")
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at) VALUES('claude','p','x',0)")
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness, session_key, model,"
            " total_tokens, semantics) VALUES('claude:m1', 1, 'claude', 'claude:s1',"
            " 'claude-fable-5-1', 1234, 'claude:x')")
        self.con.commit()

    def test_render_holds_aggregates_only(self):
        body = publish.render(publish.summarize(self.con, {"claude:s1"}, "task T"))
        self.assertTrue(body.startswith(publish.MARKER))
        self.assertIn("1 session on claude", body)
        self.assertIn("| claude | claude-fable-5-1 | unknown | 1 | 1,234 |", body)
        self.assertIn("AgentsMD 12.1.0", body)

    def test_second_publish_edits_the_same_comment(self):
        comments = []

        def fake_gh(args, payload=None):
            if args[0].endswith("per_page=100"):
                return list(comments)
            if args[:2] == ["-X", "POST"]:
                comments.append({"id": 7, "body": payload["body"], "html_url": "u7"})
                return comments[-1]
            if args[:2] == ["-X", "PATCH"]:
                self.assertTrue(args[2].endswith("/comments/7"))
                comments[0]["body"] = payload["body"]
                return comments[0]
            raise AssertionError(args)

        with mock.patch.object(publish, "_gh", fake_gh):
            first = publish.post("o/r", publish.MARKER + " one", pr=5)
            second = publish.post("o/r", publish.MARKER + " two", pr=5)
        self.assertEqual((first["action"], second["action"]), ("created", "updated"))
        self.assertEqual(len(comments), 1)
        self.assertTrue(comments[0]["body"].endswith("two"))

    def test_target_must_be_exactly_one(self):
        with self.assertRaises(ValueError):
            publish.post("o/r", "b")
        with self.assertRaises(ValueError):
            publish.post("o/r", "b", pr=1, commit="abc")
