#!/usr/bin/env python3
"""hub-guard の自動テスト。python3 tests/test_hub_guard.py で実行する。"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent / "scripts/hub-guard"


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Path(self.tmp.name).resolve()
        self.root = self.env / "hub-env"
        self.child = self.env / "todo"
        (self.root / "sub").mkdir(parents=True)
        self.child.mkdir()
        (self.child / "a.md").write_text("x")

    def run_guard(self, payload, root=None):
        data = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([str(GUARD), "--root", str(root or self.root)], input=data, capture_output=True, text=True)

    def call(self, path, tool="Edit", cwd=None):
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        return self.run_guard({"tool_name": tool, "tool_input": {key: path}, "cwd": str(cwd or self.root)}).returncode

    def test_allows_inside(self):
        for tool in ("Edit", "Write", "NotebookEdit"):
            self.assertEqual(self.call(str(self.root / "MAP.md"), tool), 0)
        self.assertEqual(self.call(str(self.root / "new/dir/file.md"), "Write"), 0)

    def test_work_worktree(self):
        tree = self.env / ".hub-worktrees/w/hub-env"
        other = self.env / ".hub-worktrees/x/hub-env"
        for d in (tree, other):
            d.mkdir(parents=True)
            (d / ".git").write_text("gitdir: x")
        def run(project, path):
            env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
            data = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(path)}, "cwd": str(project)})
            return subprocess.run([str(GUARD), "--root", str(self.root)], input=data, capture_output=True, text=True, env=env).returncode
        self.assertEqual(run(tree, tree / "MAP.md"), 0)
        self.assertEqual(run(tree, self.root / "work/w/status.md"), 0)
        self.assertEqual(run(tree, other / "MAP.md"), 2)
        self.assertEqual(run(tree, self.child / "a.md"), 2)
        self.assertEqual(run(self.root, tree / "MAP.md"), 2)
        (tree / ".git").unlink()
        (tree / ".git").mkdir()
        self.assertEqual(run(tree, tree / "MAP.md"), 2)

    def test_allows_relative_inside(self):
        self.assertEqual(self.call("MAP.md"), 0)
        self.assertEqual(self.call("../MAP.md", cwd=self.root / "sub"), 0)

    def test_denies_outside(self):
        for tool in ("Edit", "Write", "NotebookEdit"):
            self.assertEqual(self.call(str(self.child / "a.md"), tool), 2)

    def test_denies_relative_and_dotdot(self):
        self.assertEqual(self.call("../todo/a.md"), 2)
        self.assertEqual(self.call(str(self.root / "sub/../../todo/a.md")), 2)
        self.assertEqual(self.call("a.md", cwd=self.child), 2)

    def test_denies_sibling_with_same_prefix(self):
        (self.env / "hub-env-evil").mkdir()
        self.assertEqual(self.call(str(self.env / "hub-env-evil/a.md")), 2)

    def test_denies_symlinks(self):
        (self.root / "dirlink").symlink_to(self.child)
        (self.root / "filelink.md").symlink_to(self.child / "a.md")
        (self.root / "dangling.md").symlink_to(self.child / "new.md")
        self.assertEqual(self.call(str(self.root / "dirlink/a.md")), 2)
        self.assertEqual(self.call(str(self.root / "filelink.md")), 2)
        self.assertEqual(self.call(str(self.root / "dangling.md")), 2, "宙づりのリンクの先へ書ける")
        self.assertEqual(self.call("dirlink/new.md"), 2)

    def test_allows_when_root_is_reached_through_symlink(self):
        link = self.env / "link-to-env"
        link.symlink_to(self.root)
        self.assertEqual(self.run_guard({"tool_name": "Edit", "tool_input": {"file_path": str(self.root / "a")},
                                         "cwd": str(self.root)}, root=link).returncode, 0)

    def test_ignores_other_tools(self):
        self.assertEqual(self.run_guard({"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}).returncode, 0)
        self.assertEqual(self.run_guard({"tool_name": "Bash", "tool_input": {"command": "touch /tmp/x"}}).returncode, 0)

    def test_fails_closed(self):
        self.assertEqual(self.run_guard("not json").returncode, 2)
        self.assertEqual(self.run_guard("[]").returncode, 2)
        self.assertEqual(self.run_guard({"tool_name": "Edit", "tool_input": {}}).returncode, 2)
        self.assertEqual(self.run_guard({"tool_name": "Edit", "tool_input": {"file_path": 1}}).returncode, 2)
        self.assertEqual(self.run_guard({"tool_name": "Edit", "tool_input": {"file_path": "a"}}).returncode, 2, "cwd なしの相対パス")
        self.assertEqual(self.run_guard({"tool_name": "Write", "tool_input": {"file_path": "/a\u0000b"}, "cwd": "/"}).returncode, 2)
        self.assertEqual(self.run_guard({"tool_name": "Edit", "tool_input": {"file_path": "x"}, "cwd": str(self.root)},
                                        root=self.env / "missing").returncode, 2)

    def test_bad_usage_denies(self):
        p = subprocess.run([str(GUARD)], input="{}", capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)

    def test_reason_goes_to_stderr(self):
        p = self.run_guard({"tool_name": "Edit", "tool_input": {"file_path": str(self.child / "a.md")}, "cwd": str(self.root)})
        self.assertIn("hub-env の外", p.stderr)


if __name__ == "__main__":
    unittest.main()
