"""Issue #36: full tool-call arguments with secrets redacted.

Tool calls keep their full path or command in events.target (privacy rule
8) so an audit can list what a session read before it edited a file.
"""

import json
import os
import tempfile
import time
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
        raw = ("echo '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n"
               "-----END OPENSSH PRIVATE KEY-----' | ssh-add -")
        self.assertEqual(privacy.argument_text(raw), "echo '[redacted]' | ssh-add -")

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

    def test_file_bodies_written_by_commands_are_omitted(self):
        body = "FILE-BODY-must-not-persist"
        cases = {
            f"cat > /tmp/c.py <<'EOF'\n{body}\nEOF\npython3 /tmp/c.py":
                "cat > /tmp/c.py <<'EOF'\n[content omitted]\nEOF\npython3 /tmp/c.py",
            f"cat <<EOF | tee -a /r/n.md\n{body}\nEOF\n":
                "cat <<EOF | tee -a /r/n.md\n[content omitted]\nEOF\n",
            f"cat > /r/a <<A\n{body}\nA\ncat > /r/b <<-B\n\t{body}\n\tB\nls":
                "cat > /r/a <<A\n[content omitted]\nA\n"
                "cat > /r/b <<-B\n[content omitted]\n\tB\nls",
            f"printf '%s' '{body}' > /r/f && ls":
                "printf [content omitted] > /r/f && ls",
            f"echo {body} >> /r/notes.md; cat /r/notes.md":
                "echo [content omitted] >> /r/notes.md; cat /r/notes.md",
            f'tools.exec_command({{cmd:"cat > /r/x.md <<\'EOF\'\\n{body}\\nEOF\\ngit add /r/x.md"}})':
                'tools.exec_command({cmd:"cat > /r/x.md <<\'EOF\'\\n[content omitted]'
                '\\nEOF\\ngit add /r/x.md"})',
            f"cat > /r/open <<EOF\n{body} never closed":
                "cat > /r/open <<EOF\n[content omitted]",
            f"cat <<A <<B > /r/out\nfirst\nA\n{body}\nB\nls":
                "cat <<A <<B > /r/out\n[content omitted]\nA\n[content omitted]\nB\nls",
            f"printf 'first line\n{body}' > /r/out\nls":
                "printf [content omitted] > /r/out\nls",
            f"echo a; printf '{body}' >> /r/f; echo b":
                "echo a; printf [content omitted] >> /r/f; echo b",
            f"echo {body} &>/r/out": "echo [content omitted] &>/r/out",
            f"echo {body} &>> /r/out; ls": "echo [content omitted] &>> /r/out; ls",
            f"printf '{body}' 1>/r/out": "printf [content omitted] 1>/r/out",
            f"echo {body} | tee /r/out": "echo [content omitted] | tee /r/out",
            f"printf '{body}' | tee -a /r/out; ls":
                "printf [content omitted] | tee -a /r/out; ls",
            f"echo {body}2>/r/out": "echo [content omitted] >/r/out",
            f"echo {body} | sudo tee /r/f": "echo [content omitted] | sudo tee /r/f",
            f"printf {body} | /usr/bin/tee /r/f":
                "printf [content omitted] | /usr/bin/tee /r/f",
            f"echo {body} | cat | tee /r/f": "echo [content omitted] | cat | tee /r/f",
            f"echo {body} 2>&1 | tee /r/f": "echo [content omitted] 2>&1 | tee /r/f",
            f"echo {body} 2>&1 > /r/f": "echo [content omitted] 2>&1 > /r/f",
            f"printf {body} >&/r/f": "printf [content omitted] >&/r/f",
            f"tee /r/f <<<{body}": "tee /r/f <<<[content omitted]",
            f"tee /r/f <<< '{body} two'": "tee /r/f <<< [content omitted]",
            f"cat <<<{body} > /r/f": "cat <<<[content omitted] > /r/f",
            f"cat <<E &>/r/f\n{body}\nE\n": "cat <<E &>/r/f\n[content omitted]\nE\n",
            f"cat <<E 1>/r/f\n{body}\nE\n": "cat <<E 1>/r/f\n[content omitted]\nE\n",
        }
        for raw, want in cases.items():
            with self.subTest(raw[:30]):
                self.assertEqual(privacy.argument_text(raw), want)

    def test_programs_fed_by_heredoc_stay_as_the_command(self):
        for raw in ("python3 - <<'EOF'\nprint(open('/r/a').read())\nEOF",
                    'git commit -F - <<EOF\nfix: message\nEOF',
                    "echo done 2>&1 | tail -1",
                    "echo failed >&2; exit 1",
                    "python3 - <<E 2>&1\nprint(1)\nE\n",
                    "echo a && ls",
                    "echo a | grep a",
                    "echo warn 2>/dev/null",
                    "echo x 2>&1 | tail -1",
                    "grep x <<<needle",
                    "echo a | tee-log x"):
            self.assertEqual(privacy.argument_text(raw), raw)

    def test_commands_after_a_heredoc_survive(self):
        # Issue #39, Codex: a python3 heredoc inside one exec string, with a
        # '>' in its program, then a second exec call. Before the fix the
        # whole line counted as the heredoc's introducing line, so the
        # second command became [content omitted].
        program = ('await tools.exec_command({cmd: "python3 - <<\'PY\'\\n'
                   'import json\\nprint(\'<summary>\' + str(1) + \'</summary>\')'
                   '\\nPY\\n", workdir: "/w"});')
        issue = ('await tools.exec_command({cmd: "gh issue create --repo '
                 'example/repo --title T --body-file /tmp/b.md"});')
        # Issue #39, Claude: an interpreter heredoc whose program holds a
        # '>' and a string literal with escaped newlines.
        claude = ("python3 - <<'EOF'\nimport sys\nif len(sys.argv) > 1:\n"
                  "    pass\nSTUB = \"#!/bin/sh\\nexit 0\\n\"\nprint(STUB)\n"
                  "EOF\ngh issue create --title T")
        for raw in (program + "\n" + issue, program + " " + issue, claude,
                    "const f = (x) => x;\n" + program + "\n" + issue):
            with self.subTest(raw[:40]):
                self.assertEqual(privacy.argument_text(raw), raw)

    def test_file_bodies_stay_omitted_around_kept_commands(self):
        body = "FILE-BODY-must-not-persist"
        cases = {
            # An escaped write in one exec call, then another call.
            f'tools.exec_command({{cmd:"cat > /r/x <<\'EOF\'\\n{body}\\nEOF\\n"}});'
            ' tools.exec_command({cmd:"gh pr list"});':
                'tools.exec_command({cmd:"cat > /r/x <<\'EOF\'\\n[content omitted]'
                '\\nEOF\\n"}); tools.exec_command({cmd:"gh pr list"});',
            # A kept interpreter program whose string holds a shell stub
            # that writes a file through a heredoc.
            f"python3 - <<'EOF'\nS = \"cat > /r/f <<'X'\\n{body}\\nX\\nexit 0\\n\"\n"
            "EOF\ngh issue create --title T":
                "python3 - <<'EOF'\nS = \"cat > /r/f <<'X'\\n[content omitted]"
                "\\nX\\nexit 0\\n\"\nEOF\ngh issue create --title T",
            # A real-newline write whose body or introducing line holds an
            # escaped newline.
            f"cat > /r/f <<EOF; printf 'x\\n'\n{body}\nEOF\ngh pr list":
                "cat > /r/f <<EOF; printf 'x\\n'\n[content omitted]\nEOF\ngh pr list",
            f"cat > /r/f <<EOF\n{body} with \\n inside\nEOF\nls":
                "cat > /r/f <<EOF\n[content omitted]\nEOF\nls",
            # An unclosed escaped write still fails closed to the end.
            f'tools.exec_command({{cmd:"cat > /r/o <<EOF\\n{body}"}});\nls':
                'tools.exec_command({cmd:"cat > /r/o <<EOF\\n[content omitted]',
        }
        for raw, want in cases.items():
            with self.subTest(raw[:40]):
                self.assertEqual(privacy.argument_text(raw), want)

    def test_long_commands_are_stripped_in_linear_time(self):
        for raw in (("echo x " * 8000) + "z", "printf '" + "a" * 60000,
                    "cat > /r/f <<A\n" * 5000, "<<EOF\n" * 10000,
                    "echo x 2>&1 | " * 5000, "tee <<<a " * 8000,
                    "<<<a " * 12000, "cat > f <<A\\n x\n" * 5000,
                    "<<A\n<<B\\n" * 8000):
            start = time.monotonic()
            privacy.argument_text(raw)
            self.assertLess(time.monotonic() - start, 1.0, raw[:20])

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
            _codex_line(4, {"type": "function_call", "name": "send_message",
                            "call_id": "c-raw",
                            "arguments": "private raw argument text"}),
            _codex_line(5, {"type": "function_call", "name": "send_message",
                            "call_id": "c-str",
                            "arguments": json.dumps("private json string")}),
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
        self.assertIsNone(rows["c-raw"])
        self.assertIsNone(rows["c-str"])
        blob = self.blob()
        self.assertNotIn(SECRETS["github classic"], blob)
        self.assertNotIn(PATCH_BODY, blob)
        self.assertNotIn("private brief text", blob)
        self.assertNotIn("private raw argument text", blob)
        self.assertNotIn("private json string", blob)

    def test_claude_long_command_is_full_and_redacted(self):
        command = (f"export OPENAI_API_KEY={SECRETS['openai']}; "
                   f"cat /r/docs/guide.md {LONG_TAIL}")
        write = f"cat > /r/new.md <<'EOF'\n{PATCH_BODY}\nEOF"
        path = self.write("claude-args.jsonl", [
            _claude_line("a1", 1, [{"type": "tool_use", "id": "t-read",
                                    "name": "Read",
                                    "input": {"file_path": "/r/AGENTS.md"}}]),
            _claude_line("a2", 2, [{"type": "tool_use", "id": "t-bash",
                                    "name": "Bash",
                                    "input": {"command": command}}]),
            _claude_line("a4", 3, [{"type": "tool_use", "id": "t-write",
                                    "name": "Bash",
                                    "input": {"command": write}}]),
            _claude_line("a3", 4, [{"type": "tool_use", "id": "t-edit",
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
        self.assertEqual(before[2], (
            "Bash", "cat > /r/new.md <<'EOF'\n[content omitted]\nEOF"))
        self.assertEqual(len(before), 3)
        blob = self.blob()
        self.assertNotIn(SECRETS["openai"], blob)
        self.assertNotIn(PATCH_BODY, blob)


if __name__ == "__main__":
    unittest.main()
