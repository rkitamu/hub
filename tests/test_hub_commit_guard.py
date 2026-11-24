#!/usr/bin/env python3
"""hub-commit-guard の自動テスト。python3 tests/test_hub_commit_guard.py で実行する。

判定だけを行う。コマンドは実行しない。
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent / "scripts/hub-commit-guard"
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
ENV.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")


def sh(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=ENV)


class CommitGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        sh(self.repo, "init", "-q", "-b", "main")
        sh(self.repo, "commit", "-q", "--allow-empty", "-m", "init")

    def run_guard(self, command, cwd=None, args=()):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd or self.repo)}
        return subprocess.run([str(GUARD), *args], input=json.dumps(payload), capture_output=True, text=True, env=ENV)

    def deny(self, command, **kw):
        self.assertEqual(self.run_guard(command, **kw).returncode, 2, f"拒否されるべき: {command}")

    def allow(self, command, **kw):
        self.assertEqual(self.run_guard(command, **kw).returncode, 0, f"許可されるべき: {command}")

    # ── 許可 ──

    def test_main_protection_is_not_our_job(self):
        for cmd in ("git commit -m x", "git commit --amend --no-edit", "git push origin main", "git push --force origin main",
                    "git push -f", "git merge feature", "git merge --continue", "git rebase main", "git reset --hard HEAD~1"):
            self.allow(cmd)

    def test_ordinary_commands(self):
        for cmd in ("git status", "git add . && git commit -m x", "git commit -am x", "git commit -m -n",
                    "git commit -m --no-verify", "git commit -m x -- -n", "git commit -F -n",
                    "git commit -S -m x", "git commit -uno -m x", "git commit --verify -m x", "git push -n origin feature",
                    "git merge -n feature", "git log --grep=no-verify", "echo 'git commit --no-verify'",
                    "cat <<'EOF'\ngit commit --no-verify\nEOF", "# git commit -n", "git commit --dry-run",
                    "echo 'unterminated"):
            self.allow(cmd)

    def test_reading_hooks_path(self):
        for cmd in ("git config core.hooksPath", "git config --get core.hooksPath", "git config get core.hooksPath",
                    "git config --list", "git config -l --show-origin", "git config --get-regexp hookspath",
                    "git config user.name t", "git -c user.name=t commit -m x", "git config --unset user.name"):
            self.allow(cmd)

    def test_commit_message_heredoc(self):
        self.allow("git commit -m \"$(cat <<'EOF'\nfix: don't use -n or --no-verify\nEOF\n)\"")

    def test_other_env_vars(self):
        self.allow("GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=user.name GIT_CONFIG_VALUE_0=t git commit -m x")
        self.allow("GIT_DIR=.git git commit -m x")

    # ── 拒否: --no-verify と commit -n ──

    def test_no_verify(self):
        for cmd in ("git commit --no-verify -m x", "git commit -m x --no-verify", "git commit --no-verif -m x",
                    "git push --no-verify", "git push --no-verify origin feature", "git merge --no-verify feature",
                    "git merge --continue --no-verify", "git pull --no-verify", "git rebase --no-verify main",
                    "git am --no-verify x.patch", "git -C . commit --no-verify -m x"):
            self.deny(cmd)

    def test_commit_n(self):
        for cmd in ("git commit -n -m x", "git commit -nm x", "git commit -an -m x", "git commit -m x -n",
                    "git commit -vn -m x"):
            self.deny(cmd)

    def test_routes(self):
        for cmd in ("git add . && git commit -n -m x", "(git commit -n -m x)", "echo $(git commit -n -m x)",
                    "bash -c 'git commit --no-verify -m x'", "sh -lc \"git push --no-verify\"", "eval git commit -n -m x",
                    "env git commit -n -m x", "sudo -u me git commit -n -m x", "timeout 5 git push --no-verify",
                    "xargs git commit -n -m", "find . -maxdepth 0 -exec git commit -n -m x \\;",
                    "bash <<'EOF'\ngit commit -n -m x\nEOF", "GIT=git; $GIT commit -n -m x", "/usr/bin/git commit -n -m x",
                    "g''it com\"\"mit --no-verify -m x"):
            self.deny(cmd)

    def test_alias(self):
        sh(self.repo, "config", "alias.ci", "commit -n")
        sh(self.repo, "config", "alias.ci2", "ci")
        sh(self.repo, "config", "alias.pnv", "!git push --no-verify")
        sh(self.repo, "config", "alias.st2", "status")
        self.deny("git ci -m x")
        self.deny("git ci2 -m x")
        self.deny("git pnv")
        self.allow("git st2")
        self.deny("git -c alias.zz='commit --no-verify' zz -m x")

    # ── 拒否: core.hooksPath の上書き ──

    def test_dash_c_hooks_path(self):
        for cmd in ("git -c core.hooksPath=/dev/null commit -m x", "git -c core.hookspath=/dev/null push",
                    "git -c CORE.HOOKSPATH= commit -m x", "git -c core.hooksPath commit -m x",
                    "git -c user.name=t -c core.hooksPath=x commit -m x", "git --config-env=core.hooksPath=V commit -m x",
                    "git --config-env core.hooksPath=V commit -m x", 'git -c "$UNKNOWN_X" commit -m x'):
            self.deny(cmd)

    def test_env_hooks_path(self):
        for cmd in ("GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null git commit -m x",
                    "GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/dev/null'\" git push",
                    "export GIT_CONFIG_KEY_0=core.hooksPath; git commit -m x",
                    "env GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_COUNT=1 git commit -m x",
                    "sudo GIT_CONFIG_KEY_0=core.hooksPath git commit -m x",
                    "declare -x GIT_CONFIG_PARAMETERS=\"'core.hookspath'=''\"",
                    "GIT_CONFIG_KEY_0=\"$UNKNOWN_X\" git commit -m x"):
            self.deny(cmd)

    def test_config_hooks_path(self):
        for cmd in ("git config core.hooksPath /dev/null", "git config --local core.hooksPath x",
                    "git config --unset core.hooksPath", "git config --unset-all core.hooksPath",
                    "git config set core.hooksPath x", "git config unset core.hooksPath",
                    "git config --add core.hooksPath x", "git config --replace-all core.hooksPath x",
                    "git config --global core.hooksPath x", "git config --remove-section core",
                    "git config --rename-section core x", "git config remove-section core",
                    "git -C . config core.hooksPath ''", "bash -c 'git config --unset core.hooksPath'"):
            self.deny(cmd)

    # ── 入出力 ──

    def test_allow_option_is_accepted_and_ignored(self):
        self.assertEqual(self.run_guard("git commit -m x", args=("--allow", str(self.root / "work"))).returncode, 0)
        self.assertEqual(self.run_guard("git commit -n -m x", args=("--allow", str(self.repo))).returncode, 2)
        self.assertEqual(self.run_guard("git status", args=("--bogus",)).returncode, 2)

    def test_fails_closed(self):
        self.deny("git commit -n -m 'unterminated")
        self.deny('git commit -m "$(unterminated')
        p = subprocess.run([str(GUARD)], input="not json", capture_output=True, text=True, env=ENV)
        self.assertEqual(p.returncode, 2)

    def test_without_cwd(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}
        p = subprocess.run([str(GUARD)], input=json.dumps(payload), capture_output=True, text=True, env=ENV)
        self.assertEqual(p.returncode, 0)
        payload["tool_input"]["command"] = "git commit -n -m x"
        p = subprocess.run([str(GUARD)], input=json.dumps(payload), capture_output=True, text=True, env=ENV)
        self.assertEqual(p.returncode, 2)

    def test_other_tools_ignored(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "/x"}, "cwd": str(self.repo)}
        p = subprocess.run([str(GUARD)], input=json.dumps(payload), capture_output=True, text=True, env=ENV)
        self.assertEqual(p.returncode, 0)

    def test_reason_goes_to_stderr(self):
        p = self.run_guard("git commit --no-verify -m x")
        self.assertIn("--no-verify", p.stderr)


if __name__ == "__main__":
    unittest.main()
