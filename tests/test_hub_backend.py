#!/usr/bin/env python3
"""backend の選択・読み込みと、環境の特定の自動テスト。python3 tests/test_hub_backend.py で実行する。"""

import os
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


class BackendNameTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("HUB_BACKEND", None)

    def test_env_var_wins(self):
        os.environ["HUB_BACKEND"] = "fake"
        self.assertEqual(hub_backend.backend_name({"backend": "herdr", "session": "s"}), "fake")

    def test_config_backend(self):
        self.assertEqual(hub_backend.backend_name({"backend": "none", "session": "s"}), "none")

    def test_old_format_with_session_is_herdr(self):
        self.assertEqual(hub_backend.backend_name({"session": "s"}), "herdr")

    def test_default_is_none(self):
        self.assertEqual(hub_backend.backend_name({}), "none")

    def test_none_is_not_available_yet(self):
        with self.assertRaises(hublib.HubError):
            hub_backend.backend_for(Path("/nonexistent"), {})


class LoadModuleTest(unittest.TestCase):
    def test_loads_herdr(self):
        module = hub_backend.load_module("herdr")
        backend = module.create(Path("/env"), {"session": "s"})
        self.assertIsInstance(backend, hub_backend.Backend)
        self.assertTrue(issubclass(module.HerdrError, hub_backend.BackendError))

    def test_rejects_bad_name(self):
        for name in ("../lib", "Herdr", "", "a/b"):
            with self.assertRaises(hublib.HubError, msg=name):
                hub_backend.load_module(name)

    def test_rejects_missing(self):
        with self.assertRaises(hublib.HubError):
            hub_backend.load_module("nosuchbackend")


class HerdrDetectTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.herdr = hub_backend.load_module("herdr")

    def test_old_and_new_session_keys(self):
        os.environ["HERDR_SESSION"] = "s"
        self.assertTrue(self.herdr.detect(Path("/e"), {"session": "s"}))
        self.assertTrue(self.herdr.detect(Path("/e"), {"herdr": {"session": "s"}}))
        self.assertFalse(self.herdr.detect(Path("/e"), {"session": "other"}))

    def test_outside_herdr(self):
        os.environ.pop("HERDR_SESSION", None)
        self.assertFalse(self.herdr.detect(Path("/e"), {"session": "s"}))

    def test_check_names_the_session(self):
        os.environ["HERDR_SESSION"] = "other"
        with self.assertRaises(hub_backend.BackendError) as cm:
            self.herdr.create(Path("/e"), {"session": "s"}).check()
        self.assertIn("セッション s の", str(cm.exception))

    def test_launch_hint_quotes(self):
        hint = self.herdr.create(Path("/e"), {}).launch_hint({"name": "a", "cwd": "/a b", "argv": ["--x", "y z"]})
        self.assertEqual(hint, "cd '/a b' && claude --x 'y z'")


class FindEnvTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("HUB_ENV", "HUB_BACKEND", "HERDR_SESSION"):
            os.environ.pop(k, None)
        self.envs_file = self.root / "envs"
        envs_patch = mock.patch.object(hublib, "ENVS_FILE", self.envs_file)
        envs_patch.start()
        self.addCleanup(envs_patch.stop)
        self.a = self.make_env("a", 'session = "sa"\n')
        self.b = self.make_env("b", 'session = "sb"\n')
        self.envs_file.write_text(f"{self.a}\n{self.b}\n")
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.root)

    def make_env(self, name, toml):
        env = self.root / name
        (env / "hub-env").mkdir(parents=True)
        (env / "hub-env/hub.toml").write_text(toml)
        return env

    def test_hub_env_var(self):
        os.environ["HUB_ENV"] = str(self.b)
        os.environ["HERDR_SESSION"] = "sa"
        self.assertEqual(hublib.find_env()[0], self.b)

    def test_hub_env_var_without_config(self):
        os.environ["HUB_ENV"] = str(self.root)
        with self.assertRaises(hublib.HubError):
            hublib.find_env()

    def test_backend_session(self):
        os.environ["HERDR_SESSION"] = "sb"
        env, cfg = hublib.find_env()
        self.assertEqual((env, cfg["session"]), (self.b, "sb"))

    def test_two_envs_in_one_session(self):
        (self.b / "hub-env/hub.toml").write_text('session = "sa"\n')
        os.environ["HERDR_SESSION"] = "sa"
        with self.assertRaises(hublib.HubError):
            hublib.find_env()

    def test_cwd_inside_env(self):
        deep = self.a / ".hub-worktrees/w/app"
        deep.mkdir(parents=True)
        os.chdir(deep)
        self.assertEqual(hublib.find_env()[0], self.a)

    def test_unknown_session_falls_back_to_cwd(self):
        os.environ["HERDR_SESSION"] = "nope"
        os.chdir(self.b / "hub-env")
        self.assertEqual(hublib.find_env()[0], self.b)

    def test_nowhere(self):
        with self.assertRaises(hublib.HubError):
            hublib.find_env()


if __name__ == "__main__":
    unittest.main()
