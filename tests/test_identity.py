"""Instruction identity: direction block, versioned paths, embedded text."""

import hashlib
import json
import unittest

from agent_observer import identity
from tests.helpers import LedgerCase


def block(payload: dict) -> str:
    return ("prefix <<<AGENTSMD_PROJECT_DIRECTION_V1>>>\n" + json.dumps(payload)
            + "\n<<<END_AGENTSMD_PROJECT_DIRECTION_V1>>> suffix")


class ParseTest(unittest.TestCase):
    def test_block_keeps_hashes_and_drops_contents(self):
        parsed = identity.parse_direction_block(block({
            "status": "ready",
            "repository_root": "/repo",
            "git": {"head": {"sha": "abc123"}},
            "files": [{"path": "/repo/VISION.md", "sha256": "v1",
                       "content": "private vision text"}],
            "instructions": {"sha256": "inst-sha", "resolved_target": "/a/AGENTS.md"},
            "preferences": {"sha256": "pref-sha", "status": "ready",
                            "content": "private preference text"},
        }))
        self.assertEqual(parsed["instructions_sha256"], "inst-sha")
        self.assertEqual(parsed["preferences_sha256"], "pref-sha")
        self.assertEqual(parsed["direction_sha256"], {"VISION.md": "v1"})
        self.assertEqual(parsed["git_head"], "abc123")
        self.assertNotIn("private", json.dumps(parsed))

    def test_text_without_block_has_no_identity(self):
        self.assertIsNone(identity.parse_direction_block("plain text"))
        self.assertEqual(identity.parse_direction_block(
            "<<<AGENTSMD_PROJECT_DIRECTION_V1>>> not json"), {"status": "unparsed"})

    def test_version_and_skill_come_only_from_installed_paths(self):
        path = "/u/.claude/plugins/cache/toolboxmd/agentsmd/12.0.1/skills/operations/SKILL.md"
        self.assertEqual(identity.version_from_path(path), "12.0.1")
        self.assertEqual(identity.skill_from_path(path), "operations")
        self.assertIsNone(identity.version_from_path("/dev/agentsmd/AGENTS.md"))
        self.assertIsNone(identity.version_from_path("/other/1.2.3/file"))

    def test_most_read_version_wins(self):
        ident = identity.SessionIdentity()
        for version, times in (("11.4.0", 1), ("12.0.1", 3)):
            for _ in range(times):
                ident.observe_path(f"/p/agentsmd/{version}/skills/operations/x.md")
        self.assertEqual(ident.fields()["agentsmd_version"], "12.0.1")


class EmbeddedTest(LedgerCase):
    def test_release_text_matches_as_prefix_and_text_is_not_kept(self):
        release = b"# Global Agent Rules\n\nrelease body\n"
        sha = hashlib.sha256(release).hexdigest()
        identity.store_version_map(self.con, [{
            "sha256": sha, "size_bytes": len(release), "version": "12.0.1",
            "commit_sha": "c", "released_at": 1.0}])
        ident = identity.SessionIdentity()
        ident.observe_text("# AGENTS.md instructions for /p\n\n<INSTRUCTIONS>\n"
                           + release.decode() + "\nproject rules\n</INSTRUCTIONS>")
        fields = ident.fields(self.con)
        self.assertEqual(fields["instructions_sha256"], sha)
        self.assertNotIn("release body", fields["identity_json"])
        self.assertNotIn("project rules", fields["identity_json"])

    def test_unknown_embedded_text_stays_unknown(self):
        ident = identity.SessionIdentity()
        ident.observe_text("<INSTRUCTIONS>\nunreleased edit\n</INSTRUCTIONS>")
        self.assertNotIn("instructions_sha256", ident.fields(self.con))
