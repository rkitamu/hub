"""hub のコマンドが共有する処理（herdr の呼び出し・環境の特定・作業文書の読み書き）。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ENVS_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "hub/envs"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "hub"
DEFAULT_TODO_FILE = "todo/data/active.md"
FRONT_MATTER_KEYS = ("state", "todo", "repos", "workspace", "agents", "worktrees")
STATES = ("active", "blocked", "done")


class HubError(Exception):
    pass


class HerdrError(HubError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


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


# ── 環境 ───────────────────────────────────────────────────────


def registered_envs() -> list[Path]:
    if not ENVS_FILE.exists():
        return []
    return [Path(l) for l in ENVS_FILE.read_text().split() if l]


def read_config(env: Path) -> dict | None:
    try:
        return tomllib.loads((env / "hub-env/hub.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None


def find_env() -> tuple[Path, dict]:
    session = os.environ.get("HERDR_SESSION")
    if not session:
        raise HubError("HERDR_SESSION がありません。hub.toml の session に書いたセッションのペインで実行してください")
    hits = [(e, c) for e in registered_envs() if (c := read_config(e)) and c.get("session") == session]
    if len(hits) != 1:
        raise HubError(f"session '{session}' の環境が {len(hits)} 個あります（{ENVS_FILE}）")
    return hits[0]


def work_dir(env: Path) -> Path:
    return (env / "hub-env/work").resolve()


def todo_file(env: Path, cfg: dict) -> Path:
    return (env / cfg.get("todo_file", DEFAULT_TODO_FILE)).resolve()


# ── エージェントの起動引数 ───────────────────────────────────────


def env_state_dir(env: Path) -> Path:
    env = env.resolve()
    return STATE_DIR / "envs" / f"{env.name}-{hashlib.sha256(str(env).encode()).hexdigest()[:10]}"


def rule_files(env: Path) -> list[tuple[str, Path]]:
    """<hub-env>/rules のファイルを名前順で返す。"""
    return [(f"hub-env/rules/{f.name}", f) for f in sorted((env / "hub-env/rules").glob("*.md")) if f.is_file()]


def build_rules(env: Path) -> Path | None:
    """rules を連結した生成ファイルを state ディレクトリに作る。rules が無ければ None。"""
    target = env_state_dir(env) / "agent-rules.md"
    files = rule_files(env)
    if not files:
        target.unlink(missing_ok=True)
        return None
    try:
        text = "\n\n".join(f"<!-- {name} -->\n{f.read_text().strip()}" for name, f in files) + "\n"
    except (OSError, UnicodeDecodeError) as e:
        raise HubError(f"rules を読めません: {e}")
    target.parent.mkdir(parents=True, exist_ok=True)
    # NOTE: 起動中の別のエージェントが読みかけのファイルを見ないよう、置き換えで更新する。
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".agent-rules.")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, target)
    return target


def claude_launch_args(env: Path) -> list[str]:
    """環境のエージェント全員に渡す claude の引数。設定が壊れていれば、何かを閉じる前に止まれるよう例外にする。"""
    args: list[str] = []
    settings = env / "hub-env/agent-settings.json"
    if settings.exists():
        try:
            if not isinstance(json.loads(settings.read_text()), dict):
                raise ValueError("オブジェクトではありません")
        except (OSError, ValueError) as e:
            raise HubError(f"agent-settings.json を読めません: {settings}: {e}")
        args += ["--settings", str(settings)]
    if rules := build_rules(env):
        args += ["--append-system-prompt-file", str(rules)]
    return args


def agent_argv(kind: str, launch: list[str], own: list[str], extra: list[str] | None = None) -> list[str]:
    """agent の args に hub の引数を足す。--add-dir などは後ろの語を飲み込むので、hub の分は後ろに置く。"""
    if kind != "claude":
        return list(own)
    for flag in (a for a in launch if a.startswith("--")):
        if any(a == flag or a.startswith(f"{flag}=") for a in own):
            raise HubError(f"agent の args の {flag} は hub が渡す引数と衝突します。rules か agent-settings.json に移してください")
    return [*own, *launch, *(extra or [])]


# ── Claude Code のフォルダ信頼 ───────────────────────────────────


def claude_config_file() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home()) / ".claude.json"


@contextmanager
def claude_config(timeout: float = 5.0) -> Iterator[tuple[dict, dict]]:
    """設定をロックして読み、(全体, projects) を渡す。例外が無ければ置き換えで書き戻す。"""
    file = claude_config_file().resolve()
    # NOTE: Claude Code と同じ <file>.lock ディレクトリで排他する。古いロックは奪わず、失敗として扱う。
    lock = file.with_name(file.name + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise HubError(f"{file} のロックを取れません")
            time.sleep(0.1)
        except OSError as e:
            raise HubError(f"{file} のロックを取れません: {e}")
    try:
        try:
            data = json.loads(file.read_text())
        except (OSError, ValueError) as e:
            raise HubError(f"{file} を読めません: {e}")
        projects = data.get("projects") if isinstance(data, dict) else None
        if not isinstance(projects, dict):
            raise HubError(f"{file} に projects がありません")
        before = json.dumps(data, sort_keys=True)
        yield data, projects
        if json.dumps(data, sort_keys=True) == before:
            return
        fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=".claude.json.hub.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, file.stat().st_mode & 0o777)
            os.replace(tmp, file)
        except OSError as e:
            Path(tmp).unlink(missing_ok=True)
            raise HubError(f"{file} を書けません: {e}")
    finally:
        lock.rmdir()


def is_trusted(projects: dict, path: Path) -> bool:
    """Claude Code と同じく、そのフォルダか親が信頼済みなら True。"""
    return any(isinstance(e := projects.get(str(p)), dict) and e.get("hasTrustDialogAccepted") is True
               for p in (path, *path.parents))


def trust_worktree(source: Path, tree: Path) -> bool:
    """元のリポジトリが信頼済みなら worktree を登録する。新しく登録したら True。"""
    with claude_config() as (_, projects):
        if is_trusted(projects, tree):
            return False
        if not is_trusted(projects, source):
            raise HubError(f"元のリポジトリが信頼済みではありません: {source}")
        entry = projects.setdefault(str(tree), {})
        if not isinstance(entry, dict):
            raise HubError(f"{tree} の設定を読めません")
        entry["hasTrustDialogAccepted"] = True
        return True


def untrust_worktree(tree: Path) -> None:
    with claude_config() as (_, projects):
        if isinstance(entry := projects.get(str(tree)), dict):
            entry.pop("hasTrustDialogAccepted", None)


# ── git ────────────────────────────────────────────────────────


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise HubError(f"git {' '.join(args)} ({cwd}): {p.stderr.strip()}")
    return p


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def dirty_files(path: Path) -> list[str]:
    return [l for l in git(path, "status", "--porcelain").stdout.splitlines() if l]


# ── status.md ──────────────────────────────────────────────────
# front matter の値は JSON で書く（JSON は YAML の部分集合なので、YAML としても読める）。


def parse_status(text: str) -> tuple[dict, str]:
    m = re.match(r"---\n(.*?)\n---\n?(.*)", text, re.S)
    if not m:
        raise HubError("status.md に front matter がありません")
    meta: dict = {}
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise HubError(f"status.md の front matter を読めません: {line}")
        value = value.strip()
        try:
            meta[key.strip()] = json.loads(value)
        except json.JSONDecodeError:
            meta[key.strip()] = value
    missing = [k for k in FRONT_MATTER_KEYS if k not in meta]
    if missing:
        raise HubError(f"status.md の front matter に {', '.join(missing)} がありません")
    if meta["state"] not in STATES:
        raise HubError(f"status.md の state が不正です: {meta['state']}")
    return meta, m.group(2)


def render_status(meta: dict, body: str) -> str:
    lines = []
    for key in FRONT_MATTER_KEYS:
        value = meta[key]
        rendered = value if key == "state" else json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {rendered}")
    return "---\n" + "\n".join(lines) + "\n---\n" + body


def read_status(path: Path) -> tuple[dict, str]:
    return parse_status(path.read_text())


def write_status(path: Path, meta: dict, body: str) -> None:
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(render_status(meta, body))
    tmp.replace(path)
