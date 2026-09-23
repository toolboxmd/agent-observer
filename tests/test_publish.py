"""GitHub summary: offline render, owned comments patched, foreign left alone."""

from unittest import mock

from agent_observer import db, publish
from tests.helpers import LedgerCase

ME = "observer-bot"
FOREIGN = "spoof-account"


def comment(cid, body, author, created="2026-09-20T10:00:00Z"):
    return {"id": cid, "body": body, "html_url": f"u{cid}",
            "user": {"login": author}, "created_at": created,
            "updated_at": created}


class PublishGh:
    """Fake gh api: answers the login lookup, records PATCH/POST calls.

    Comment listings paginate at 100 per page like the real API: the
    page query selects the slice, so a marker beyond the first page is
    only found when the caller paginates.
    """

    def __init__(self, comments, login=ME):
        self.comments = comments
        self.login = login
        self.patched = []
        self.posted = []
        self.next_id = max([c["id"] for c in comments] + [100]) + 1
        self.list_calls = []

    def __call__(self, args, payload=None):
        if args == ["user"]:
            return {"login": self.login}
        if "per_page=100" in args[0]:
            self.list_calls.append(args[0])
            url = args[0]
            page = 1
            for chunk in url.replace("?", "&").split("&"):
                if chunk.startswith("page="):
                    try:
                        page = int(chunk.split("=", 1)[1])
                    except ValueError:
                        page = 1
            start = (page - 1) * 100
            return list(self.comments[start:start + 100])
        if args[:2] == ["-X", "POST"]:
            new = comment(self.next_id, payload["body"], self.login)
            self.next_id += 1
            self.comments.append(new)
            self.posted.append(new)
            return new
        if args[:2] == ["-X", "PATCH"]:
            cid = int(args[2].rstrip("/").rsplit("/", 1)[-1])
            target = next(c for c in self.comments if c["id"] == cid)
            target["body"] = payload["body"]
            self.patched.append(cid)
            return target
        raise AssertionError(args)


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

    def test_render_marks_unknown_counters_honestly(self):
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness, session_key,"
            " model, total_tokens, semantics) VALUES('claude:m2', 1, 'claude',"
            " 'claude:s1', NULL, NULL, 'claude:x')")
        self.con.commit()
        body = publish.render(publish.summarize(self.con, {"claude:s1"}, "task T"))
        self.assertIn("unknown", body)
        self.assertIn("lower bound", body)

    def test_second_publish_edits_the_same_owned_comment(self):
        gh = PublishGh([])
        with mock.patch.object(publish, "_gh", gh):
            first = publish.post("o/r", publish.MARKER + " one", pr=5)
            second = publish.post("o/r", publish.MARKER + " two", pr=5)
        self.assertEqual((first["action"], second["action"]), ("created", "updated"))
        self.assertEqual(len(gh.comments), 1)
        self.assertEqual(gh.patched, [gh.comments[0]["id"]])
        self.assertTrue(gh.comments[0]["body"].endswith("two"))

    def test_foreign_marker_is_never_patched(self):
        gh = PublishGh([comment(9, publish.MARKER + " spoofed", FOREIGN)])
        with mock.patch.object(publish, "_gh", gh):
            result = publish.post("o/r", publish.MARKER + " mine", pr=5)
        self.assertEqual(result["action"], "created")
        self.assertEqual(gh.patched, [])
        self.assertEqual(len(gh.posted), 1)
        self.assertEqual(result["foreign_markers_left_alone"], [9])
        spoofed = next(c for c in gh.comments if c["id"] == 9)
        self.assertIn("spoofed", spoofed["body"])

    def test_duplicate_owned_markers_update_newest_and_report(self):
        gh = PublishGh([
            comment(11, publish.MARKER + " old", ME, "2026-09-20T10:00:00Z"),
            comment(12, publish.MARKER + " newer", ME, "2026-09-20T11:00:00Z"),
        ])
        with mock.patch.object(publish, "_gh", gh):
            result = publish.post("o/r", publish.MARKER + " fresh", pr=5)
        self.assertEqual(result["action"], "updated")
        self.assertEqual(result["id"], 12)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["duplicate_ids"], [11])
        self.assertEqual(gh.patched, [12])

    def test_commit_comments_check_authorship_too(self):
        gh = PublishGh([comment(21, publish.MARKER + " spoofed", FOREIGN)])
        with mock.patch.object(publish, "_gh", gh):
            result = publish.post("o/r", publish.MARKER + " mine", commit="abc123")
        self.assertEqual(result["action"], "created")
        self.assertEqual(gh.patched, [])

    def test_marker_beyond_first_page_is_updated_not_duplicated(self):
        fillers = [comment(i, f"filler {i}", "someone-else",
                           f"2026-09-19T10:{i % 60:02d}:00Z")
                   for i in range(1, 101)]
        owned = comment(1001, publish.MARKER + " old", ME,
                        "2026-09-20T12:00:00Z")
        foreign = comment(1002, publish.MARKER + " spoofed", FOREIGN,
                          "2026-09-20T12:30:00Z")
        gh = PublishGh(fillers + [owned, foreign])
        with mock.patch.object(publish, "_gh", gh):
            result = publish.post("o/r", publish.MARKER + " fresh", pr=5)
        self.assertEqual(result["action"], "updated")
        self.assertEqual(result["id"], 1001)
        self.assertEqual(gh.patched, [1001])
        self.assertEqual(len(gh.posted), 0)
        self.assertTrue(any("page=2" in call for call in gh.list_calls),
                        "expected pagination past the first page")
        spoofed = next(c for c in gh.comments if c["id"] == 1002)
        self.assertIn("spoofed", spoofed["body"])

    def test_pagination_has_no_page_cap(self):
        # 101 full pages of fillers: an owned marker past the old
        # page-100 cutoff is still found and updated, not duplicated.
        fillers = [comment(i, f"filler {i}", "someone-else")
                   for i in range(1, 101 * 100 + 1)]
        owned = comment(20001, publish.MARKER + " old", ME,
                        "2026-09-20T12:00:00Z")
        gh = PublishGh(fillers + [owned])
        with mock.patch.object(publish, "_gh", gh):
            result = publish.post("o/r", publish.MARKER + " fresh", pr=5)
        self.assertEqual(result["action"], "updated")
        self.assertEqual(result["id"], 20001)
        self.assertEqual(gh.patched, [20001])
        self.assertEqual(len(gh.posted), 0)

    def test_shared_unknown_total_with_unknown_count_renders(self):
        summary = publish.summarize(self.con, {"claude:s1"}, "task T")
        summary["shared_tokens"] = None
        summary["shared_tokens_unknown"] = 2
        body = publish.render(summary)
        self.assertIn("Shared with other tasks and not divided", body)
        self.assertIn("unknown", body)

    def test_target_must_be_exactly_one(self):
        with self.assertRaises(ValueError):
            publish.post("o/r", "b")
        with self.assertRaises(ValueError):
            publish.post("o/r", "b", pr=1, commit="abc")
