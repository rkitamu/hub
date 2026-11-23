#!/usr/bin/env python3
"""hub-session-record の自動テスト。python3 tests/test_hub_session_record.py で実行する。"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "scripts/hub-session-record"
SID = "11111111-2222-4333-8444-555555555555"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cwd = self.root / "hub-env"
        self.cwd.mkdir()
        self.state = self.root / "state/hub-session.json"

    def payload(self, sid=SID, **over):
        return {"hook_event_name": "SessionStart", "session_id": sid, "source": "startup", "cwd": str(self.cwd),
                "transcript_path": str(self.root / f"{sid}.jsonl"), **over}

    def run_hook(self, payload):
        data = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([str(HOOK), "--state", str(self.state), "--cwd", str(self.cwd)],
                              input=data, capture_output=True, text=True)

    def test_records_session(self):
        self.assertEqual(self.run_hook(self.payload()).returncode, 0)
        self.assertEqual(json.loads(self.state.read_text())["session_id"], SID)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_new_hub_session_replaces_record(self):
        self.run_hook(self.payload())
        self.run_hook(self.payload(OTHER, source="clear"))
        self.assertEqual(json.loads(self.state.read_text())["session_id"], OTHER)

    def test_rejected_inputs_keep_record(self):
        self.run_hook(self.payload())
        before = self.state.read_text()
        bad = [
            "not json", "[]",
            self.payload(hook_event_name="Stop"),
            self.payload("not-a-uuid"),
            self.payload(OTHER, cwd=str(self.root)),
            self.payload(OTHER, transcript_path=str(self.root / f"{SID}.jsonl")),
            self.payload(OTHER, transcript_path=f"{OTHER}.jsonl"),
            self.payload(OTHER, transcript_path=1),
        ]
        for p in bad:
            with self.subTest(p=p):
                self.assertEqual(self.run_hook(p).returncode, 0, "フックでセッションを止めない")
                self.assertEqual(self.state.read_text(), before)

    def test_rejected_input_creates_nothing(self):
        self.run_hook(self.payload(cwd=str(self.root)))
        self.assertFalse(self.state.exists())

    def test_bad_usage_does_not_block(self):
        self.assertEqual(subprocess.run([str(HOOK)], input="{}", capture_output=True, text=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
