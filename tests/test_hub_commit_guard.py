#!/usr/bin/env python3
"""hub-commit-guard の自動テスト。python3 tests/test_hub_commit_guard.py で実行する。

実際の git リポジトリに対して判定だけを行う。コマンドは実行しない。
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
        self.main = self.root / "on-main"
        self.feat = self.root / "on-feat"
        self.master = self.root / "on-master"
        self.plain = self.root / "plain"
        self.plain.mkdir()
        for path, branch in ((self.main, "main"), (self.master, "master"), (self.feat, "main")):
            path.mkdir()
            sh(path, "init", "-q", "-b", branch)
            sh(path, "commit", "-q", "--allow-empty", "-m", "init")
        sh(self.feat, "switch", "-q", "-c", "feature")

    def rc(self, command, cwd=None, allow=()):
        args = [str(GUARD)]
        for a in allow:
            args += ["--allow", str(a)]
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd or self.main)}
        return subprocess.run(args, input=json.dumps(payload), capture_output=True, text=True, env=ENV).returncode

    def deny(self, command, **kw):
        self.assertEqual(self.rc(command, **kw), 2, f"拒否されるべき: {command}")

    def allow(self, command, **kw):
        self.assertEqual(self.rc(command, **kw), 0, f"許可されるべき: {command}")

    def test_direct(self):
        self.deny("git commit -m x")
        self.deny("git commit --amend --no-edit")
        self.deny("git commit -m x", cwd=self.master)
        self.allow("git commit -m x", cwd=self.feat)

    def test_unborn_main(self):
        new = self.root / "new"
        new.mkdir()
        sh(new, "init", "-q", "-b", "main")
        self.deny("git commit -m first", cwd=new)

    def test_detached_is_allowed(self):
        sh(self.feat, "checkout", "-q", "--detach")
        self.allow("git commit -m x", cwd=self.feat)

    def test_outside_repo(self):
        self.allow("git commit -m x", cwd=self.plain)

    def test_dash_c(self):
        self.deny(f"git -C {self.main} commit -m x", cwd=self.feat)
        self.allow(f"git -C {self.feat} commit -m x", cwd=self.main)
        self.deny("git -C ../on-main commit -m x", cwd=self.feat)
        self.deny(f"git -C {self.root} -C on-main commit -m x", cwd=self.feat)

    def test_git_options(self):
        self.deny("git -c user.name=a commit -m x")
        self.deny("git --no-pager commit -m x")
        self.deny(f"git --git-dir={self.main}/.git --work-tree={self.main} commit -m x", cwd=self.feat)
        self.deny(f"git --git-dir {self.main}/.git commit -m x", cwd=self.feat)
        self.deny(f"GIT_DIR={self.main}/.git git commit -m x", cwd=self.feat)
        self.deny(f"export GIT_DIR={self.main}/.git; git commit -m x", cwd=self.feat)
        self.allow(f"GIT_DIR={self.feat}/.git git commit -m x")

    def test_compound(self):
        for cmd in ("git add . && git commit -m x", "git status; git commit -m x", "false || git commit -m x",
                    "git add .\ngit commit -m x", "true | git commit -m x", "git commit -m x &",
                    "(git commit -m x)", "{ git commit -m x; }", "if true; then git commit -m x; fi",
                    "for i in 1; do git commit -m x; done", "echo $(git commit -m x)", "echo `git commit -m x`",
                    'echo "$(git commit -m x)"', "git add . \\\n && git commit -m x", "cat <(git commit -m x)"):
            self.deny(cmd)

    def test_cd(self):
        self.deny(f"cd {self.main} && git commit -m x", cwd=self.feat)
        self.allow(f"cd {self.feat} && git commit -m x")
        self.deny("cd ../on-main; git commit -m x", cwd=self.feat)
        self.deny(f"(cd {self.main} && git commit -m x)", cwd=self.feat)
        self.deny(f"(cd {self.feat}); git commit -m x")
        self.allow(f"(cd {self.main}); git commit -m x", cwd=self.feat)
        self.deny("cd /no/such/dir; git commit -m x")
        self.deny('cd "$UNKNOWN_DIR_X" && git commit -m x', cwd=self.feat)

    def test_variables(self):
        self.deny(f'R={self.main}; git -C "$R" commit -m x', cwd=self.feat)
        self.allow(f'R={self.feat}; git -C "$R" commit -m x')
        self.deny('git -C "$UNKNOWN_DIR_X" commit -m x', cwd=self.feat)

    def test_wrappers(self):
        for cmd in ("env git commit -m x", "env A=1 git commit -m x", "command git commit -m x", "/usr/bin/git commit -m x",
                    "\\git commit -m x", "'git' commit -m x", "g''it com\"\"mit -m x", "sudo -u me git commit -m x",
                    "nohup git commit -m x", "timeout 5 git commit -m x", "nice -n 5 git commit -m x", "time git commit -m x",
                    "echo x | xargs git commit -m", "find . -maxdepth 0 -exec git commit -m x \\;",
                    "bash -c 'git commit -m x'", "sh -lc \"git commit -m x\"", "eval git commit -m x",
                    "eval 'git commit -m x'", "bash <<'EOF'\ngit commit -m x\nEOF", 'GIT=git; $GIT commit -m x',
                    "$(command -v git) commit -m x"):
            self.deny(cmd)
        self.deny(f"env -C {self.main} git commit -m x", cwd=self.feat)

    def test_branch_switch_in_chain(self):
        self.allow("git switch -c work && git commit -m x")
        self.allow("git checkout -b work && git add . && git commit -m x")
        self.allow("git switch --detach && git commit -m x")
        self.deny("git checkout main && git commit -m x", cwd=self.feat)
        self.deny("git switch main && git commit -m x", cwd=self.feat)
        self.deny("git checkout -b main && git commit -m x", cwd=self.feat)

    def test_untrusted_switch(self):
        self.deny("git switch -c work; git commit -m x")
        self.deny("git switch -c work\ngit commit -m x")
        self.deny("git switch -c work || git commit -m x")
        self.deny("false || git switch -c work && git commit -m x")
        self.deny("git switch -c work && true; git commit -m x")
        self.deny("git checkout main -- file && git commit -m x")
        self.deny("git checkout -- file && git commit -m x")

    def test_switch_in_other_repo_does_not_count(self):
        self.deny(f"git -C {self.feat} switch -c other && git commit -m x")

    def test_alias(self):
        sh(self.main, "config", "alias.ci", "commit -v")
        sh(self.main, "config", "alias.ci2", "ci")
        sh(self.main, "config", "alias.shci", "!git commit -m x")
        sh(self.main, "config", "alias.st2", "status")
        self.deny("git ci -m x")
        self.deny("git ci2 -m x")
        self.deny("git shci")
        self.allow("git st2")
        self.deny("git -c alias.zz=commit zz -m x")

    def test_text_is_not_a_command(self):
        for cmd in ("echo 'git commit -m x'", 'echo "git commit"', "# git commit -m x", "grep -r 'git commit' .",
                    "git log --grep=commit", "git show HEAD:commit", "ls commit", "git status",
                    "cat <<'EOF'\ngit commit -m x\nEOF", "cat <<EOF > note.md\ndon't git commit -m x\nEOF",
                    "git commit --dry-run", "git commit --help", "command -v git", "echo a && echo b 2>&1 >/dev/null"):
            self.allow(cmd)

    def test_commit_message_heredoc_on_feature(self):
        msg = "git commit -m \"$(cat <<'EOF'\nfix: don't break (x)\n\ngit commit in body\nEOF\n)\""
        self.allow(msg, cwd=self.feat)
        self.deny(msg, cwd=self.main)

    def test_local_merge_is_allowed(self):
        self.allow("git merge feature")
        self.allow("git merge --no-ff feature -m x")
        self.allow("git merge --continue")
        self.deny("git merge --squash feature && git commit -m x")

    def test_allow_list(self):
        self.allow("git commit -m x", allow=[self.main])
        self.allow(f"git -C {self.main} commit -m x", cwd=self.feat, allow=[self.main])
        self.deny("git commit -m x", cwd=self.master, allow=[self.main])

    def test_allow_does_not_cover_nested_repo(self):
        nested = self.main / "data"
        nested.mkdir()
        sh(nested, "init", "-q", "-b", "main")
        self.deny("git commit -m x", cwd=nested, allow=[self.main])

    def test_fails_closed(self):
        self.deny("git commit -m 'unterminated")
        self.deny('git commit -m "$(unterminated')
        p = subprocess.run([str(GUARD)], input="not json", capture_output=True, text=True, env=ENV)
        self.assertEqual(p.returncode, 2)
        p = subprocess.run([str(GUARD), "--allow", "relative"], input="{}", capture_output=True, text=True, env=ENV)
        self.assertEqual(p.returncode, 2)
        payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}
        p = subprocess.run([str(GUARD)], input=json.dumps(payload), capture_output=True, text=True, env=ENV)
        self.assertEqual(p.returncode, 2, "cwd が無いと判定できない")

    def test_unparsable_unrelated_command_passes(self):
        self.allow("echo 'unterminated")

    def test_other_tools_ignored(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "/x"}, "cwd": str(self.main)}
        p = subprocess.run([str(GUARD)], input=json.dumps(payload), capture_output=True, text=True, env=ENV)
        self.assertEqual(p.returncode, 0)

    def test_reason_goes_to_stderr(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": str(self.main)}
        p = subprocess.run([str(GUARD)], input=json.dumps(payload), capture_output=True, text=True, env=ENV)
        self.assertIn("main への git commit", p.stderr)


if __name__ == "__main__":
    unittest.main()
