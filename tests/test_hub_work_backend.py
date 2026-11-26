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


import hub_work  # noqa: E402


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(HUB / "scripts" / name))
    spec = importlib.util.spec_from_loader(name.replace("-", "_"), loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeBackend(hub_backend.Backend):
    name = "fake"

    def __init__(self, env, cfg):
        super().__init__(env, cfg)
        self.calls: list[tuple] = []
        self.places: dict[str, dict] = {"hub-env": {"id": "p0", "label": "hub-env"}}
        self.tabs: dict[str, list[dict]] = {"hub-env": [{"id": "t0", "title": "Commander"}]}
        self.agents: dict[str, dict] = {"commander": {"name": "commander", "status": "idle", "place": "p0"}}
        self.layout: dict[str, str] = {}  # ディレクトリ → 場所のラベル
        self.here: str | None = None
        self.tab_here: str | None = None
        self.send_result = "ok"
        self.problems: list[str] = []
        self.n = 0

    def label_of(self, place_id):
        return next(l for l, p in self.places.items() if p["id"] == place_id)

    def list_agents(self):
        return list(self.agents.values())

    def list_places(self):
        return list(self.places.values())

    def current_place(self):
        return self.here

    def current_tab(self):
        return self.tab_here

    def open_place(self, label, cwd, title, worktree=None, env=None):
        self.n += 1
        self.calls.append(("open_place", label, str(cwd), title, worktree, env))
        self.places[label] = {"id": f"p{self.n}", "label": label}
        self.tabs[label] = [{"id": f"t{self.n}", "title": title}]
        return {"place": f"p{self.n}", "pane": f"p{self.n}:root", "tab": f"t{self.n}",
                "data": {"source": {"id": "x", "label": "auto"}} if worktree else {}}

    def add_slot(self, place, cwd, title, env=None):
        self.n += 1
        self.calls.append(("add_slot", place, str(cwd), title, env))
        self.tabs.setdefault(self.label_of(place), []).append({"id": f"t{self.n}", "title": title})
        return f"{place}:t{self.n}"

    def list_tabs(self, label):
        return list(self.tabs.get(label, []))

    def close_tab(self, tab):
        self.calls.append(("close_tab", tab))
        for tabs in self.tabs.values():
            tabs[:] = [t for t in tabs if t["id"] != tab]

    def close_tab_later(self, tab):
        self.calls.append(("close_tab_later", tab))

    def place_for_dir(self, path):
        return self.layout.get(str(path))

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
        self.tabs.pop(label, None)
        return self.places.pop(label, None) is not None


class WorkEnv(unittest.TestCase):
    """一時ディレクトリに環境と app リポジトリを作り、hub-work・hub-lead・hub-crew を backend を差し替えて呼べるようにする。"""

    def make_backend(self):
        return FakeBackend(self.env, {})

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("HUB_ROLE", "HUB_WORK"):
            os.environ.pop(k, None)
        self.env = self.root / "env"
        (self.env / "hub-env").mkdir(parents=True)
        (self.env / "hub-env/hub.toml").write_text(
            'backend = "herdr"\n[[agent]]\nname = "commander"\ncwd = "hub-env"\n'
            '[[agent]]\nname = "todo"\ncwd = "app"\n[herdr]\nsession = "s"\n')
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
        self.backend = self.make_backend()
        self.scripts = {n: load_script(n) for n in ("hub-work", "hub-lead", "hub-crew")}
        for m in self.scripts.values():
            m.backend_for = lambda env, cfg: self.backend
        self.wd = self.env / "hub-env/work"

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.app), *args], capture_output=True, text=True, check=True)

    def run_as(self, script, role, work, *argv):
        out, err = io.StringIO(), io.StringIO()
        env = {k: v for k, v in (("HUB_ROLE", role), ("HUB_WORK", work)) if v}
        with mock.patch.object(sys, "argv", [script, *argv]), mock.patch.dict(os.environ, env), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.scripts[script].main()
        return rc, out.getvalue(), err.getvalue()

    def run_cmd(self, *argv):
        return self.run_as("hub-work", None, None, *argv)

    def lead(self, work, *argv):
        return self.run_as("hub-lead", "lead", work, *argv)

    def crew(self, work, *argv):
        return self.run_as("hub-crew", "crew", work, *argv)

    def calls(self, kind):
        return [c for c in self.backend.calls if c[0] == kind]

    def meta(self, name, archived=False):
        return hublib.read_status(self.wd / ("archive" if archived else "") / name / "status.md")[0]

    def start_assigned(self, name, request="振る指示"):
        """作業を始め、app に crew を振る。"""
        self.backend.layout[str(self.app)] = "app"
        self.backend.places["app"] = {"id": "pa", "label": "app"}
        self.backend.tabs["app"] = [{"id": "ta", "title": "Shell"}]
        rc, _, err = self.run_cmd("start", name, "--request", "始める指示")
        self.assertEqual(rc, 0, err)
        rc, _, err = self.lead(name, "assign", "app", "--request", request)
        self.assertEqual(rc, 0, err)
        return self.env / f".hub-worktrees/{name}/app"


class StartTest(WorkEnv):
    def test_start_puts_lead_tab_in_commander_place(self):
        rc, _, err = self.run_cmd("start", "plain", "--todo", "30", "--request", "依頼の本文")
        self.assertEqual(rc, 0, err)
        (_, place, cwd, title, env), = self.calls("add_slot")
        self.assertEqual((place, cwd, title), ("p0", str((self.env / "hub-env").resolve()), "plain"))
        self.assertEqual(env, {"HUB_WORK": "plain", "HUB_ROLE": "lead"})
        (_, pane, spec), = self.calls("start_agent")
        self.assertEqual(spec["name"], "w-plain")
        argv = spec["argv"]
        self.assertIn(f"Edit(/{self.wd / 'plain'}/**)", argv)
        prompt = Path(argv[argv.index("--append-system-prompt-file") + 1])
        self.assertIn("# lead の役割", prompt.read_text())
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        guard = settings["hooks"]["PreToolUse"][-1]["hooks"][0]["command"]
        self.assertIn(f"--allow {self.wd / 'plain'}", guard)
        meta = self.meta("plain")
        self.assertEqual((meta["workspace"], meta["repos"], meta["todo"]), ("hub-env", [], 30))
        self.assertEqual(meta["agents"][0]["role"], "lead")
        (_, name, text), = self.calls("send")
        self.assertEqual(name, "w-plain")
        self.assertIn("依頼の本文", text)
        self.assertIn("lead", text)

    def test_start_needs_commander(self):
        del self.backend.agents["commander"]
        rc, _, err = self.run_cmd("start", "nocmd")
        self.assertEqual(rc, 1)
        self.assertIn("herdr-reset", err)
        self.assertFalse((self.wd / "nocmd").exists())

    def test_start_is_refused_for_lead_and_crew(self):
        for script_role in ("lead", "crew"):
            rc, _, err = self.run_as("hub-work", script_role, "x", "start", "y")
            self.assertEqual(rc, 1)
            self.assertIn(f"hub-{script_role} を使ってください", err)
        self.assertEqual(self.calls("add_slot"), [])

    def test_start_rejects_existing_agent_and_tab(self):
        self.backend.agents["w-dup"] = {"name": "w-dup"}
        self.assertEqual(self.run_cmd("start", "dup")[0], 1)
        self.backend.tabs["hub-env"].append({"id": "tx", "title": "dup2"})
        self.assertEqual(self.run_cmd("start", "dup2")[0], 1)
        self.assertEqual(self.calls("add_slot"), [])

    def test_undelivered_brief_is_saved_without_leaking(self):
        self.backend.send_result = "blocked"
        rc, _, err = self.run_cmd("start", "blk", "--request", "秘密ではない依頼-固有")
        self.assertEqual(rc, 1)
        self.assertIn("blocked", err)
        self.assertNotIn("秘密ではない依頼-固有", err)
        self.assertIn("秘密ではない依頼-固有", (self.wd / "blk/undelivered-w-blk.md").read_text())


class AssignTest(WorkEnv):
    def test_assign_creates_worktree_and_crew_tab(self):
        tree = self.start_assigned("tree", "振る指示-固有")
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD", cwd=tree).stdout.strip(), "work/tree")
        _, (_, place, cwd, title, env) = self.calls("add_slot")
        self.assertEqual((place, cwd, title), ("pa", str(tree), "tree"))
        self.assertEqual(env, {"HUB_WORK": "tree", "HUB_ROLE": "crew"})
        spec = self.calls("start_agent")[1][2]
        self.assertEqual(spec["name"], "w-tree-app")
        argv = spec["argv"]
        self.assertIn("# crew の役割", Path(argv[argv.index("--append-system-prompt-file") + 1]).read_text())
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        guard = settings["hooks"]["PreToolUse"][-1]["hooks"][0]["command"]
        self.assertIn(f"--allow {tree} --allow {self.wd / 'tree'}", guard)
        meta = self.meta("tree")
        self.assertEqual(meta["repos"], ["app"])
        self.assertEqual(meta["worktrees"][0]["branch"], "work/tree")
        self.assertNotIn("created_workspace", meta["worktrees"][0])
        (_, name, text) = self.calls("send")[1]
        self.assertEqual(name, "w-tree-app")
        self.assertIn("振る指示-固有", text)
        self.assertIn("lead は w-tree", text)

    def test_assign_creates_place_for_repo_outside_layout(self):
        self.run_cmd("start", "auto")
        rc, _, err = self.lead("auto", "assign", "app", "--request", "x")
        self.assertEqual(rc, 0, err)
        (_, label, cwd, title, wt, env), = self.calls("open_place")
        self.assertEqual((label, title, wt, env), ("app", "auto", None, {"HUB_WORK": "auto", "HUB_ROLE": "crew"}))
        self.assertTrue(self.meta("auto")["worktrees"][0]["created_workspace"])

    def test_assign_hub_env_uses_distinct_tab_title(self):
        hub_env = self.env / "hub-env"
        self.git("init", "-q", "-b", "main", cwd=hub_env)
        (hub_env / ".gitignore").write_text("/work/\n")
        self.git("add", ".", cwd=hub_env)
        self.git("commit", "-q", "-m", "init", cwd=hub_env)
        self.backend.layout[str(hub_env.resolve())] = "hub-env"
        self.run_cmd("start", "henv")
        rc, _, err = self.lead("henv", "assign", "hub-env", "--request", "地図の変更")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls("add_slot")[1][3], "henv/hub-env")

    def test_assign_same_repo_sends_to_current_crew(self):
        self.start_assigned("same")
        self.backend.calls.clear()
        rc, out, err = self.lead("same", "assign", "app", "--request", "追加の指示-固有")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls("add_slot"), [])
        self.assertEqual(self.calls("send"), [("send", "w-same-app", "追加の指示-固有")])
        self.assertEqual(len(self.meta("same")["agents"]), 2)

    def test_assign_is_lead_only(self):
        self.run_cmd("start", "only")
        rc, _, err = self.run_as("hub-lead", None, "only", "assign", "app", "--request", "x")
        self.assertEqual(rc, 1)
        self.assertIn("hub-work を使ってください", err)
        rc, _, err = self.run_as("hub-lead", "crew", "only", "assign", "app", "--request", "x")
        self.assertEqual(rc, 1)
        self.assertIn("hub-crew を使ってください", err)

    def test_lead_send_is_limited_to_own_crew_and_residents(self):
        self.start_assigned("snd")
        self.backend.calls.clear()
        self.assertEqual(self.lead("snd", "send", "w-snd-app", "a")[0], 0)
        self.assertEqual(self.lead("snd", "send", "todo", "b")[0], 0)
        rc, _, err = self.lead("snd", "send", "commander", "c")
        self.assertEqual(rc, 1)
        self.assertIn("送れません", err)
        self.assertEqual(self.lead("snd", "send", "w-other-app", "d")[0], 1)
        self.assertEqual([c[1] for c in self.calls("send")], ["w-snd-app", "todo"])

    def test_notify_goes_to_lead_and_commander(self):
        self.start_assigned("nt")
        self.backend.calls.clear()
        self.assertEqual(self.crew("nt", "notify", "crew の報告")[0], 0)
        self.assertEqual(self.lead("nt", "notify", "lead の報告")[0], 0)
        self.assertEqual([c[1] for c in self.calls("send")], ["w-nt", "commander"])


class FinishTest(WorkEnv):
    def test_lead_finish_closes_crews_then_own_tab(self):
        tree = self.start_assigned("fin")
        lead_tab = next(t["id"] for t in self.backend.tabs["hub-env"] if t["title"] == "fin")
        crew_tab = next(t["id"] for t in self.backend.tabs["app"] if t["title"] == "fin")
        self.backend.tab_here = lead_tab
        self.backend.calls.clear()
        rc, _, err = self.lead("fin", "finish")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls("close_tab"), [("close_tab", crew_tab)])
        self.assertEqual(self.calls("close_tab_later"), [("close_tab_later", lead_tab)])
        self.assertEqual(self.calls("close_place"), [])
        self.assertFalse(tree.exists())
        self.assertFalse(self.has_branch("work/fin"))
        self.assertEqual(self.meta("fin", archived=True)["state"], "done")
        self.assertFalse((hublib.env_state_dir(self.env) / "works/fin").exists())

    def test_finish_closes_created_place_when_only_ours(self):
        self.run_cmd("start", "cp")
        self.lead("cp", "assign", "app", "--request", "x")
        self.backend.calls.clear()
        rc, _, err = self.run_cmd("finish", "cp")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c[1] for c in self.calls("close_place")], ["app"])
        # 司令塔からの finish は lead のタブも閉じる
        self.assertEqual(len(self.calls("close_tab")), 1)
        self.assertEqual(self.calls("close_tab_later"), [])

    def test_finish_stops_on_dirty_before_closing(self):
        tree = self.start_assigned("dirty")
        (tree / "x").write_text("x")
        self.backend.calls.clear()
        rc, _, err = self.lead("dirty", "finish")
        self.assertEqual(rc, 1)
        self.assertIn("未コミット", err)
        self.assertEqual(self.calls("close_tab") + self.calls("close_place"), [])
        self.assertTrue((self.wd / "dirty/status.md").exists())

    def test_commander_finish_refuses_from_work_tab(self):
        self.start_assigned("self")
        self.backend.tab_here = next(t["id"] for t in self.backend.tabs["app"] if t["title"] == "self")
        rc, _, err = self.run_cmd("finish", "self")
        self.assertEqual(rc, 1)
        self.assertIn("hub-lead finish", err)

    def test_crew_cannot_finish(self):
        self.start_assigned("cf")
        rc, _, err = self.run_as("hub-lead", "crew", "cf", "finish")
        self.assertEqual(rc, 1)
        self.assertTrue((self.wd / "cf/status.md").exists())

    # ── finish: 作業ブランチの片付け ──

    def has_branch(self, branch, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.app), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
                              capture_output=True).returncode == 0

    def start_with_commit(self, name):
        tree = self.start_assigned(name)
        self.git("commit", "-q", "--allow-empty", "-m", f"{name} の変更", cwd=tree)
        return tree

    def add_origin(self):
        origin = self.root / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.app), str(origin)], check=True)
        self.git("remote", "add", "origin", str(origin))
        self.git("fetch", "-q", "origin")
        self.git("remote", "set-head", "origin", "main")
        return origin

    def test_finish_keeps_unmerged_branch_without_remote(self):
        self.start_with_commit("unm")
        rc, out, err = self.run_cmd("finish", "unm")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has_branch("work/unm"))
        self.assertIn("main にマージされていないので残しています", out)

    def test_finish_deletes_branch_merged_into_local_main(self):
        self.start_with_commit("lm")
        self.git("merge", "-q", "--no-ff", "-m", "merge", "work/lm")
        rc, out, err = self.run_cmd("finish", "lm")
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.has_branch("work/lm"))
        self.assertIn("ブランチ work/lm を消しました（main にマージ済み）", out)

    def test_finish_deletes_branch_merged_into_origin_even_if_local_main_is_old(self):
        origin = self.add_origin()
        tree = self.start_with_commit("om")
        self.git("push", "-q", "-u", "origin", "work/om", cwd=tree)
        # GitHub で PR をマージし、リモートのブランチが自動で消えた状態を作る
        self.git("push", "-q", "origin", "work/om:main", cwd=tree)
        self.git("branch", "-q", "-D", "work/om", cwd=origin)
        rc, out, err = self.run_cmd("finish", "om")
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.has_branch("work/om"))
        self.assertIn("origin/main にマージ済み", out)
        # 古いリモート参照は prune される。ローカルの main は動かさない
        self.assertNotEqual(subprocess.run(["git", "-C", str(self.app), "rev-parse", "--verify", "-q",
                                            "refs/remotes/origin/work/om"], capture_output=True).returncode, 0)
        self.assertEqual(self.git("rev-list", "--count", "main").stdout.strip(), "1")

    def test_finish_does_not_touch_remote_branch(self):
        origin = self.add_origin()
        tree = self.start_with_commit("rb")
        self.git("push", "-q", "-u", "origin", "work/rb", cwd=tree)
        self.git("push", "-q", "origin", "work/rb:main", cwd=tree)
        rc, _, err = self.run_cmd("finish", "rb")
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.has_branch("work/rb"))
        self.assertTrue(self.has_branch("work/rb", cwd=origin))

    def test_finish_keeps_branch_merged_only_locally_when_origin_exists(self):
        self.add_origin()
        self.start_with_commit("ol")
        self.git("merge", "-q", "--no-ff", "-m", "merge", "work/ol")
        rc, out, err = self.run_cmd("finish", "ol")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has_branch("work/ol"))
        self.assertIn("origin/main にマージされていないので残しています", out)

    def test_finish_keeps_branch_when_fetch_fails(self):
        origin = self.add_origin()
        self.start_assigned("ff")
        origin.rename(origin.with_name("gone.git"))
        rc, out, err = self.run_cmd("finish", "ff")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has_branch("work/ff"))
        self.assertIn("判定できないので残しています: origin を fetch できませんでした", out)
        self.assertEqual(self.meta("ff", archived=True)["state"], "done")

    def test_finish_reports_squash_merged_pr_and_keeps_branch(self):
        self.start_with_commit("sq")
        self.enterContext(mock.patch.object(hub_work, "merged_pr", lambda repo, branch: "42"))
        rc, out, err = self.run_cmd("finish", "sq")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has_branch("work/sq"))
        self.assertIn("PR #42 がマージ済みですが", out)

    def test_finish_restores_upstream_when_delete_fails(self):
        self.start_with_commit("up")
        self.git("merge", "-q", "--no-ff", "-m", "merge", "work/up")
        self.git("config", "branch.work/up.remote", "elsewhere")
        self.git("config", "branch.work/up.merge", "refs/heads/x")
        real = hub_work.git
        self.enterContext(mock.patch.object(hub_work, "git", lambda cwd, *args, check=True: (
            subprocess.CompletedProcess(args, 1, "", "拒否-固有") if args[:2] == ("branch", "-d") else real(cwd, *args, check=check))))
        rc, out, err = self.run_cmd("finish", "up")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has_branch("work/up"))
        self.assertIn("拒否-固有", out)
        self.assertEqual(self.git("config", "branch.work/up.remote").stdout.strip(), "elsewhere")
        self.assertEqual(self.git("config", "branch.work/up.merge").stdout.strip(), "refs/heads/x")



class ResumeTest(WorkEnv):
    def test_commander_resume_rebuilds_lead_and_crew(self):
        self.start_assigned("again")
        self.backend.agents = {"commander": self.backend.agents["commander"]}
        self.backend.calls.clear()
        rc, _, err = self.run_cmd("resume", "again")
        self.assertEqual(rc, 0, err)
        # 古いタブは閉じて作り直す
        self.assertEqual(len(self.calls("close_tab")), 2)
        self.assertEqual([c[3] for c in self.calls("add_slot")], ["again", "again"])
        self.assertEqual({c[2]["name"] for c in self.calls("start_agent")}, {"w-again", "w-again-app"})
        self.assertIn("再開", self.calls("send")[0][2])

    def test_resume_is_idempotent(self):
        self.start_assigned("idem")
        self.backend.calls.clear()
        rc, _, _ = self.run_cmd("resume", "idem")
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls("add_slot") + self.calls("start_agent"), [])

    def test_lead_resume_rebuilds_only_crews(self):
        self.start_assigned("lr")
        del self.backend.agents["w-lr-app"]
        del self.backend.agents["w-lr"]
        self.backend.calls.clear()
        rc, _, err = self.lead("lr", "resume")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c[2]["name"] for c in self.calls("start_agent")], ["w-lr-app"])


class LegacyTest(WorkEnv):
    """role の無い旧形式の記録は、hub-work の finish と resume だけができる。"""

    def write_legacy(self, name):
        hub_work.ensure_work_repo(self.env, self.wd)
        path = self.wd / name / "status.md"
        path.parent.mkdir()
        meta = {"state": "active", "todo": None, "repos": ["app"], "workspace": f"w-{name}",
                "agents": [{"name": f"w-{name}", "repo": "app", "cwd": str(self.app), "workspace": f"w-{name}", "kind": "claude"}],
                "worktrees": []}
        hublib.write_status(path, meta, "\n# x\n")

    def test_resume_opens_place_and_finish_closes_it(self):
        self.write_legacy("old")
        rc, _, err = self.run_cmd("resume", "old")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls("open_place")[0][1], "w-old")
        self.assertIn("hub-agent notify", self.calls("send")[0][2])
        self.backend.here = "w-old"
        self.assertEqual(self.run_cmd("finish", "old")[0], 1)
        self.backend.here = None
        rc, _, err = self.run_cmd("finish", "old")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls("close_place")[0][1], "w-old")

    def test_lead_cannot_assign_or_finish_legacy(self):
        self.write_legacy("old2")
        self.assertEqual(self.lead("old2", "assign", "app", "--request", "x")[0], 1)
        rc, _, err = self.lead("old2", "finish")
        self.assertEqual(rc, 1)
        self.assertIn("hub-work finish old2", err)


class ListTest(WorkEnv):
    def test_list_shows_backend_status(self):
        self.run_cmd("start", "ls")
        rc, out, _ = self.run_cmd("list")
        self.assertEqual(rc, 0)
        self.assertIn("w-ls=idle", out)
        del self.backend.agents["w-ls"]
        _, out, _ = self.run_cmd("list")
        self.assertIn("w-ls=停止", out)


class NoBackendTest(WorkEnv):
    """backend なし（none）では、worktree と status.md を作り、起動コマンドを示す。"""

    def make_backend(self):
        return hub_backend.NoneBackend(self.env, {})

    def test_start_and_assign_show_launch_commands(self):
        rc, out, err = self.run_cmd("start", "nb", "--request", "依頼-固有")
        self.assertEqual(rc, 0, err)
        brief = self.wd / "nb/brief-w-nb.md"
        self.assertIn("依頼-固有", brief.read_text())
        self.assertIn(f"cd {(self.env / 'hub-env').resolve()} && env HUB_WORK=nb HUB_ROLE=lead claude", out)
        self.assertIn(f'"$(cat {brief})"', out)
        rc, out, err = self.lead("nb", "assign", "app", "--request", "振る-固有")
        self.assertEqual(rc, 0, err)
        tree = self.env / ".hub-worktrees/nb/app"
        self.assertTrue(tree.is_dir())
        self.assertIn(f"cd {tree} && env HUB_WORK=nb HUB_ROLE=crew claude", out)
        self.assertIn("振る-固有", (self.wd / "nb/brief-w-nb-app.md").read_text())
        self.assertEqual(self.crew("nb", "notify", "x")[0], 3)

    def test_list_and_finish(self):
        self.run_cmd("start", "nf")
        self.lead("nf", "assign", "app", "--request", "x")
        _, out, _ = self.run_cmd("list")
        self.assertIn("w-nf=不明", out)
        rc, _, err = self.lead("nf", "finish")
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.env / ".hub-worktrees/nf").exists())


if __name__ == "__main__":
    unittest.main()
