#!/usr/bin/env python3
"""hub-commit-guard の自動テスト。python3 tests/test_hub_commit_guard.py で実行する。"""

import json
import subprocess
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent / "scripts/hub-commit-guard"


def run(command, args=(), tool="Bash"):
    payload = {"tool_name": tool, "tool_input": {"command": command}, "cwd": "/"}
    return subprocess.run([str(GUARD), *args], input=json.dumps(payload), capture_output=True, text=True)


class CommitGuardTest(unittest.TestCase):
    def deny(self, *commands):
        for c in commands:
            self.assertEqual(run(c).returncode, 2, f"拒否されるべき: {c}")

    def allow(self, *commands):
        for c in commands:
            self.assertEqual(run(c).returncode, 0, f"許可されるべき: {c}")

    def test_allowed(self):
        self.allow("git commit -m x", "git commit -am x", "git commit --amend --no-edit", "git push origin main",
                   "git push --force origin main", "git push -n origin feature", "git merge --continue",
                   "git status && git log -n 3", "git config user.name t", "git -c user.name=t commit -m x",
                   "git config core.hooksPath", "git config --get core.hooksPath", "git config get core.hooksPath",
                   "git config core.hooksPath && git status")

    def test_no_verify(self):
        self.deny("git commit --no-verify -m x", "git push --no-verify", "git merge --no-verify feature",
                  "git commit --no-verif -m x", "bash -c 'git push --no-verify'")

    def test_commit_n(self):
        self.deny("git commit -n -m x", "git commit -nm x", "git commit -an -m x", "git commit -m x -n",
                  "git add . && git commit -vn -m x")

    def test_hooks_path(self):
        self.deny("git -c core.hooksPath=/dev/null commit -m x", "git -c CORE.HOOKSPATH= push",
                  "git --config-env=core.hooksPath=V commit -m x",
                  "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null git commit -m x",
                  "GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/dev/null'\" git push",
                  "git config core.hooksPath /dev/null", "git config --unset core.hooksPath",
                  "git config set core.hooksPath x", "git config core.hooksPath; git config core.hooksPath x")

    def test_allow_option_is_accepted_and_ignored(self):
        self.assertEqual(run("git commit -m x", args=("--allow", "/x/work")).returncode, 0)
        self.assertEqual(run("git commit -n -m x", args=("--allow", "/x/work")).returncode, 2)
        self.assertEqual(run("git status", args=("--bogus",)).returncode, 2)

    def test_io(self):
        self.assertEqual(run("git commit --no-verify", tool="Edit").returncode, 0)
        p = subprocess.run([str(GUARD)], input="not json", capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("--no-verify", run("git push --no-verify").stderr)


if __name__ == "__main__":
    unittest.main()
