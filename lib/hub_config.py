"""hub.toml のうち、backend によらない部分（エージェントの定義）を読む。

hub.toml は [[agent]] にエージェントを並べ、backend ごとの設定（herdr のレイアウトなど）は別の表に置く。
旧形式（[[workspace]] のペインに agent を書く形）は読まず、contrib/herdr/migrate-hub-toml での書き換えを案内する。
"""

from __future__ import annotations

import re
from pathlib import Path

from hublib import HUB_ROOT, HubError

COMMANDER = "commander"
# 司令塔のセッションを記録するファイルの名前（環境の state ディレクトリの中）
SESSION_RECORD = f"{COMMANDER}-session.json"
AGENT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
LAUNCHERS = (None, "setup")


def is_legacy(cfg: dict) -> bool:
    """[[agent]] が無く、旧形式の session か [[workspace]] があれば旧形式。"""
    return "agent" not in cfg and ("session" in cfg or "workspace" in cfg)


def check_format(cfg: dict) -> None:
    if is_legacy(cfg):
        raise HubError("hub.toml が旧形式です（[[agent]] がありません）。次のコマンドで新形式に書き換えてください:\n"
                       f"  {HUB_ROOT / 'contrib/herdr/migrate-hub-toml'} <hub-env>/hub.toml --write")


def _agent(env: Path, name: str, cwd: str, kind: str = "claude", args: list | None = None,
           launched_by: str | None = None) -> dict:
    return {"name": name, "cwd": cwd, "path": (env / cwd).resolve(), "kind": kind, "args": list(args or []),
            "launched_by": launched_by}


def agent_defs(env: Path, cfg: dict) -> list[dict]:
    """エージェントの定義: [{"name", "cwd"（環境からの相対）, "path"（絶対）, "kind", "args", "launched_by"}]。"""
    check_format(cfg)
    entries = cfg.get("agent", [])
    if not isinstance(entries, list):
        raise HubError("hub.toml の agent は [[agent]] の表で書いてください")
    out: list[dict] = []
    seen: set[str] = set()
    for i, a in enumerate(entries):
        where = f"hub.toml の {i + 1} 番目の [[agent]]"
        name, cwd = a.get("name"), a.get("cwd")
        if not isinstance(name, str) or not AGENT_NAME_RE.fullmatch(name):
            raise HubError(f"{where}: name が無いか、使えない文字があります")
        if name in seen:
            raise HubError(f"{where}: name {name} が重複しています")
        seen.add(name)
        if not isinstance(cwd, str) or not cwd:
            raise HubError(f"{where}（{name}）: cwd がありません")
        args = a.get("args", [])
        if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
            raise HubError(f"{where}（{name}）: args は文字列の配列にしてください")
        kind = a.get("kind", "claude")
        if not isinstance(kind, str) or not kind:
            raise HubError(f"{where}（{name}）: kind が不正です")
        if a.get("launched_by") not in LAUNCHERS:
            raise HubError(f"{where}（{name}）: launched_by は \"setup\" だけ書けます")
        unknown = sorted(set(a) - {"name", "cwd", "args", "kind", "launched_by"})
        if unknown:
            raise HubError(f"{where}（{name}）: 知らないキーです: {', '.join(unknown)}")
        agent = _agent(env, name, cwd, kind, args, a.get("launched_by"))
        if name == COMMANDER and agent["path"] != (env / "hub-env").resolve():
            raise HubError(f"{COMMANDER} は司令塔の名前です。cwd は hub-env にしてください")
        out.append(agent)
    return out


def find_agent(env: Path, cfg: dict, name: str) -> dict:
    for a in agent_defs(env, cfg):
        if a["name"] == name:
            return a
    raise HubError(f"hub.toml にエージェント {name} がありません")
