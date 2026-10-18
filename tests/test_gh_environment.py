"""Structured gh subprocesses ignore terminal forcing without changing callers."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_observer import dispatch, publish


_FAKE = r'''#!/usr/bin/env python3
import json
import os
import sys

force = os.environ.get("CLICOLOR_FORCE", "")
colored = (force not in ("", "0") or
           (os.environ.get("GH_FORCE_TTY") and not os.environ.get("NO_COLOR")))
payload = json.load(sys.stdin) if "--input" in sys.argv else {"number": 93}
output = json.dumps(payload)
if colored:
    output = "\x1b[32m" + output + "\x1b[0m"
sys.stdout.write(output + "\n")
'''


class StructuredGhEnvironmentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        executable = Path(tmp.name) / "gh"
        executable.write_text(_FAKE, encoding="utf-8")
        executable.chmod(0o700)
        patch = mock.patch.dict(os.environ, {
            "PATH": tmp.name + os.pathsep + os.environ.get("PATH", ""),
        })
        patch.start()
        self.addCleanup(patch.stop)

    def _check_callers(self, forcing):
        with mock.patch.dict(os.environ, forcing):
            os.environ.pop("NO_COLOR", None)
            before = dict(os.environ)
            callers = (
                ("dispatch", lambda: dispatch._gh_json(
                    ["pr", "view", "93", "--json", "number"])),
                ("publish", lambda: publish._gh(["user"])),
            )
            for name, call in callers:
                with self.subTest(caller=name):
                    try:
                        self.assertEqual(call(), {"number": 93})
                    finally:
                        self.assertEqual(dict(os.environ), before)

    def test_forced_color_returns_plain_json(self):
        self._check_callers({"CLICOLOR_FORCE": "1", "GH_FORCE_TTY": ""})

    def test_forced_tty_without_no_color_returns_plain_json(self):
        self._check_callers({"CLICOLOR_FORCE": "0", "GH_FORCE_TTY": "1"})

    def test_payload_content_survives_child_environment(self):
        body = "Synthetic comment: café\n\n**cost** $1.00\n\x1b[32mkept\x1b[0m\n"
        payload = {"body": body}
        with mock.patch.dict(os.environ, {
                "CLICOLOR_FORCE": "1", "GH_FORCE_TTY": "1"}):
            os.environ.pop("NO_COLOR", None)
            before = dict(os.environ)
            # The fake echoes stdin; no network request or publication occurs.
            self.assertEqual(publish._gh(["synthetic"], payload), payload)
            self.assertEqual(dict(os.environ), before)
