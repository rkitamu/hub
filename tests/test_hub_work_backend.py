#!/usr/bin/env python3
"""hub-work が backend の操作をどう呼ぶかの自動テスト。herdr は使わず、呼び出しを記録する偽の backend で確かめる。

python3 tests/test_hub_work_backend.py で実行する。
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HUB = Path(__file__).resolve().parent.parent
# NOTE: hub の中に __pycache__ を作らない（install.sh のリンクの対象に紛れ込むため）。
sys.dont_write_bytecode = True
sys.path.insert(0, str(HUB / "lib"))
import hub_backend  # noqa: E402
import hublib  # noqa: E402


def load_hub_work():
    loader = importlib.machinery.SourceFileLoader("hub_work", str(HUB / "scripts/hub-work"))
    spec = importlib.util.spec_from_loader("hub_work", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeBackend(hub_backend.Backend):
    name = "fake"

    def __init__(self, env, cfg):
        super().__init__(env, cfg)
        self.calls: list[tuple] = []
        self.places: dict[str, dict] = {}  # label → {"id", "label"}
        self.agents: dict[str, dict] = {}
        self.here: str | None = None
        self.send_result = "ok"
        self.problems: list[str] = []
        self.n = 0

    def list_agents(self):
        return list(self.agents.values())

    def list_places(self):
        return list(self.places.values())

    def current_place(self):
        return self.here

    def open_place(self, label, cwd, title, worktree=None):
        self.n += 1
        self.calls.append(("open_place", label, str(cwd), title, worktree))
        self.places[label] = {"id": f"p{self.n}", "label": label}
        return {"place": f"p{self.n}", "pane": f"p{self.n}:root", "data": {"source": {"id": "x", "label": "auto"}} if worktree else {}}

    def add_slot(self, place, cwd, title):
        self.calls.append(("add_slot", place, str(cwd), title))
        return f"{place}:slot"

    def start_agent(self, pane, spec):
        self.calls.append(("start_agent", pane, spec))
        self.agents[spec["name"]] = {"name": spec["name"], "status": "idle", "cwd": spec["cwd"], "place": pane}
        return "started"

    def send(self, name, text):
        self.calls.append(("send", name, text))
        return self.send_result

    def can_close(self, labels, worktree_labels):
        self.calls.append(("can_close", set(labels), set(worktree_labels)))
        return self.problems

    def close_place(self, label, worktree=None):
        self.calls.append(("close_place", label, worktree))
        return self.places.pop(label, None) is not None


class WorkEnv(unittest.TestCase):
    """一時ディレクトリに環境と app リポジトリを作り、hub-work を backend を差し替えて呼べるようにする。"""

    def make_backend(self):
        return FakeBackend(self.env, {})

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = self.root / "env"
        (self.env / "hub-env").mkdir(parents=True)
        (self.env / "hub-env/hub.toml").write_text('backend = "herdr"\n[herdr]\nsession = "s"\n')
        os.environ.update({
            "HUB_ENV": str(self.env), "XDG_STATE_HOME": str(self.root / "state"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        (self.root / "claude").mkdir()
        (self.root / "claude/.claude.json").write_text('{"projects": {}}')
        self.app = self.env / "app"
        self.app.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("commit", "-q", "--allow-empty", "-m", "init")
        self.hw = load_hub_work()
        self.backend = self.make_backend()
        self.hw.backend_for = lambda env, cfg: self.backend
        self.wd = self.env / "hub-env/work"

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.app), *args], capture_output=True, text=True, check=True)

    def run_cmd(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["hub-work", *argv]), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = self.hw.main()
        return rc, out.getvalue(), err.getvalue()



class HubWorkTest(WorkEnv):
    def calls(self, kind):
        return [c for c in self.backend.calls if c[0] == kind]

    def meta(self, name, archived=False):
        return hublib.read_status(self.wd / ("archive" if archived else "") / name / "status.md")[0]

    # ── start ──

    def test_start_plain(self):
        rc, _, err = self.run_cmd("start", "plain", "--repo", "app")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls("open_place"), [("open_place", "w-plain", str(self.app), "app", None)])
        (_, pane, spec), = self.calls("start_agent")
        self.assertEqual(pane, "p1:root")
        self.assertEqual((spec["name"], spec["kind"], spec["cwd"]), ("w-plain", "claude", str(self.app)))
        self.assertIn(f"Edit(/{self.wd / 'plain'}/**)", spec["argv"])
        self.assertEqual(self.meta("plain")["agents"][0]["workspace"], "w-plain")

    def test_start_worktree_creates_checkout_in_core(self):
        rc, _, err = self.run_cmd("start", "tree", "--repo", "app", "--worktree")
        self.assertEqual(rc, 0, err)
        tree = self.env / ".hub-worktrees/tree/app"
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD", cwd=tree).stdout.strip(), "work/tree")
        (_, label, cwd, title, wt), = self.calls("open_place")
        self.assertEqual((label, cwd, title), ("w-tree", str(tree), "app"))
        self.assertEqual(wt, {"repo": str(self.app), "path": str(tree), "branch": "work/tree"})
        # backend の data は worktree の記録に残る
        self.assertEqual(self.meta("tree")["worktrees"][0]["source"], {"id": "x", "label": "auto"})

    def test_start_rejects_existing_place_and_agent(self):
        self.backend.places["w-dup"] = {"id": "p0", "label": "w-dup"}
        rc, _, err = self.run_cmd("start", "dup", "--repo", "app")
        self.assertNotEqual(rc, 0)
        self.assertIn("ワークスペース", err)
        self.backend.places.clear()
        self.backend.agents["w-dup2"] = {"name": "w-dup2"}
        rc, _, _ = self.run_cmd("start", "dup2", "--repo", "app")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.calls("open_place"), [])

    def test_start_sends_brief(self):
        rc, _, err = self.run_cmd("start", "req", "--repo", "app", "--request", "依頼の本文")
        self.assertEqual(rc, 0, err)
        (_, name, text), = self.calls("send")
        self.assertEqual(name, "w-req")
        self.assertIn("依頼の本文", text)

    def test_undelivered_brief_is_saved_without_leaking(self):
        self.backend.send_result = "blocked"
        rc, _, err = self.run_cmd("start", "blk", "--repo", "app", "--request", "秘密ではない依頼-固有")
        self.assertEqual(rc, 1)
        self.assertIn("blocked", err)
        self.assertNotIn("秘密ではない依頼-固有", err)
        self.assertIn("秘密ではない依頼-固有", (self.wd / "blk/undelivered-brief.md").read_text())

    # ── resume ──

    def test_resume_reuses_checkout(self):
        self.run_cmd("start", "again", "--repo", "app", "--worktree")
        self.backend.places.clear()
        self.backend.agents.clear()
        self.backend.calls.clear()
        rc, _, err = self.run_cmd("resume", "again")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.calls("open_place")), 1)
        self.assertEqual(len(self.calls("start_agent")), 1)
        self.assertEqual(self.git("rev-parse", "--verify", "-q", "refs/heads/work/again").returncode, 0)

    def test_resume_is_idempotent(self):
        self.run_cmd("start", "idem", "--repo", "app")
        self.backend.calls.clear()
        rc, _, _ = self.run_cmd("resume", "idem")
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls("open_place") + self.calls("start_agent") + self.calls("add_slot"), [])

    def test_resume_adds_slot_when_place_exists(self):
        self.run_cmd("start", "slot", "--repo", "app")
        self.backend.agents.clear()
        self.backend.calls.clear()
        self.run_cmd("resume", "slot")
        self.assertEqual(self.calls("add_slot"), [("add_slot", "p1", str(self.app), "app")])
        self.assertEqual(self.calls("start_agent")[0][1], "p1:slot")

    # ── finish ──

    def test_finish_closes_worktree_then_archives(self):
        self.run_cmd("start", "fin", "--repo", "app", "--worktree")
        self.backend.calls.clear()
        rc, _, err = self.run_cmd("finish", "fin")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls("can_close"), [("can_close", {"w-fin"}, {"w-fin"})])
        (_, label, wt), = self.calls("close_place")
        self.assertEqual((label, wt["source"]), ("w-fin", {"id": "x", "label": "auto"}))
        # backend が checkout を消さなくても、core が消す
        self.assertFalse((self.env / ".hub-worktrees/fin").exists())
        self.assertEqual(self.git("rev-parse", "--verify", "-q", "refs/heads/work/fin").returncode, 0)
        self.assertEqual(self.meta("fin", archived=True)["state"], "done")

    def test_finish_stops_on_dirty_before_closing(self):
        self.run_cmd("start", "dirty", "--repo", "app", "--worktree")
        (self.env / ".hub-worktrees/dirty/app/x").write_text("x")
        rc, _, err = self.run_cmd("finish", "dirty")
        self.assertEqual(rc, 1)
        self.assertIn("未コミット", err)
        self.assertEqual(self.calls("close_place"), [])

    def test_finish_stops_on_backend_problem(self):
        self.run_cmd("start", "prob", "--repo", "app")
        self.backend.problems = ["つながっている-固有"]
        rc, _, err = self.run_cmd("finish", "prob")
        self.assertEqual(rc, 1)
        self.assertIn("つながっている-固有", err)
        self.assertEqual(self.calls("close_place"), [])
        self.assertTrue((self.wd / "prob/status.md").exists())

    def test_finish_refuses_from_own_place(self):
        self.run_cmd("start", "self", "--repo", "app")
        self.backend.here = "w-self"
        rc, _, err = self.run_cmd("finish", "self")
        self.assertEqual(rc, 1)
        self.assertIn("司令塔で実行", err)
        self.assertEqual(self.calls("close_place"), [])

    # ── list ──

    def test_list_shows_backend_status(self):
        self.run_cmd("start", "ls", "--repo", "app")
        rc, out, _ = self.run_cmd("list")
        self.assertEqual(rc, 0)
        self.assertIn("w-ls=idle", out)
        self.backend.agents.clear()
        _, out, _ = self.run_cmd("list")
        self.assertIn("w-ls=停止", out)


class NoBackendTest(WorkEnv):
    """backend なし（none）では、worktree と status.md を作り、起動コマンドを示す。"""

    def make_backend(self):
        return hub_backend.NoneBackend(self.env, {})

    def test_start_shows_launch_command_with_brief(self):
        rc, out, err = self.run_cmd("start", "nb", "--repo", "app", "--worktree", "--request", "依頼-固有")
        self.assertEqual(rc, 0, err)
        tree = self.env / ".hub-worktrees/nb/app"
        self.assertTrue(tree.is_dir())
        brief = self.wd / "nb/brief.md"
        self.assertIn("依頼-固有", brief.read_text())
        self.assertIn(f"cd {tree} && claude --allowedTools", out)
        self.assertIn(f'"$(cat {brief})"', out)
        self.assertFalse((self.wd / "nb/undelivered-brief.md").exists())

    def test_start_without_request(self):
        rc, out, _ = self.run_cmd("start", "nr", "--repo", "app")
        self.assertEqual(rc, 0)
        self.assertNotIn("$(cat", out)
        self.assertFalse((self.wd / "nr/brief.md").exists())

    def test_list_and_finish(self):
        self.run_cmd("start", "nf", "--repo", "app", "--worktree")
        _, out, _ = self.run_cmd("list")
        self.assertIn("w-nf=不明", out)
        rc, _, err = self.run_cmd("finish", "nf")
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.env / ".hub-worktrees/nf").exists())

if __name__ == "__main__":
    unittest.main()
