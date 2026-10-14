"""publish's gh call must return JSON, or name the command and its stderr.

A delegated worker sets CLICOLOR_FORCE. gh then colorizes `gh api` JSON
even on a pipe, and json.loads reports Expecting value. These tests use a
fake gh on PATH and never touch GitHub or the ledger.
"""

import os
import stat
import tempfile
import unittest

from agent_observer import publish


_FAKE = """#!/usr/bin/env python3
import os, sys
mode = os.environ.get("AO_GH_MODE", "")
if mode == "empty":
    sys.stderr.write("keyring locked\\n")
    raise SystemExit(0)
if mode == "text":
    sys.stdout.write("not-json\\n")
    sys.stderr.write("color forced\\n")
    raise SystemExit(0)
if mode == "color":
    force = os.environ.get("CLICOLOR_FORCE", "")
    if force and force != "0":
        sys.stdout.write("\\x1b[1;37m{\\x1b[m\\n")
    else:
        sys.stdout.write('{"login":"octocat"}\\n')
    raise SystemExit(0)
sys.stderr.write("unknown mode\\n")
raise SystemExit(2)
"""


class PublishGhTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bin_dir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bin_dir)
        path = os.path.join(bin_dir, "gh")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_FAKE)
        os.chmod(path, stat.S_IRWXU)
        self._saved = {
            key: os.environ.get(key)
            for key in ("PATH", "CLICOLOR_FORCE", "AO_GH_MODE")}
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        os.environ["CLICOLOR_FORCE"] = "1"

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _error(self, mode):
        os.environ["AO_GH_MODE"] = mode
        with self.assertRaises(RuntimeError) as caught:
            publish._gh(["user"])
        message = str(caught.exception)
        self.assertIn("gh api user", message)
        self.assertNotIn("Expecting value", message)
        return message

    def test_colored_json_parses_when_color_is_forced(self):
        os.environ["AO_GH_MODE"] = "color"
        self.assertEqual(publish._gh(["user"]), {"login": "octocat"})

    def test_empty_stdout_names_the_command_and_stderr(self):
        message = self._error("empty")
        self.assertIn("keyring locked", message)

    def test_non_json_stdout_names_the_command_and_stderr(self):
        message = self._error("text")
        self.assertIn("color forced", message)


if __name__ == "__main__":
    unittest.main()
