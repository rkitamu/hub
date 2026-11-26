#!/usr/bin/env python3
"""司令塔のセッションの記録（設定の生成）と --resume の引数の自動テスト。python3 tests/test_hub_commander_session.py で実行する。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent
# NOTE: hub の中に __pycache__ を作らない（install.sh のリンクの対象に紛れ込むため）。
sys.dont_write_bytecode = True
sys.path.insert(0, str(HUB / "lib"))
import hublib  # noqa: E402

SID = "11111111-2222-4333-8444-555555555555"


class CommanderLaunchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.env = self.root / "env"
        (self.env / "hub-env").mkdir(parents=True)
        self.cwd = self.env / "hub-env"
        self.record = self.root / "state/hub-session.json"

    def launch(self, agent=None, launch=None):
        base = ["--settings", str(self.cwd / "agent-settings.json"), "--append-system-prompt-file", "/r.md"]
        return hublib.commander_launch(self.env, agent or {"name": "hub"}, self.cwd, base if launch is None else launch,
                                       self.record)

    def test_replaces_settings_and_keeps_rest(self):
        (self.cwd / "agent-settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash"}]}}))
        args = self.launch()
        target = self.record.parent / "hub-agent-settings.json"
        self.assertEqual(args, ["--settings", str(target), "--append-system-prompt-file", "/r.md"])
        data = json.loads(target.read_text())
        self.assertEqual(data["hooks"]["PreToolUse"], [{"matcher": "Bash"}])
        command = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn(str(HUB / "scripts/hub-session-record"), command)
        self.assertIn(str(self.record), command)
        self.assertIn(str(self.cwd), command)

    def test_without_agent_settings(self):
        args = self.launch(launch=[])
        self.assertEqual(args[0], "--settings")
        self.assertIn("SessionStart", json.loads(Path(args[1]).read_text())["hooks"])

    def test_not_recorded_for_other_kinds_or_own_settings(self):
        self.assertIsNone(self.launch({"name": "hub", "kind": "codex"}))
        self.assertIsNone(self.launch({"name": "hub", "args": ["--settings=/x.json"]}))

    def test_broken_hooks(self):
        (self.cwd / "agent-settings.json").write_text(json.dumps({"hooks": []}))
        with self.assertRaises(hublib.HubError):
            self.launch()


class CommanderResumeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.cwd = self.root / "hub-env"
        self.cwd.mkdir()
        self.record = self.root / "hub-session.json"
        self.transcript = self.root / f"{SID}.jsonl"
        self.transcript.write_text("")

    def write(self, **over):
        data = {"session_id": SID, "transcript_path": str(self.transcript), "cwd": str(self.cwd), **over}
        self.record.write_text(json.dumps(data))

    def resume(self, agent=None):
        return hublib.commander_resume_args("hub", agent or {"name": "hub"}, self.cwd, self.record)

    def test_returns_resume(self):
        self.write()
        self.assertEqual(self.resume(), ["--resume", SID])

    def test_errors(self):
        cases = {
            "記録されていません": lambda: None,
            "読めません": lambda: self.record.write_text("{broken"),
            "cwd": lambda: self.write(cwd=str(self.root)),
            "履歴": lambda: self.transcript.unlink(),
        }
        for expected, prepare in cases.items():
            with self.subTest(expected):
                self.record.unlink(missing_ok=True)
                self.transcript.write_text("")
                if expected != "記録されていません":
                    self.write()
                prepare()
                with self.assertRaises(hublib.HubError) as cm:
                    self.resume()
                self.assertIn(expected, str(cm.exception))

    def test_rejects_conflicting_agent(self):
        self.write()
        for agent in ({"kind": "codex"}, {"args": ["--settings", "/x"]}, {"args": ["-c"]}, {"args": ["--resume=x"]}):
            with self.subTest(agent), self.assertRaises(hublib.HubError):
                self.resume(agent)


if __name__ == "__main__":
    unittest.main()
