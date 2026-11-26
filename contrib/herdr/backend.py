"""hub の backend の herdr 実装。場所は herdr のワークスペース、置き場はタブのペイン。"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from hub_backend import Backend, BackendError
from hub_config import agent_defs
from hublib import HubError


class HerdrError(BackendError):
    pass


def herdr(*args: str, check: bool = True) -> dict:
    p = subprocess.run(["herdr", *args], capture_output=True, text=True)
    out: dict = {}
    # NOTE: herdr はサーバーのエラーを stderr に JSON で返す。
    for stream in (p.stdout, p.stderr):
        try:
            parsed = json.loads(stream) if stream.strip() else {}
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed:
            out = parsed
            break
    if check and (p.returncode != 0 or "error" in out):
        err = out.get("error") or {}
        msg = err.get("message") or p.stderr.strip() or p.stdout.strip()
        raise HerdrError(f"herdr {' '.join(args)}: {msg}", err.get("code"))
    return out


def workspaces() -> list[dict]:
    return herdr("workspace", "list")["result"]["workspaces"]


def live_agents() -> list[dict]:
    return herdr("agent", "list")["result"]["agents"]


def session_of(cfg: dict) -> str | None:
    return (cfg.get("herdr") or {}).get("session")


def detect(env: Path, cfg: dict) -> bool:
    current = os.environ.get("HERDR_SESSION")
    return bool(current) and current == session_of(cfg)


def layout(env: Path, cfg: dict) -> list[dict]:
    """[herdr] の [[herdr.workspace]] を、ペインの agent = "<name>" を [[agent]] の定義（{name, kind, args}）に置き換えて返す。

    ペインの cwd は、書かれていなければエージェントの cwd にする。
    """
    defs = {a["name"]: a for a in agent_defs(env, cfg)}
    wss = copy.deepcopy((cfg.get("herdr") or {}).get("workspace") or [])
    for ws in wss:
        label = ws.get("label")
        for name in ws.get("agents", []):
            if name not in defs:
                raise HubError(f"[[herdr.workspace]] {label} の agents の {name} が [[agent]] にありません")
        for tab in ws.get("tab", []):
            for pane in tab.get("pane", []):
                if "agent" not in pane:
                    continue
                ref = pane["agent"]
                if not isinstance(ref, str):
                    raise HubError(f"ワークスペース {label} のペイン: agent には [[agent]] の name を書いてください")
                if ref not in defs:
                    raise HubError(f"ワークスペース {label} のペイン: agent の {ref} が [[agent]] にありません")
                d = defs[ref]
                if d["launched_by"] == "setup":
                    raise HubError(f"ワークスペース {label} のペイン: {ref} は launched_by = \"setup\" です")
                if pane.get("cwd") and (env / pane["cwd"]).resolve() != d["path"]:
                    raise HubError(f"ワークスペース {label} のペイン: cwd が {ref} の cwd と違います")
                pane["cwd"] = d["cwd"]
                pane["agent"] = {"name": d["name"], "kind": d["kind"], "args": d["args"]}
    return wss


def create(env: Path, cfg: dict) -> "HerdrBackend":
    return HerdrBackend(env, cfg)


def _worktree_info(ws: dict) -> dict:
    return ws.get("worktree") or {}


def _has_linked(info: dict, every: list[dict]) -> bool:
    """info が元の checkout のワークスペースで、同じリポジトリの worktree のワークスペースがつながっているか。"""
    return bool(info) and not info.get("is_linked_worktree") and any(
        _worktree_info(o).get("repo_key") == info.get("repo_key") and _worktree_info(o).get("is_linked_worktree")
        for o in every)


class HerdrBackend(Backend):
    name = "herdr"
    # NOTE: 作成直後のペインはシェルが起動しきっておらず、agent_pane_busy で弾かれる。それを待つ秒数。
    START_RETRY = 30.0
    SEND_TIMEOUT = 30

    def check(self) -> None:
        if not detect(self.env, self.cfg):
            current = os.environ.get("HERDR_SESSION")
            where = f"セッション {current}" if current else "herdr の外"
            raise BackendError(f"herdr のセッション {session_of(self.cfg)} のペインで実行してください（今は{where}）")

    # ── 一覧 ──

    def list_agents(self) -> list[dict]:
        return [{"name": a.get("name"), "status": a.get("agent_status"), "cwd": a.get("cwd"),
                 "place": a.get("workspace_id")} for a in live_agents()]

    def list_places(self) -> list[dict]:
        return [{"id": w["workspace_id"], "label": w["label"]} for w in workspaces()]

    def current_place(self) -> str | None:
        here = os.environ.get("HERDR_WORKSPACE_ID")
        if not here:
            return None
        return next((w["label"] for w in workspaces() if w["workspace_id"] == here), None)

    # ── 組み立て ──

    def open_place(self, label: str, cwd: Path, title: str, worktree: dict | None = None) -> dict:
        data: dict = {}
        if worktree:
            before = {w["workspace_id"] for w in workspaces()}
            res = herdr("worktree", "open", "--cwd", str(worktree["repo"]), "--path", str(worktree["path"]),
                        "--label", label, "--no-focus")["result"]
            # NOTE: 元のリポジトリのワークスペースが無いと、herdr が親を自動で作る。close_place で片付けるために記録する。
            for w in workspaces():
                if w["workspace_id"] not in before and w["workspace_id"] != res["workspace"]["workspace_id"]:
                    data["source"] = {"id": w["workspace_id"], "label": w["label"]}
        else:
            res = herdr("workspace", "create", "--cwd", str(cwd), "--label", label, "--no-focus")["result"]
        herdr("tab", "rename", res["tab"]["tab_id"], title)
        return {"place": res["workspace"]["workspace_id"], "pane": res["root_pane"]["pane_id"], "data": data}

    def add_slot(self, place: str, cwd: Path, title: str) -> str:
        res = herdr("tab", "create", "--workspace", place, "--cwd", str(cwd), "--label", title, "--no-focus")["result"]
        return res["root_pane"]["pane_id"]

    def start_agent(self, pane: str, spec: dict) -> str:
        cmd = ["agent", "start", spec["name"], "--kind", spec.get("kind", "claude"), "--pane", pane]
        if spec.get("argv"):
            cmd += ["--", *spec["argv"]]
        deadline = time.monotonic() + self.START_RETRY
        while True:
            try:
                herdr(*cmd)
                return "started"
            except HerdrError as e:
                # 起動はしたが確認ダイアログ（初回のフォルダ信頼など）で止まっている
                if e.code == "agent_not_ready":
                    return "needs_confirm"
                if e.code != "agent_pane_busy" or time.monotonic() > deadline:
                    raise
                time.sleep(0.5)

    # ── やり取り ──

    def send(self, name: str, text: str) -> str:
        # NOTE: herdr() はエラーに引数を含めるので、本文を出さないよう直接呼ぶ。
        try:
            p = subprocess.run(["herdr", "agent", "prompt", name, text], capture_output=True, text=True,
                               timeout=self.SEND_TIMEOUT)
        except subprocess.TimeoutExpired:
            return "timeout"
        except OSError:
            return "herdr_failed"
        code = None
        for stream in (p.stdout, p.stderr):
            try:
                parsed = json.loads(stream) if stream.strip() else {}
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                code = parsed["error"].get("code")
                break
        if p.returncode == 0 and code is None:
            return "ok"
        return {"agent_blocked": "blocked"}.get(code, code or "prompt_failed")

    def read(self, name: str, lines: int = 40) -> str:
        p = subprocess.run(["herdr", "agent", "read", name, "--source", "recent-unwrapped", "--lines", str(lines)],
                           capture_output=True, text=True)
        return p.stdout if p.returncode == 0 else ""

    # ── 検査 ──

    def layout_dirs(self) -> list[Path]:
        try:
            return [(self.env / ws["cwd"]).resolve() for ws in layout(self.env, self.cfg) if ws.get("cwd")]
        except HubError:
            return []

    def check_layout(self, work_labels: set[str]) -> list[str]:
        try:
            wss = layout(self.env, self.cfg)
        except HubError as e:
            return [f"hub.toml: {e}"]
        out: list[str] = []
        for ws in wss:
            d = (self.env / ws.get("cwd", "")).resolve()
            if not d.is_dir():
                out.append(f"hub.toml: ワークスペース {ws.get('label')} の cwd がありません: {d}")
            if ws.get("setup") and not (self.env / ws["setup"]).is_file():
                out.append(f"hub.toml: ワークスペース {ws.get('label')} の setup がありません: {self.env / ws['setup']}")
        known = {ws.get("label") for ws in wss} | work_labels
        for w in workspaces():
            if w["label"] not in known:
                out.append(f"ワークスペース {w['label']}: hub.toml にも作業にもありません")
        return out

    # ── 片付け ──

    def can_close(self, labels: set[str], worktree_labels: set[str]) -> list[str]:
        every = workspaces()
        problems: list[str] = []
        for label in sorted(labels):
            hits = [w for w in every if w["label"] == label]
            if len(hits) > 1:
                problems.append(f"ラベル {label} のワークスペースが {len(hits)} 個あり、どれか決められません")
            elif hits and label not in worktree_labels and _has_linked(_worktree_info(hits[0]), every):
                problems.append(f"{label} には他の worktree のワークスペースがつながっているので閉じられません。"
                                "他の作業を巻き込むため、先にそちらを finish してください")
        return problems

    def close_place(self, label: str, worktree: dict | None = None) -> bool:
        ws = next((w for w in workspaces() if w["label"] == label), None)
        if ws:
            if worktree:
                herdr("worktree", "remove", "--workspace", ws["workspace_id"])
            else:
                herdr("workspace", "close", ws["workspace_id"])
        if worktree:
            self._close_auto_created(worktree)
        return ws is not None

    def _close_auto_created(self, worktree: dict) -> None:
        """herdr が worktree のために自動で作った親を閉じる。使われている形跡があれば残す。"""
        src = worktree.get("source")
        if not src:
            return
        every = workspaces()
        ws = next((w for w in every if w["workspace_id"] == src["id"] and w["label"] == src["label"]), None)
        if not ws:
            return
        key = _worktree_info(ws).get("repo_key")
        linked = any(_worktree_info(o).get("repo_key") == key and _worktree_info(o).get("is_linked_worktree")
                     for o in every)
        has_agent = any(x.get("workspace_id") == ws["workspace_id"] for x in live_agents())
        if linked or has_agent or ws.get("pane_count") != 1 or ws.get("tab_count") != 1:
            print(f"note: 自動で作られた {src['label']} は使われているので残しました", file=sys.stderr)
            return
        herdr("workspace", "close", ws["workspace_id"])
        print(f"自動で作られたワークスペースを閉じました: {src['label']}")
