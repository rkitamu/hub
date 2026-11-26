"""エージェントを置いて動かす仕組み（backend）の呼び出し口。

core（scripts/ と lib/）は、ここに定義した操作だけで backend を使う。backend の実装は
<hub>/contrib/<名前>/backend.py に置き、モジュールの create(env, cfg) が Backend を返す。

用語:
- 場所（place）: backend がエージェントを置く単位。herdr ではワークスペース。status.md の workspace はそのラベル。
- 置き場（pane）: 場所の中で、エージェントを 1 人起動できるところ。herdr ではタブ（の最初のペイン）。
- タブ（tab）: 置き場を持つ、名前の付いた単位。作業の lead・crew は、場所の中のタブとして置く。
- AgentSpec: {"name", "kind", "cwd", "argv", "env"}。argv は hub の引数を足し終えた claude の引数で、backend は加工しない。
  env はエージェントに渡す環境変数（HUB_WORK・HUB_ROLE）。backend は置き場を作るときに渡す。
"""

from __future__ import annotations

import importlib.util
import os
import re
import shlex
from pathlib import Path
from types import ModuleType

from hublib import HubError

HUB_ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"[a-z][a-z0-9_-]*")


class BackendError(HubError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class Backend:
    """backend の操作の一覧。実装はこれを継承し、使う操作を上書きする。"""

    name = "?"
    # True なら、エージェントを起動も一覧もできない（起動コマンドを人に示すだけ）
    manual = False

    def __init__(self, env: Path, cfg: dict):
        self.env = env
        self.cfg = cfg

    def check(self) -> None:
        """使えなければ BackendError。何かを作る前に呼ぶ。"""

    def list_agents(self) -> list[dict]:
        """動いているエージェント: [{"name", "status", "cwd", "place"}]。"""
        raise NotImplementedError

    def list_places(self) -> list[dict]:
        """場所: [{"id", "label"}]。"""
        raise NotImplementedError

    def current_place(self) -> str | None:
        """このプロセスがいる場所のラベル。場所の外なら None。"""
        return None

    def open_place(self, label: str, cwd: Path, title: str, worktree: dict | None = None,
                   env: dict | None = None) -> dict:
        """場所を作り、{"place": <id>, "pane": <最初の置き場>, "tab": <最初のタブ>, "data": {...}} を返す。

        worktree は {"repo", "path", "branch"}（旧形式の作業だけが使う）。checkout は core が作成済み。
        data は backend 用の値で、core は worktree の記録に足して保存し、close_place に返す。
        env は最初の置き場に渡す環境変数。
        """
        raise NotImplementedError

    def add_slot(self, place: str, cwd: Path, title: str, env: dict | None = None) -> str:
        """既存の場所（id）に、title という名前のタブで置き場を足し、その id を返す。"""
        raise NotImplementedError

    def list_tabs(self, label: str) -> list[dict]:
        """場所（ラベル）のタブ: [{"id", "title"}]。場所が無ければ空。"""
        return []

    def close_tab(self, tab: str) -> None:
        """タブを閉じる。"""

    def current_tab(self) -> str | None:
        """このプロセスがいるタブの id。タブの外なら None。"""
        return None

    def close_tab_later(self, tab: str) -> None:
        """このプロセスが終わった後に、タブを裏で閉じる（自分のタブを閉じるとき）。"""

    def place_for_dir(self, path: Path) -> str | None:
        """そのディレクトリを受け持つ、レイアウトの場所のラベル。無ければ None。"""
        return None

    def start_agent(self, pane: str, spec: dict) -> str:
        """起動して "started" を返す。確認ダイアログで止まっていれば "needs_confirm"、起動できない backend は "manual"。失敗は BackendError。"""
        raise NotImplementedError

    def send(self, name: str, text: str) -> str:
        """送って "ok" / "blocked" / "timeout" / その他のエラーコードを返す。text はエラーやログに出さない。"""
        raise NotImplementedError

    def read(self, name: str, lines: int = 40) -> str:
        """エージェントの画面の文字列。読めなければ空文字。"""
        return ""

    def can_close(self, labels: set[str], worktree_labels: set[str]) -> list[str]:
        """閉じると困る理由の一覧。空なら閉じてよい。"""
        return []

    def close_place(self, label: str, worktree: dict | None = None) -> bool:
        """場所を閉じ、閉じたら True。worktree は記録（data を含む）。checkout が残れば core が消す。"""
        raise NotImplementedError

    def layout_dirs(self) -> list[Path]:
        """backend のレイアウトが使うディレクトリ（hub-status・hub-check がリポジトリとして数える）。"""
        return []

    def check_layout(self, work_labels: set[str]) -> list[str]:
        """backend のレイアウトと実際の状態のずれ（hub-check が出す）。work_labels は作業が記録した場所のラベル。"""
        return []

    def launch_hint(self, spec: dict) -> str:
        """人が手で起動するためのコマンド。"""
        env = [f"{k}={v}" for k, v in (spec.get("env") or {}).items()]
        cmd = [*(["env", *env] if env else []), spec.get("kind", "claude"), *spec.get("argv", [])]
        return f"cd {shlex.quote(str(spec['cwd']))} && {shlex.join(cmd)}"


class NoneBackend(Backend):
    """backend なし。場所もエージェントも作らず、起動コマンドを人に示す。"""

    name = "none"
    manual = True

    def list_agents(self) -> list[dict]:
        return []

    def list_places(self) -> list[dict]:
        return []

    def open_place(self, label: str, cwd: Path, title: str, worktree: dict | None = None,
                   env: dict | None = None) -> dict:
        return {"place": label, "pane": label, "tab": label, "data": {}}

    def add_slot(self, place: str, cwd: Path, title: str, env: dict | None = None) -> str:
        return place

    def start_agent(self, pane: str, spec: dict) -> str:
        return "manual"

    def send(self, name: str, text: str) -> str:
        return "no_backend"

    def close_place(self, label: str, worktree: dict | None = None) -> bool:
        return False


def load_module(name: str) -> ModuleType:
    if not NAME_RE.fullmatch(name):
        raise HubError(f"backend の名前が不正です: {name}")
    path = HUB_ROOT / "contrib" / name / "backend.py"
    if not path.is_file():
        raise HubError(f"backend {name} がありません: {path}")
    key = f"hub_backend_{name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def backend_name(cfg: dict) -> str:
    if name := os.environ.get("HUB_BACKEND"):
        return name
    if name := cfg.get("backend"):
        return name
    return "none"


def detect(env: Path, cfg: dict) -> bool:
    """このプロセスが、その環境の backend の中で動いているか。"""
    name = backend_name(cfg)
    if name == "none":
        return False
    try:
        return bool(load_module(name).detect(env, cfg))
    except HubError:
        return False


def backend_for(env: Path, cfg: dict) -> Backend:
    name = backend_name(cfg)
    if name == "none":
        return NoneBackend(env, cfg)
    return load_module(name).create(env, cfg)
