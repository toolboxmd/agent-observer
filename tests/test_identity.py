"""Instruction identity: direction block, versioned paths, embedded text."""

import hashlib
import json
import unittest

from agent_observer import identity
from tests.helpers import LedgerCase


def block(payload: dict) -> str:
    return ("prefix <<<AGENTSMD_PROJECT_DIRECTION_V1>>>\n" + json.dumps(payload)
            + "\n<<<END_AGENTSMD_PROJECT_DIRECTION_V1>>> suffix")


INST = "cda5d15cf67856d4674b05324a77faf7043caca2f8016312644aa8895106e5e4"
PREF = "77cc39dd5fb2a2c9df54d9286cb58123b8d251e640b7278029d101cdd29888c0"
VISION = "010eb056bda2c80910656784c27f52a00a189ed658b0bec4eb1f8151debd3da7"
HEAD = "89905efc43cfff0b7187ded9af4a652a0a41a6ec"


class ParseTest(unittest.TestCase):
    def test_block_keeps_valid_hashes_paths_and_statuses(self):
        parsed = identity.parse_direction_block(block({
            "status": "ready",
            "repository_root": "/repo",
            "git": {"head": {"sha": HEAD}},
            "files": [{"path": "/repo/VISION.md", "sha256": VISION,
                       "content": "private vision text"}],
            "instructions": {"sha256": INST, "resolved_target": "/a/AGENTS.md"},
            "preferences": {"sha256": PREF, "status": "ready",
                            "content": "private preference text"},
        }))
        self.assertEqual(parsed["instructions_sha256"], INST)
        self.assertEqual(parsed["instructions_path"], "/a/AGENTS.md")
        self.assertEqual(parsed["preferences_sha256"], PREF)
        self.assertEqual(parsed["preferences_status"], "ready")
        self.assertEqual(parsed["direction_sha256"], {"VISION.md": VISION})
        self.assertEqual(parsed["repository_root"], "/repo")
        self.assertEqual(parsed["git_head"], HEAD)
        self.assertEqual(parsed["status"], "ready")
        self.assertNotIn("private", json.dumps(parsed))

    def test_invalid_statuses_hashes_paths_heads_and_names_are_dropped(self):
        parsed = identity.parse_direction_block(block({
            "status": "definitely-ready",
            "repository_root": "relative/repo",
            "git": {"head": {"sha": "abc123"}},
            "files": [{"path": "/repo/EVIL.md", "sha256": VISION},
                      {"path": "/repo/VISION.md", "sha256": "v1"},
                      {"path": "/repo/MISSION.md", "sha256": VISION}],
            "instructions": {"sha256": "inst-sha",
                             "resolved_target": "relative/AGENTS.md"},
            "preferences": {"sha256": "pref-sha", "status": "super-ready"},
        }))
        self.assertNotIn("status", parsed)
        self.assertNotIn("instructions_sha256", parsed)
        self.assertNotIn("instructions_path", parsed)
        self.assertNotIn("preferences_sha256", parsed)
        self.assertNotIn("preferences_status", parsed)
        self.assertNotIn("repository_root", parsed)
        self.assertNotIn("git_head", parsed)
        # Only the approved direction file with a valid hash survives.
        self.assertEqual(parsed.get("direction_sha256"),
                         {"MISSION.md": VISION})

    def test_wrong_types_and_malformed_values_are_dropped(self):
        parsed = identity.parse_direction_block(block({
            "status": "ready",
            "repository_root": "/repo\x00injected",
            "git": {"head": {"sha": HEAD.upper()}},
            "files": "not-a-list",
            "instructions": {"sha256": 42, "resolved_target": None},
            "preferences": {"sha256": ["x"], "status": None},
        }))
        self.assertEqual(parsed.get("status"), "ready")
        for key in ("instructions_sha256", "instructions_path",
                    "preferences_sha256", "preferences_status",
                    "direction_sha256", "repository_root", "git_head"):
            self.assertNotIn(key, parsed)

    def test_marker_shaped_paths_and_text_never_persist(self):
        evil = "SECRET-MARKER-aaa111 <<<AGENTSMD_PROJECT_DIRECTION_V1>>>"
        parsed = identity.parse_direction_block(block({
            "status": "ready",
            "repository_root": "/repo/<<<injected",
            "git": {"head": {"sha": HEAD}},
            "files": [{"path": "/repo/VISION.md", "sha256": VISION}],
            "instructions": {"sha256": INST,
                             "resolved_target": "/a/<b>AGENTS.md"},
            "preferences": {"sha256": PREF, "status": "ready"},
        }))
        self.assertNotIn("repository_root", parsed)
        self.assertNotIn("instructions_path", parsed)
        blob = json.dumps(parsed)
        self.assertNotIn("SECRET-MARKER", blob)
        self.assertNotIn("AGENTSMD_PROJECT_DIRECTION_V1", blob)
        self.assertNotIn("content", blob)
        self.assertIn(VISION, blob)

    def test_identity_json_holds_only_sanitized_allowlisted_fields(self):
        ident = identity.SessionIdentity()
        ident.observe_text(block({
            "status": "ready",
            "repository_root": "/repo",
            "git": {"head": {"sha": HEAD}},
            "files": [{"path": "/repo/VISION.md", "sha256": VISION,
                       "content": "SECRET-VISION-CONTENT-aaa111"}],
            "instructions": {"sha256": INST, "resolved_target": "/a/AGENTS.md",
                             "content": "SECRET-INSTR-CONTENT-bbb222"},
            "preferences": {"sha256": PREF, "status": "ready",
                            "content": "SECRET-PREF-CONTENT-ccc333"},
            "title": "SECRET-TITLE-ddd444",
            "arbitrary": {"free": "SECRET-FREE-eee555"},
        }))
        fields = ident.fields()
        payload = json.loads(fields["identity_json"])
        allowed = {"direction_status", "instructions_sha256",
                   "preferences_sha256", "preferences_status",
                   "instructions_path", "direction_sha256",
                   "repository_root", "git_head",
                   "loaded_instructions_match", "embedded_match",
                   "plugin_paths"}
        self.assertTrue(set(payload) <= allowed, set(payload) - allowed)
        blob = fields["identity_json"]
        for sentinel in ("SECRET-VISION-CONTENT", "SECRET-INSTR-CONTENT",
                         "SECRET-PREF-CONTENT", "SECRET-TITLE",
                         "SECRET-FREE", "AGENTSMD_PROJECT_DIRECTION",
                         "direction_block", "arbitrary", "content"):
            self.assertNotIn(sentinel, blob)
        self.assertEqual(payload["direction_status"], "ready")
        self.assertEqual(payload["instructions_sha256"], INST)

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
