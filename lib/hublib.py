"""hub のコマンドが共有する処理（環境の特定・エージェントの起動引数・作業文書の読み書き）。backend の呼び出しは hub_backend にある。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
import tomllib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ENVS_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "hub/envs"
HUB_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "hub"
DEFAULT_TODO_FILE = "todo/data/active.md"
FRONT_MATTER_KEYS = ("state", "todo", "repos", "workspace", "agents", "worktrees")
STATES = ("active", "blocked", "done")


class HubError(Exception):
    pass


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
    """環境を特定する。HUB_ENV → backend の中（herdr ならセッション）→ カレントディレクトリ の順に試す。"""
    from hub_backend import detect  # NOTE: hub_backend が HubError を import するので、循環を避けて中で読む。

    if explicit := os.environ.get("HUB_ENV"):
        env = Path(explicit).expanduser().resolve()
        if (cfg := read_config(env)) is None:
            raise HubError(f"HUB_ENV の環境の hub.toml を読めません: {env / 'hub-env/hub.toml'}")
        return _found(env, cfg)
    envs = [(e, c) for e in registered_envs() if (c := read_config(e)) is not None]
    hits = [(e, c) for e, c in envs if detect(e, c)]
    if len(hits) > 1:
        raise HubError(f"このセッションの環境が {len(hits)} 個あります（{ENVS_FILE}）")
    if hits:
        return _found(*hits[0])
    cwd = Path.cwd().resolve()
    hits = [(e, c) for e, c in envs if cwd == e.resolve() or e.resolve() in cwd.parents]
    if len(hits) == 1:
        return _found(*hits[0])
    raise HubError(f"環境を特定できません（{ENVS_FILE}）。hub.toml の session に書いたセッションのペインか環境の中で実行するか、"
                   "HUB_ENV に環境のパスを指定してください")


def _found(env: Path, cfg: dict) -> tuple[Path, dict]:
    from hub_config import check_format  # NOTE: hub_config が HubError を import するので、循環を避けて中で読む。

    check_format(cfg)
    return env, cfg


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


ROLES = ("commander", "lead", "crew")


def role_file(role: str) -> Path:
    return HUB_ROOT / "roles" / f"{role}.md"


def _replace(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # NOTE: 起動中の別のエージェントが読みかけのファイルを見ないよう、置き換えで更新する。
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)


def build_rules(env: Path, role: str | None = None) -> Path | None:
    """役割のファイル（role があれば）と rules を連結した生成ファイルを state ディレクトリに作る。どちらも無ければ None。

    role が無いときは agent-rules.md（常駐エージェント用）、あるときは <role>-prompt.md。
    """
    if role is not None and role not in ROLES:
        raise HubError(f"役割が不正です: {role}")
    target = env_state_dir(env) / (f"{role}-prompt.md" if role else "agent-rules.md")
    files = ([(f"hub/roles/{role}.md", role_file(role))] if role else []) + rule_files(env)
    if not files:
        target.unlink(missing_ok=True)
        return None
    try:
        text = "\n\n".join(f"<!-- {name} -->\n{f.read_text().strip()}" for name, f in files) + "\n"
    except (OSError, UnicodeDecodeError) as e:
        raise HubError(f"{'役割のファイルか ' if role else ''}rules を読めません: {e}")
    _replace(target, text)
    return target


def agent_settings(env: Path) -> dict | None:
    """<hub-env>/agent-settings.json の中身。無ければ None。壊れていれば HubError。"""
    settings = env / "hub-env/agent-settings.json"
    if not settings.exists():
        return None
    try:
        data = json.loads(settings.read_text())
        if not isinstance(data, dict):
            raise ValueError("オブジェクトではありません")
    except (OSError, ValueError) as e:
        raise HubError(f"agent-settings.json を読めません: {settings}: {e}")
    return data


def claude_launch_args(env: Path, role: str | None = None) -> list[str]:
    """環境のエージェントに渡す claude の引数。設定が壊れていれば、何かを閉じる前に止まれるよう例外にする。"""
    args: list[str] = []
    if agent_settings(env) is not None:
        args += ["--settings", str(env / "hub-env/agent-settings.json")]
    if rules := build_rules(env, role):
        args += ["--append-system-prompt-file", str(rules)]
    return args


def settings_with_hooks(env: Path, target: Path, hooks: dict[str, list]) -> Path:
    """agent-settings.json にフックを足した設定を target に生成する。"""
    settings = agent_settings(env) or {}
    have = settings.setdefault("hooks", {})
    if not isinstance(have, dict):
        raise HubError(f"agent-settings.json の hooks を読めません: {env / 'hub-env/agent-settings.json'}")
    for event, entries in hooks.items():
        if not isinstance(have.setdefault(event, []), list):
            raise HubError(f"agent-settings.json の hooks.{event} を読めません")
        have[event].extend(entries)
    _replace(target, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    return target


def replace_settings(launch: list[str], settings: Path) -> list[str]:
    """launch の --settings を settings に差し替える（無ければ足す）。"""
    rest: list[str] = []
    skip = False
    for a in launch:
        if skip:
            skip = False
        elif a == "--settings":
            skip = True
        else:
            rest.append(a)
    return ["--settings", str(settings), *rest]


def agent_argv(kind: str, launch: list[str], own: list[str], extra: list[str] | None = None) -> list[str]:
    """agent の args に hub の引数を足す。--add-dir などは後ろの語を飲み込むので、hub の分は後ろに置く。"""
    if kind != "claude":
        return list(own)
    for flag in (a for a in launch if a.startswith("--")):
        if any(a == flag or a.startswith(f"{flag}=") for a in own):
            raise HubError(f"agent の args の {flag} は hub が渡す引数と衝突します。rules か agent-settings.json に移してください")
    return [*own, *launch, *(extra or [])]


# ── 司令塔のセッションの記録と再開 ─────────────────────────────────

SESSION_FLAGS = ("--resume", "-r", "--continue", "-c", "--session-id", "--fork-session")


def has_flag(args: list[str], *flags: str) -> bool:
    return any(a == f or a.startswith(f"{f}=") for a in args for f in flags)


def commander_launch(env: Path, agent: dict, cwd: Path, launch: list[str], record: Path) -> list[str] | None:
    """agent-settings.json にセッション記録のフックを足した設定を生成し、司令塔に launch の代わりに渡す引数を返す。

    claude でないか、agent の args に --settings があれば記録できないので None。
    """
    if agent.get("kind", "claude") != "claude" or has_flag(agent.get("args", []), "--settings"):
        return None
    script = HUB_ROOT / "scripts/hub-session-record"
    command = shlex.join([str(script), "--state", str(record), "--cwd", str(cwd)])
    target = record.parent / record.name.replace("-session.json", "-agent-settings.json")
    settings_with_hooks(env, target, {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]})
    return replace_settings(launch, target)


def commander_resume_args(name: str, agent: dict, cwd: Path, record: Path) -> list[str]:
    """記録と履歴を確かめて、司令塔を同じセッションで起動し直す claude の引数を返す。問題があれば HubError。"""
    if agent.get("kind", "claude") != "claude":
        raise HubError(f"--resume は claude の {name} だけで使えます")
    if has_flag(agent.get("args", []), "--settings"):
        raise HubError(f"{name} の args に --settings があるとセッションを記録できないので、--resume を使えません")
    if has_flag(agent.get("args", []), *SESSION_FLAGS):
        raise HubError(f"{name} の args のセッション指定は --resume と同時に使えません")
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        sid, transcript, recorded_cwd = data["session_id"], data["transcript_path"], data["cwd"]
        if str(uuid.UUID(sid)) != sid or not isinstance(transcript, str) or not isinstance(recorded_cwd, str):
            raise ValueError
    except FileNotFoundError:
        raise HubError(f"{name} のセッション ID が記録されていません: {record}。"
                       "一度 --resume なしで起動すると記録されます")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise HubError(f"{name} のセッションの記録を読めません: {record}")
    if Path(recorded_cwd) != cwd:
        raise HubError(f"記録されたセッションの cwd が hub.toml と違います: {recorded_cwd}")
    if Path(transcript).name != f"{sid}.jsonl" or not Path(transcript).is_file():
        raise HubError(f"セッション {sid} の履歴が見つかりません: {transcript}")
    return ["--resume", sid]


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
