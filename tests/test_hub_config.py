#!/usr/bin/env python3
"""hub.toml の読み込み（エージェントの定義と herdr のレイアウト）と、旧形式からの書き換えの自動テスト。

python3 tests/test_hub_config.py で実行する。
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

HUB = Path(__file__).resolve().parent.parent
# NOTE: hub の中に __pycache__ を作らない（install.sh のリンクの対象に紛れ込むため）。
sys.dont_write_bytecode = True
sys.path.insert(0, str(HUB / "lib"))
import hub_backend  # noqa: E402
import hub_config  # noqa: E402
import hublib  # noqa: E402

herdr_backend = hub_backend.load_module("herdr")


def load_script(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


migrate = load_script("migrate_hub_toml", HUB / "contrib/herdr/migrate-hub-toml")

# personal の hub.toml と同じ形の旧形式（setup・複数タブ・cwd の違うペインを含む）
LEGACY = """
session = "personal"
todo_file = "todo/data/active.md"

[[workspace]]
label = "hub-env"
cwd = "hub-env"
[[workspace.tab]]
label = "Claude"
[[workspace.tab.pane]]
name = "claude"
agent = { name = "hub", args = ["--model", "m"] }
[[workspace.tab]]
label = "Shell"

[[workspace]]
label = "todo"
cwd = "todo"
setup = "todo/contrib/herdr/todo-herdr"
agents = ["todo"]

[[workspace]]
label = "app"
cwd = "app"
[[workspace.tab]]
label = "Dev"
[[workspace.tab.pane]]
name = "view"
run = "make view"
[[workspace.tab.pane]]
name = "claude"
split = { from = "view", direction = "right", ratio = 0.55 }
cwd = "app/sub"
agent = { name = "app-dev", kind = "codex" }
"""


class AgentDefsTest(unittest.TestCase):
    env = Path("/env")

    def defs(self, text):
        return hub_config.agent_defs(self.env, tomllib.loads(text))

    def test_legacy(self):
        cfg = tomllib.loads(LEGACY)
        self.assertTrue(hub_config.is_legacy(cfg))
        self.assertEqual(hub_config.commander_name(cfg), "hub")
        self.assertEqual(hub_config.session_record_name(cfg), "hub-session.json")
        got = {a["name"]: (a["cwd"], a["kind"], a["args"], a["launched_by"]) for a in self.defs(LEGACY)}
        self.assertEqual(got, {"hub": ("hub-env", "claude", ["--model", "m"], None),
                               "todo": ("todo", "claude", [], "setup"),
                               "app-dev": ("app/sub", "codex", [], None)})

    def test_new(self):
        text = '[[agent]]\nname = "commander"\ncwd = "hub-env"\n[[agent]]\nname = "x"\ncwd = "x"\nargs = ["-a"]\n'
        cfg = tomllib.loads(text)
        self.assertFalse(hub_config.is_legacy(cfg))
        self.assertEqual(hub_config.commander_name(cfg), "commander")
        self.assertEqual(hub_config.session_record_name(cfg), "commander-session.json")
        defs = self.defs(text)
        self.assertEqual([a["name"] for a in defs], ["commander", "x"])
        self.assertEqual(defs[1]["path"], Path("/env/x"))
        self.assertEqual(hub_config.find_agent(self.env, cfg, "x")["args"], ["-a"])
        with self.assertRaises(hublib.HubError):
            hub_config.find_agent(self.env, cfg, "nobody")

    def test_empty_config_is_new_format(self):
        self.assertFalse(hub_config.is_legacy({}))
        self.assertEqual(hub_config.agent_defs(self.env, {}), [])

    def test_invalid(self):
        cases = {
            "name が無い": '[[agent]]\ncwd = "x"\n',
            "使えない名前": '[[agent]]\nname = "a b"\ncwd = "x"\n',
            "重複": '[[agent]]\nname = "a"\ncwd = "x"\n[[agent]]\nname = "a"\ncwd = "y"\n',
            "cwd が無い": '[[agent]]\nname = "a"\n',
            "args が文字列": '[[agent]]\nname = "a"\ncwd = "x"\nargs = "-a"\n',
            "launched_by": '[[agent]]\nname = "a"\ncwd = "x"\nlaunched_by = "pane"\n',
            "知らないキー": '[[agent]]\nname = "a"\ncwd = "x"\nsession = "s"\n',
            "commander の cwd": '[[agent]]\nname = "commander"\ncwd = "app"\n',
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(hublib.HubError):
                self.defs(text)


class LayoutTest(unittest.TestCase):
    env = Path("/env")
    AGENTS = '[[agent]]\nname = "commander"\ncwd = "hub-env"\nargs = ["-m"]\n[[agent]]\nname = "todo"\ncwd = "todo"\nlaunched_by = "setup"\n'

    def layout(self, text):
        return herdr_backend.layout(self.env, tomllib.loads(text))

    def test_resolves_agent_refs(self):
        wss = self.layout(self.AGENTS + '[herdr]\nsession = "s"\n[[herdr.workspace]]\nlabel = "h"\ncwd = "hub-env"\n'
                          '[[herdr.workspace.tab]]\nlabel = "C"\n[[herdr.workspace.tab.pane]]\nname = "c"\nagent = "commander"\n')
        pane = wss[0]["tab"][0]["pane"][0]
        self.assertEqual(pane["agent"], {"name": "commander", "kind": "claude", "args": ["-m"]})
        self.assertEqual(pane["cwd"], "hub-env")

    def test_invalid(self):
        head = self.AGENTS + '[[herdr.workspace]]\nlabel = "w"\ncwd = "hub-env"\n'
        pane = '[[herdr.workspace.tab]]\nlabel = "t"\n[[herdr.workspace.tab.pane]]\nname = "p"\n'
        cases = {
            "知らない agent": head + pane + 'agent = "nobody"\n',
            "表の agent": head + pane + 'agent = { name = "commander" }\n',
            "setup のエージェントをペインに": head + pane + 'agent = "todo"\n',
            "cwd の食い違い": head + pane + 'agent = "commander"\ncwd = "app"\n',
            "setup の agents の知らない名前": head + 'setup = "s"\nagents = ["nobody"]\n',
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(hublib.HubError):
                self.layout(text)


class MigrateTest(unittest.TestCase):
    env = Path("/env")

    def test_round_trip_keeps_agents_and_layout(self):
        old = tomllib.loads(LEGACY)
        new = tomllib.loads(migrate.migrate(old))
        self.assertEqual(new["backend"], "herdr")
        self.assertEqual(new["herdr"]["session"], "personal")
        self.assertEqual(new["todo_file"], "todo/data/active.md")
        rename = lambda n: "commander" if n == "hub" else n
        strip = lambda defs: sorted((rename(a["name"]), a["path"], a["kind"], a["args"], a["launched_by"]) for a in defs)
        self.assertEqual(strip(hub_config.agent_defs(self.env, new)), strip(hub_config.agent_defs(self.env, old)))
        # レイアウトは、ペインの cwd がエージェントの cwd で埋まることと、司令塔の名前のほかは同じ
        expected = herdr_backend.layout(self.env, old)
        for ws in expected:
            if "agents" in ws:
                ws["agents"] = [rename(n) for n in ws["agents"]]
            for tab in ws.get("tab", []):
                for p in tab.get("pane", []):
                    if "agent" in p:
                        p["agent"] = {"name": rename(p["agent"]["name"]), "kind": p["agent"].get("kind", "claude"),
                                      "args": p["agent"].get("args", [])}
                        p.setdefault("cwd", ws["cwd"])
        self.assertEqual(herdr_backend.layout(self.env, new), expected)

    def test_refuses(self):
        cases = {
            "新形式": 'backend = "herdr"\n',
            "知らないキー": 'session = "s"\nextra = 1\n',
            "ペインの知らないキー": '[[workspace]]\nlabel = "a"\ncwd = "a"\n[[workspace.tab]]\nlabel = "t"\n'
                                  '[[workspace.tab.pane]]\nname = "p"\nfoo = 1\n',
            "重複": '[[workspace]]\nlabel = "a"\ncwd = "a"\nsetup = "s"\nagents = ["x"]\n'
                    '[[workspace]]\nlabel = "b"\ncwd = "b"\nsetup = "s"\nagents = ["x"]\n',
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(migrate.MigrateError):
                migrate.migrate(tomllib.loads(text))

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["migrate-hub-toml", *argv]), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            return migrate.main(), out.getvalue(), err.getvalue()

    def test_write_keeps_backup(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "hub.toml"
            path.write_text(LEGACY)
            rc, out, _ = self.run_main(str(path))
            self.assertEqual((rc, path.read_text()), (0, LEGACY))
            self.assertIn("[[agent]]", out)
            rc, _, _ = self.run_main(str(path), "--write")
            self.assertEqual(rc, 0)
            self.assertEqual((Path(d) / "hub.toml.bak").read_text(), LEGACY)
            self.assertIn("backend", tomllib.loads(path.read_text()))
            path.write_text(LEGACY)
            rc, _, err = self.run_main(str(path), "--write")
            self.assertEqual(rc, 1)
            self.assertIn(".bak", err)
            self.assertEqual(path.read_text(), LEGACY)


if __name__ == "__main__":
    unittest.main()
