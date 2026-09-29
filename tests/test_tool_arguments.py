"""Issue #36: full tool-call arguments with secrets redacted.

Tool calls keep their full path or command in events.target (privacy rule
8) so an audit can list what a session read before it edited a file.
"""

import json
import os
import tempfile
import unittest

from agent_observer import privacy
from agent_observer.adapters import claude, grok
from agent_observer.adapters.codex import import_codex_file
from tests.helpers import LedgerCase

# Synthetic values in the shape of real credentials; none is live. Each
# prefix is joined at runtime so no credential-shaped literal sits in source
# for push protection to flag.
SECRETS = {
    "github classic": "ghp" + "_Zq7Kx2Lm9Np4Rs6Tu8Vw0Xy1Ab3Cd5Ef7Gh",
    "github fine-grained": "github" + "_pat_11ABCDEFG0123456789_abcdefghijklmnop",
    "openai": "sk" + "-proj-Q1w2E3r4T5y6U7i8O9p0",
    "anthropic": "sk" + "-ant-api03-Aa1Bb2Cc3Dd4Ee5Ff6Gg7",
    "aws": "AKIA" + "QWERTYUIOPASDFGH",
    "slack": "xoxb" + "-1234567890-abcdefghij",
    "google": "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
    "gitlab": "glpat" + "-a1B2c3D4e5F6g7H8i9J0",
    "jwt": "eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
}
PASSWORDS = ("hunter2-env-value", "hunter2-flag-value", "hunter2-url-value",
             "hunter2-json-value", "hunter2-bearer-value-0123")
PATCH_BODY = "PATCH-BODY-CONTENT-must-not-persist"


class RedactionUnitTest(unittest.TestCase):
    def test_known_secret_shapes_are_redacted(self):
        for label, secret in SECRETS.items():
            with self.subTest(label):
                kept = privacy.argument_text(f"curl -d {secret} https://x")
                self.assertNotIn(secret, kept)
                self.assertIn(privacy.REDACTED, kept)

    def test_assignments_flags_urls_and_headers_keep_only_the_name(self):
        cases = {
            "export DEPLOY_TOKEN=hunter2-env-value && make":
                "export DEPLOY_TOKEN=[redacted] && make",
            "psql --password hunter2-flag-value -U app":
                "psql --password [redacted] -U app",
            "git push https://bot:hunter2-url-value@github.com/o/r":
                "git push https://bot:[redacted]@github.com/o/r",
            '{"api_key": "hunter2-json-value", "path": "/a"}':
                '{"api_key": [redacted], "path": "/a"}',
            "curl -H 'Authorization: Bearer hunter2-bearer-value-0123'":
                "curl -H 'Authorization: Bearer [redacted]'",
        }
        for raw, want in cases.items():
            self.assertEqual(privacy.argument_text(raw), want)

    def test_private_key_block_is_redacted(self):
        raw = ("printf '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n"
               "-----END OPENSSH PRIVATE KEY-----' > k")
        self.assertEqual(privacy.argument_text(raw), "printf '[redacted]' > k")

    def test_ordinary_commands_are_unchanged(self):
        for raw in ('await tools.exec_command({cmd:"cat a",max_output_tokens:5000})',
                    "gh auth token --hostname github.com | wc -c",
                    "git log --author=someone --oneline",
                    "sed -n '1,40p' /repo/skills/operations/SKILL.md"):
            self.assertEqual(privacy.argument_text(raw), raw)

    def test_patch_bodies_keep_only_file_headers(self):
        real = ("apply_patch <<'EOF'\n*** Begin Patch\n*** Update File: /r/a.py\n"
                f"@@\n-{PATCH_BODY}\n+{PATCH_BODY}\n*** Add File: /r/b.md\n"
                f"+{PATCH_BODY}\n*** End Patch\nEOF")
        self.assertEqual(
            privacy.argument_text(real),
            "apply_patch <<'EOF'\n*** Begin Patch\n*** Update File: /r/a.py\n"
            "*** Add File: /r/b.md\n*** End Patch\nEOF")
        escaped = ('await tools.apply_patch("*** Begin Patch\\n*** Update File: /r/a.py'
                   f'\\n@@\\n+{PATCH_BODY}\\n*** End Patch");')
        self.assertEqual(
            privacy.argument_text(escaped),
            'await tools.apply_patch("*** Begin Patch\\n*** Update File: /r/a.py'
            '\\n*** End Patch");')

    def test_grok_paths_containing_sk_dash_survive(self):
        path = "/w/concepts/issue-led-risk-tiered-vertical-slice-workflow.md"
        self.assertEqual(grok._safe_target({"path": path}), path)
        self.assertEqual(grok._safe_target({"command": "sk-fake-secret-12345"}),
                         privacy.REDACTED)

    def test_argument_cap_exceeds_real_commands(self):
        long_cmd = "echo " + "x" * 60000
        self.assertEqual(privacy.argument_text(long_cmd), long_cmd)
        self.assertEqual(len(privacy.argument_text("y" * 70000)),
                         privacy.ARGUMENT_CHARS)


def _codex_line(ordinal, payload, rtype="response_item"):
    return json.dumps({"ordinal": ordinal, "type": rtype, "payload": payload,
                       "timestamp": f"2026-09-29T10:00:{ordinal:02d}.000Z"})


def _claude_line(uuid, second, content):
    return json.dumps({
        "sessionId": "sess-args", "cwd": "/r", "version": "2.1.280",
        "isSidechain": False, "type": "assistant", "uuid": uuid,
        "timestamp": f"2026-09-29T10:00:{second:02d}Z",
        "message": {"id": f"msg-{uuid}", "role": "assistant",
                    "model": "claude-fixture", "content": content}})


LONG_TAIL = " && ".join(f"grep -n pattern{i} /r/src/module_{i}.py"
                        for i in range(40))

FILES_READ_BEFORE_FIRST_EDIT = """
SELECT e.ordinal_num, e.name, e.target FROM events e
WHERE e.session_key = ? AND e.family = 'tool_call'
  AND e.ordinal_num < (SELECT MIN(ordinal_num) FROM events
                       WHERE session_key = e.session_key
                         AND family = 'file_change')
ORDER BY e.ordinal_num
"""


class FullArgumentLedgerTest(LedgerCase):
    def write(self, name, lines):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def blob(self):
        rows = self.query("SELECT target, detail_json FROM events")
        return "\n".join(f"{r['target']}\n{r['detail_json']}" for r in rows)

    def test_codex_tool_calls_keep_full_commands(self):
        program = ('const r = await tools.exec_command({cmd:"cat /r/AGENTS.md && '
                   f'GH_TOKEN={SECRETS["github classic"]} gh pr list {LONG_TAIL}",'
                   'workdir:"/r"});\ntext(r.output);\n'
                   'await tools.apply_patch("*** Begin Patch\\n*** Update File: /r/x.py'
                   f'\\n@@\\n+{PATCH_BODY}\\n*** End Patch");')
        path = self.write("codex-args.jsonl", [
            _codex_line(0, {"session_id": "sess-args", "id": "sess-args",
                            "cwd": "/r", "thread_source": "user"},
                        "session_meta"),
            _codex_line(1, {"type": "custom_tool_call", "name": "exec",
                            "call_id": "c-exec", "input": program}),
            _codex_line(2, {"type": "function_call", "name": "exec_command",
                            "call_id": "c-fn", "arguments": json.dumps(
                                {"cmd": ["bash", "-lc", "sed -n 1,9p /r/README.md"],
                                 "workdir": "/r"})}),
            _codex_line(3, {"type": "function_call", "name": "spawn_agent",
                            "call_id": "c-spawn", "arguments": json.dumps(
                                {"message": "private brief text"})}),
        ])
        import_codex_file(self.con, path)
        rows = {r["native_id"]: r["target"] for r in self.query(
            "SELECT native_id, target FROM events WHERE family='tool_call'")}
        self.assertIn(LONG_TAIL, rows["c-exec"])
        self.assertIn("cat /r/AGENTS.md", rows["c-exec"])
        self.assertIn("*** Update File: /r/x.py", rows["c-exec"])
        self.assertGreater(len(rows["c-exec"]), 500)
        self.assertEqual(rows["c-fn"], "bash -lc sed -n 1,9p /r/README.md")
        self.assertIsNone(rows["c-spawn"])
        blob = self.blob()
        self.assertNotIn(SECRETS["github classic"], blob)
        self.assertNotIn(PATCH_BODY, blob)
        self.assertNotIn("private brief text", blob)

    def test_claude_long_command_is_full_and_redacted(self):
        command = (f"export OPENAI_API_KEY={SECRETS['openai']}; "
                   f"cat /r/docs/guide.md {LONG_TAIL}")
        path = self.write("claude-args.jsonl", [
            _claude_line("a1", 1, [{"type": "tool_use", "id": "t-read",
                                    "name": "Read",
                                    "input": {"file_path": "/r/AGENTS.md"}}]),
            _claude_line("a2", 2, [{"type": "tool_use", "id": "t-bash",
                                    "name": "Bash",
                                    "input": {"command": command}}]),
            _claude_line("a3", 3, [{"type": "tool_use", "id": "t-edit",
                                    "name": "Edit",
                                    "input": {"file_path": "/r/app.py",
                                              "old_string": PATCH_BODY,
                                              "new_string": PATCH_BODY}}]),
        ])
        claude.import_claude_file(self.con, path)
        session = self.query("SELECT DISTINCT session_key FROM events")[0][0]
        before = [(r["name"], r["target"]) for r in self.query(
            FILES_READ_BEFORE_FIRST_EDIT, (session,))]
        self.assertEqual(before[0], ("Read", "/r/AGENTS.md"))
        self.assertEqual(before[1][0], "Bash")
        self.assertEqual(before[1][1], command.replace(
            SECRETS["openai"], privacy.REDACTED))
        self.assertEqual(len(before), 2)
        blob = self.blob()
        self.assertNotIn(SECRETS["openai"], blob)
        self.assertNotIn(PATCH_BODY, blob)


if __name__ == "__main__":
    unittest.main()
