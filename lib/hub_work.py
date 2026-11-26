"""作業（commander / lead / crew）の記録・worktree・タブ・エージェントを扱う。hub-work・hub-lead・hub-crew が使う。

作業は <hub-env>/work/<作業名>/status.md に記録する。新しい作業は、commander のいる場所に lead のタブを置き、
lead が assign した子リポジトリごとに、その子の場所に crew のタブを置く（agents[] の要素に role と tab がある）。
role の無い記録は旧形式（作業ごとに場所を作る形）で、finish と resume だけができる。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from hub_backend import Backend
from hub_config import COMMANDER, agent_defs
from hublib import (HUB_ROOT, HubError, agent_argv, claude_launch_args, dirty_files, env_state_dir, git, is_git_repo,
                    read_status, replace_settings, settings_with_hooks, trust_worktree, untrust_worktree, write_status)

NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,19}")
AGENT_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
# NOTE: 作業名 data の文書（archive/data/）まで /*/data/ に掛からないよう、打ち消す。
WORK_IGNORE = "# 検証データは git に入れない（hub-work が管理）\n/*/data/\n!/archive/data/\n/archive/*/data/\n"
BODY = "\n# {name}\n\n## 目的\n\n{purpose}\n\n## 進捗\n\n## 結果\n"
WORK_ENV, ROLE_ENV = "HUB_WORK", "HUB_ROLE"
COMMANDS = {None: "hub-work", "lead": "hub-lead", "crew": "hub-crew"}
RESUME_TEXT = "herdr-reset などで中断した作業の再開です。記録（lead は status.md、crew は crew-<repo>.md）の進捗から続けてください。"
# 確認待ちの相手に送り直し続ける秒数と間隔。テストでは環境変数で短くする。
NOTIFY_WAIT = 120.0
NOTIFY_INTERVAL = 5.0


# ── 役割 ───────────────────────────────────────────────────────


def current_role() -> str | None:
    return os.environ.get(ROLE_ENV) or None


def require_role(role: str | None, command: str) -> None:
    """このプロセスの役割（HUB_ROLE）が role でなければ止める。誤操作の防止で、セキュリティの境界ではない。"""
    here = current_role()
    if here == role:
        return
    if here not in COMMANDS:
        raise HubError(f"{ROLE_ENV} が不正です: {here}")
    who = f"{here}" if here else "commander"
    raise HubError(f"{command} は {role or 'commander'} のコマンドです。{who} は {COMMANDS[here]} を使ってください")


def own_work() -> str:
    name = os.environ.get(WORK_ENV)
    if not name or not NAME_RE.fullmatch(name):
        raise HubError(f"担当の作業が分かりません（{WORK_ENV}）。作業のタブで実行してください")
    return name


# ── 共通の小物 ──────────────────────────────────────────────────


def slug(path: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", Path(path).name.lower()).strip("-")


def label_map(backend: Backend) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for w in backend.list_places():
        out.setdefault(w["label"], []).append(w)
    return out


def one_place(labels: dict, label: str) -> dict | None:
    hits = labels.get(label, [])
    if len(hits) > 1:
        raise HubError(f"ラベル {label} のワークスペースが {len(hits)} 個あり、どれか決められません")
    return hits[0] if hits else None


def is_tabbed(meta: dict) -> bool:
    return any(a.get("role") for a in meta["agents"])


def lead_of(meta: dict) -> dict:
    lead = next((a for a in meta["agents"] if a.get("role") == "lead"), None)
    if not lead:
        raise HubError("status.md に lead の記録がありません")
    return lead


def crews_of(meta: dict) -> list[dict]:
    return [a for a in meta["agents"] if a.get("role") == "crew"]


def worktree_of(meta: dict, agent: dict) -> dict | None:
    return next((w for w in meta["worktrees"] if w["repo"] == agent["repo"]), None)


# ── 作業ディレクトリ ─────────────────────────────────────────────


def ensure_work_repo(env: Path, wd: Path) -> None:
    hub_env = (env / "hub-env").resolve()
    if is_git_repo(hub_env):
        rel = wd.relative_to(hub_env).as_posix() + "/"
        if git(hub_env, "check-ignore", "-q", rel, check=False).returncode != 0:
            raise HubError(f"{rel} が hub-env の git から外れていません。hub-env/.gitignore に /{rel} を足してください")
    wd.mkdir(parents=True, exist_ok=True)
    if not is_git_repo(wd):
        git(wd, "init", "-q")
        print(f"作業文書の git リポジトリを初期化しました: {wd}")
    ensure_work_ignore(wd)


def ensure_work_ignore(wd: Path) -> None:
    """data/ を git から外す。既存のリポジトリで追跡済みの data/ も、手元のファイルは残して追跡だけ外す。"""
    ignore = wd / ".gitignore"
    text = ignore.read_text() if ignore.exists() else ""
    if WORK_IGNORE not in text:
        ignore.write_text(text + ("" if not text or text.endswith("\n") else "\n") + WORK_IGNORE)
    tracked = git(wd, "ls-files", "-z", "-ci", "--exclude-standard").stdout.split("\0")
    paths = [f for f in tracked if re.match(r"(archive/[^/]+|[^/]+)/data/", f)]
    for i in range(0, len(paths), 200):
        git(wd, "rm", "-q", "--cached", "--", *paths[i:i + 200])


def commit(wd: Path, message: str) -> None:
    git(wd, "add", "-A")
    if git(wd, "diff", "--cached", "--quiet", check=False).returncode != 0:
        git(wd, "commit", "-q", "-m", message)


def check_name(name: str, wd: Path) -> None:
    if not NAME_RE.fullmatch(name):
        raise HubError("作業名は英小文字で始まる英小文字・数字・ハイフンの 20 文字以内にしてください")
    if name == "archive":
        raise HubError("archive は作業名に使えません")
    if (wd / name).exists():
        raise HubError(f"同名の作業があります: {wd / name}")
    if (wd / "archive" / name).exists():
        raise HubError(f"同名の作業が archive にあります: {wd / 'archive' / name}")


def status_path(wd: Path, name: str) -> Path:
    if not NAME_RE.fullmatch(name) or name == "archive":
        raise HubError(f"作業名が不正です: {name}")
    path = wd / name / "status.md"
    if not path.is_file():
        raise HubError(f"作業がありません: {name}")
    return path


def works_state(env: Path, name: str) -> Path:
    """作業ごとに生成する設定ファイルの置き場所。"""
    return env_state_dir(env) / "works" / name


# ── 起動の引数 ─────────────────────────────────────────────────


def work_access_args(doc_dir: Path) -> list[str]:
    """その作業のディレクトリだけに読み書きを許す claude の引数。"""
    d = str(doc_dir)
    # NOTE: --add-dir は別リポジトリの信頼確認で止まり、work 全体の信頼を求めるので使わない。
    tools = [f"Read(/{d}/**)", f"Edit(/{d}/**)", f"Write(/{d}/**)"]
    return ["--allowedTools", *tools]


def role_launch(env: Path, name: str, role: str, allow: list[Path], key: str) -> list[str]:
    """役割のファイルと rules、書き込みを allow の中に限るフックを渡す claude の引数。"""
    launch = claude_launch_args(env, role)
    cmd = shlex.join([str(HUB_ROOT / "scripts/hub-guard"), *[x for d in allow for x in ("--allow", str(d))]])
    target = works_state(env, name) / f"{key}-settings.json"
    settings_with_hooks(env, target, {"PreToolUse": [
        {"matcher": "Edit|Write|NotebookEdit", "hooks": [{"type": "command", "command": cmd}]}]})
    return replace_settings(launch, target)


def register_trust(env: Path, name: str, wt: dict) -> None:
    """hub-work が作った worktree に限り、元のリポジトリが信頼済みなら信頼済みとして登録する。"""
    source = (env / wt["repo"]).resolve()
    tree = Path(wt["path"]).resolve()
    try:
        common = lambda p: git(p, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        if (tree != (env / ".hub-worktrees" / name / slug(wt["repo"])).resolve() or wt["branch"] != f"work/{name}"
                or git(tree, "symbolic-ref", "--short", "HEAD").stdout.strip() != wt["branch"]
                or common(tree) != common(source)):
            raise HubError(f"hub-work が作った worktree と確認できません: {tree}")
        if trust_worktree(source, tree):
            wt["trusted"] = True
    except HubError as e:
        print(f"warning: worktree を信頼済みにできませんでした: {e}", file=sys.stderr)


def ensure_checkout(wt: dict, repo: Path) -> None:
    """記録の worktree の checkout が無ければ作る。ブランチがあればそれを、無ければ新しく切る。"""
    path = Path(wt["path"])
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{wt['branch']}", check=False).returncode == 0:
        git(repo, "worktree", "add", str(path), wt["branch"])
    else:
        git(repo, "worktree", "add", "-b", wt["branch"], str(path))


def run_agent(backend: Backend, pane: str, spec: dict) -> str:
    result = backend.start_agent(pane, spec)
    # 起動はしたが確認ダイアログ（初回のフォルダ信頼など）で止まっている場合は、ユーザーに任せる
    if result == "needs_confirm":
        print(f"note: {spec['name']} が確認待ちです。そのタブで答えてください", file=sys.stderr)
    return result


# ── 依頼文 ─────────────────────────────────────────────────────


def lead_brief(name: str, path: Path, request: str | None) -> str:
    head = f"あなたは作業「{name}」の lead です。作業文書は {path} です。まず読んでください。"
    return "\n".join(x for x in (head, request) if x)


def crew_brief(name: str, path: Path, meta: dict, agent: dict, request: str | None) -> str:
    wt = worktree_of(meta, agent) or {}
    head = (f"あなたは作業「{name}」の crew で、リポジトリ {agent['repo']} を担当します。"
            f"worktree は {wt.get('path', agent['cwd'])}（ブランチ {wt.get('branch', '-')}）、lead は {lead_of(meta)['name']} です。"
            f"作業のディレクトリは {path.parent} で、進捗と結果は {path.parent / ('crew-' + slug(agent['repo']) + '.md')} に書いてください。")
    return "\n".join(x for x in (head, request) if x)


def send_briefs(backend: Backend, briefs: dict[str, str], doc_dir: Path) -> list[str]:
    """依頼を送り、届けられなかったエージェントを返す。届かなかった依頼文は作業ディレクトリに残す。"""
    undelivered = []
    for name, text in briefs.items():
        saved = doc_dir / f"undelivered-{name}.md"
        if (result := backend.send(name, text)) != "ok":
            undelivered.append(name)
            saved.write_text(text + "\n")
            print(f"warning: {name} に依頼が届いていません（{result}）。ユーザーに確認し、そのタブの確認に答えてもらってから、"
                  f"次のコマンドで再送してください:\n  herdr agent prompt {name} \"$(cat {shlex.quote(str(saved))})\"",
                  file=sys.stderr)
        else:
            saved.unlink(missing_ok=True)
    return undelivered


def show_manual(backend: Backend, specs: list[dict], briefs: dict[str, str], doc_dir: Path) -> None:
    """backend が起動できないエージェントの起動コマンドを示す。依頼文は最初のプロンプトとして渡す。"""
    if not specs:
        return
    print("backend が無いので、エージェントは起動していません。それぞれの端末で次のコマンドを実行してください:")
    for spec in specs:
        hint = backend.launch_hint(spec)
        if text := briefs.get(spec["name"]):
            saved = doc_dir / f"brief-{spec['name']}.md"
            saved.write_text(text + "\n")
            print(f"  {hint} \"$(cat {shlex.quote(str(saved))})\"")
        else:
            print(f"  {hint}")


# ── タブの作業: 組み立て ─────────────────────────────────────────


def commander_place(backend: Backend) -> str:
    """commander のいる場所のラベル。backend が無ければ hub-env。"""
    if backend.manual:
        return "hub-env"
    cmdr = next((a for a in backend.list_agents() if a.get("name") == COMMANDER), None)
    place = next((p for p in backend.list_places() if cmdr and p["id"] == cmdr.get("place")), None)
    if not place:
        raise HubError(f"司令塔（{COMMANDER}）が動いていません。herdr-reset で立ててから、もう一度実行してください")
    return place["label"]


def find_tab(backend: Backend, agent: dict) -> str | None:
    return next((t["id"] for t in backend.list_tabs(agent["workspace"]) if t["title"] == agent["tab"]), None)


def ensure_tabs(backend: Backend, env: Path, meta: dict, doc_dir: Path, names: list[str]) -> tuple[list[str], list[dict]]:
    """names のエージェントを、記録の場所とタブで揃える。(新しく起動した名前, 人が起動する AgentSpec) を返す。"""
    name = doc_dir.name
    alive = {a.get("name") for a in backend.list_agents()}
    started: list[str] = []
    manual: list[dict] = []
    for agent in meta["agents"]:
        if agent["name"] not in names or agent["name"] in alive:
            continue
        role = agent["role"]
        wt = worktree_of(meta, agent) if role == "crew" else None
        if wt:
            ensure_checkout(wt, (env / wt["repo"]).resolve())
            if agent.get("kind", "claude") == "claude":
                register_trust(env, name, wt)
        cwd = Path(agent["cwd"])
        allow = [doc_dir] if role == "lead" else [cwd, doc_dir]
        key = "lead" if role == "lead" else f"crew-{slug(agent['repo'])}"
        kind = agent.get("kind", "claude")
        argv = agent_argv(kind, role_launch(env, name, role, allow, key) if kind == "claude" else [], [],
                          work_access_args(doc_dir))
        envv = {WORK_ENV: name, ROLE_ENV: role}
        spec = {"name": agent["name"], "kind": kind, "cwd": str(cwd), "argv": argv, "env": envv}
        label = agent["workspace"]
        if backend.manual:
            pane = label
        else:
            place = one_place(label_map(backend), label)
            if place:
                # NOTE: エージェントのいない古いタブ（herdr が画面だけ戻したときなど）は、作り直す。
                if old := find_tab(backend, agent):
                    backend.close_tab(old)
                pane = backend.add_slot(place["id"], cwd, agent["tab"], envv)
            elif wt and wt.get("created_workspace"):
                pane = backend.open_place(label, cwd, agent["tab"], env=envv)["pane"]
            elif role == "lead":
                raise HubError(f"司令塔のワークスペース {label} がありません。herdr-reset で立ててから、もう一度実行してください")
            else:
                raise HubError(f"ワークスペース {label} がありません。herdr-reset {label} で作ってから、もう一度実行してください")
        if run_agent(backend, pane, spec) == "manual":
            manual.append(spec)
        else:
            started.append(agent["name"])
    return started, manual


def deliver(backend: Backend, started: list[str], manual: list[dict], briefs: dict[str, str], doc_dir: Path) -> list[str]:
    undelivered = send_briefs(backend, {n: briefs[n] for n in started if n in briefs}, doc_dir)
    show_manual(backend, manual, briefs, doc_dir)
    return undelivered


# ── コマンド: commander ─────────────────────────────────────────


def start(backend: Backend, env: Path, wd: Path, name: str, todo: int | None, purpose: str | None,
          request: str | None, kind: str = "claude") -> int:
    ensure_work_repo(env, wd)
    check_name(name, wd)
    claude_launch_args(env, "lead")  # 設定が壊れていれば、何も作らずに止まる
    if todo is not None and todo < 0:
        raise HubError("--todo は TODO の id にしてください")
    lead_name = f"w-{name}"
    if not AGENT_RE.fullmatch(lead_name):
        raise HubError(f"エージェント名に使えません: {lead_name}")
    if lead_name in {a.get("name") for a in backend.list_agents()}:
        raise HubError(f"同じ名前のエージェントが既にいます: {lead_name}")
    label = commander_place(backend)
    if any(t["title"] == name for t in backend.list_tabs(label)):
        raise HubError(f"ワークスペース {label} に同じ名前のタブがあります: {name}")
    hub_env = (env / "hub-env").resolve()
    lead = {"name": lead_name, "role": "lead", "repo": "hub-env", "cwd": str(hub_env), "workspace": label,
            "tab": name, "kind": kind}
    # 作るものを先に記録する。途中で失敗しても、finish と resume が記録から続けられる。
    path = wd / name / "status.md"
    path.parent.mkdir(parents=True)
    meta = {"state": "active", "todo": todo, "repos": [], "workspace": label, "agents": [lead], "worktrees": []}
    body = BODY.format(name=name, purpose="\n\n".join(x for x in (purpose, request and f"依頼: {request}") if x) or "（未記入）")
    write_status(path, meta, body)
    commit(wd, f"start {name}")
    try:
        started, manual = ensure_tabs(backend, env, meta, path.parent, [lead_name])
    except HubError as e:
        raise HubError(f"{e}\n途中まで作りました。hub-work resume {name} で続けるか、hub-work finish {name} で片付けてください")
    briefs = {lead_name: lead_brief(name, path, request)}
    undelivered = deliver(backend, started, manual, briefs, path.parent)
    commit(wd, f"brief {name}")
    print(f"作業 {name} を始めました: {path}")
    print(f"lead: {lead_name}（ワークスペース {label} のタブ {name}）")
    return 1 if undelivered else 0


def resume(backend: Backend, env: Path, wd: Path, name: str, roles: tuple[str, ...] = ("lead", "crew")) -> int:
    path = status_path(wd, name)
    meta, body = read_status(path)
    if meta["state"] == "done":
        raise HubError(f"作業 {name} は done です")
    ensure_work_ignore(wd)
    if not is_tabbed(meta):
        return legacy_resume(backend, env, wd, name, path, meta, body)
    names = [a["name"] for a in meta["agents"] if a["role"] in roles]
    try:
        started, manual = ensure_tabs(backend, env, meta, path.parent, names)
    finally:
        write_status(path, meta, body)
        commit(wd, f"resume {name}")
    briefs = {a["name"]: (lead_brief(name, path, RESUME_TEXT) if a["role"] == "lead"
                          else crew_brief(name, path, meta, a, RESUME_TEXT)) for a in meta["agents"]}
    undelivered = deliver(backend, started, manual, briefs, path.parent)
    commit(wd, f"brief {name}")
    print(f"作業 {name} を立て直しました（起動: {', '.join(started) or 'なし'}）")
    return 1 if undelivered else 0


def list_works(backend: Backend, wd: Path, all_: bool) -> int:
    rows = []
    roots = [(wd, "")] + ([(wd / "archive", "archive/")] if all_ else [])
    alive = {x.get("name"): x.get("status") for x in backend.list_agents()}
    missing = "不明" if backend.manual else "停止"
    for root, prefix in roots:
        if not root.is_dir():
            continue
        for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in ("archive", ".git")):
            try:
                meta, _ = read_status(d / "status.md")
            except (OSError, HubError) as e:
                rows.append((prefix + d.name, "error", "-", str(e), "-"))
                continue
            st = ", ".join(f"{x['name']}={alive.get(x['name'], missing)}" for x in meta["agents"]) if not prefix else "-"
            rows.append((prefix + d.name, meta["state"], str(meta["todo"] or "-"), ",".join(meta["repos"]) or "-", st))
    if not rows:
        print("作業はありません")
    for row in rows:
        print("\t".join(row))
    return 0


# ── コマンド: lead ─────────────────────────────────────────────


def assign(backend: Backend, env: Path, wd: Path, name: str, repo: str, request: str, kind: str = "claude") -> int:
    path = status_path(wd, name)
    meta, body = read_status(path)
    if not is_tabbed(meta):
        raise HubError(f"作業 {name} は旧形式なので assign できません")
    src = (env / repo).resolve()
    if not src.is_dir():
        raise HubError(f"リポジトリがありません: {src}")
    lead = lead_of(meta)
    if existing := next((a for a in crews_of(meta) if (env / a["repo"]).resolve() == src), None):
        # 1 つの作業で、1 つのリポジトリに crew は 1 人。今の crew に送る（居なければ立て直す）
        try:
            started, manual = ensure_tabs(backend, env, meta, path.parent, [existing["name"]])
        finally:
            write_status(path, meta, body)
            commit(wd, f"assign {name} {existing['repo']}")
        brief = crew_brief(name, path, meta, existing, request) if started or manual else request
        undelivered = deliver(backend, [existing["name"]] if not manual else [], manual, {existing["name"]: brief},
                              path.parent)
        print(f"{existing['repo']} には crew {existing['name']} がいるので、指示を送りました")
        return 1 if undelivered else 0
    if not is_git_repo(src):
        raise HubError(f"git リポジトリではないので worktree を作れません: {src}")
    s = slug(repo)
    if s in {slug(a["repo"]) for a in crews_of(meta)}:
        raise HubError(f"名前が同じリポジトリの crew がいます: {s}")
    crew = f"w-{name}-{s}"
    if not AGENT_RE.fullmatch(crew):
        raise HubError(f"エージェント名に使えません: {crew}。作業名を短くしてください")
    if crew in {a.get("name") for a in backend.list_agents()}:
        raise HubError(f"同じ名前のエージェントが既にいます: {crew}")
    claude_launch_args(env, "crew")  # 設定が壊れていれば、何も作らずに止まる
    cwd = env / ".hub-worktrees" / name / s
    branch = f"work/{name}"
    if cwd.exists():
        raise HubError(f"worktree の場所が既にあります: {cwd}")
    if git(src, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).returncode == 0:
        raise HubError(f"ブランチが既にあります: {branch}（{src}）")
    label = backend.place_for_dir(src)
    created = label is None
    label = label or s
    tab = name if label != lead["workspace"] else f"{name}/{s}"
    if any(t["title"] == tab for t in backend.list_tabs(label)):
        raise HubError(f"ワークスペース {label} に同じ名前のタブがあります: {tab}")
    agent = {"name": crew, "role": "crew", "repo": repo, "cwd": str(cwd), "workspace": label, "tab": tab, "kind": kind}
    wt = {"repo": repo, "path": str(cwd), "branch": branch, "workspace": label}
    if created:
        wt["created_workspace"] = True
    meta["repos"].append(repo)
    meta["agents"].append(agent)
    meta["worktrees"].append(wt)
    write_status(path, meta, body)
    commit(wd, f"assign {name} {repo}")
    try:
        started, manual = ensure_tabs(backend, env, meta, path.parent, [crew])
    except HubError as e:
        raise HubError(f"{e}\n記録はしました。hub-lead resume で続けてください")
    finally:
        write_status(path, meta, body)
        commit(wd, f"record {name} {repo}")
    undelivered = deliver(backend, started, manual, {crew: crew_brief(name, path, meta, agent, request)}, path.parent)
    commit(wd, f"brief {name} {repo}")
    print(f"crew {crew} を立てました（ワークスペース {label} のタブ {tab}、worktree {cwd}）")
    return 1 if undelivered else 0


def lead_targets(env: Path, cfg: dict, meta: dict) -> set[str]:
    """lead が send / read してよい相手: 自分の crew と、commander を除く常駐エージェント。"""
    return {a["name"] for a in crews_of(meta)} | {a["name"] for a in agent_defs(env, cfg) if a["name"] != COMMANDER}


# ── finish ─────────────────────────────────────────────────────


def finish(backend: Backend, env: Path, wd: Path, name: str, by_lead: bool = False) -> int:
    path = status_path(wd, name)
    meta, body = read_status(path)
    dest = wd / "archive" / name
    if dest.exists():
        raise HubError(f"archive に同名の作業があります: {dest}")
    if not is_tabbed(meta):
        if by_lead:
            raise HubError(f"作業 {name} は旧形式なので、司令塔の hub-work finish {name} で閉じてください")
        return legacy_finish(backend, env, wd, name, path, meta, body)

    # ── 何かを閉じる前に、止まる条件をすべて調べる ──
    tabs = {a["name"]: find_tab(backend, a) for a in meta["agents"]}
    here = backend.current_tab()
    lead = lead_of(meta)
    if not by_lead and here and here in tabs.values():
        raise HubError("作業のタブからは hub-work finish できません。lead は hub-lead finish を使ってください")
    for wt in meta["worktrees"]:
        tree = Path(wt["path"])
        if tree.exists() and (dirty := dirty_files(tree)):
            raise HubError(f"worktree に未コミットの変更があるので止めました: {tree}\n" + "\n".join(dirty[:10]))

    # ── 閉じる ──
    ours = {t for t in tabs.values() if t}
    for crew in crews_of(meta):
        wt = worktree_of(meta, crew) or {}
        if tab := tabs[crew["name"]]:
            rest = [t for t in backend.list_tabs(crew["workspace"]) if t["id"] not in ours]
            if wt.get("created_workspace") and not rest:
                backend.close_place(crew["workspace"])
                print(f"ワークスペースを閉じました: {crew['workspace']}")
            else:
                backend.close_tab(tab)
                print(f"crew のタブを閉じました: {crew['workspace']} / {crew['tab']}")
    for wt in meta["worktrees"]:
        close_worktree(env, name, wt)
    parent = env / ".hub-worktrees" / name
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
    shutil.rmtree(works_state(env, name), ignore_errors=True)
    own_tab = tabs[lead["name"]]
    if not by_lead and own_tab:
        backend.close_tab(own_tab)
        print(f"lead のタブを閉じました: {lead['workspace']} / {lead['tab']}")

    archive(wd, name, path, meta, body, dest)
    if by_lead:
        if own_tab and own_tab == here:
            backend.close_tab_later(own_tab)
            print("このタブを閉じます")
        elif own_tab:
            print(f"note: このプロセスは lead のタブ（{lead['workspace']} / {lead['tab']}）の外にいるので、タブは閉じません")
    return 0


def close_worktree(env: Path, name: str, wt: dict) -> None:
    tree = Path(wt["path"])
    if tree.exists():
        git((env / wt["repo"]).resolve(), "worktree", "remove", str(tree))
    print(f"worktree を外しました: {tree}")
    try:
        print(tidy_branch((env / wt["repo"]).resolve(), wt["branch"], name))
    except HubError as e:
        print(f"ブランチ {wt['branch']} は、マージ済みか判定できないので残しています: {e}")
    if wt.get("trusted"):
        try:
            untrust_worktree(tree.resolve())
        except HubError as e:
            print(f"warning: worktree の信頼の登録を外せませんでした: {e}", file=sys.stderr)


def archive(wd: Path, name: str, path: Path, meta: dict, body: str, dest: Path) -> None:
    meta["state"] = "done"
    write_status(path, meta, body)
    dest.parent.mkdir(exist_ok=True)
    shutil.move(str(wd / name), str(dest))
    ensure_work_ignore(wd)
    commit(wd, f"finish {name}")
    print(f"作業 {name} を archive へ移しました: {dest}")


def net_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """origin と通信する git。認証を尋ねて止まらないようにし、時間で打ち切る。"""
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "timeout")


def merge_base_ref(repo: Path) -> str:
    """作業ブランチがマージされる先。origin があれば fetch した origin の既定ブランチ、無ければローカルの main / master。"""
    if git(repo, "remote", "get-url", "origin", check=False).returncode != 0:
        for b in ("main", "master"):
            if git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{b}", check=False).returncode == 0:
                return b
        raise HubError("origin が無く、ローカルに main も master もありません")
    if net_git(repo, "fetch", "--prune", "-q", "origin").returncode != 0:
        raise HubError("origin を fetch できませんでした")
    p = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    if p.returncode == 0:
        return p.stdout.strip()
    m = re.search(r"^ref: refs/heads/(\S+)\tHEAD$", net_git(repo, "ls-remote", "--symref", "origin", "HEAD").stdout, re.M)
    if m and git(repo, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{m[1]}", check=False).returncode == 0:
        return f"origin/{m[1]}"
    raise HubError("origin の既定ブランチが分かりません")


def merged_pr(repo: Path, branch: str) -> str | None:
    """GitHub の PR が MERGED なら番号を返す。gh が使えないときや、分からないときは None。"""
    if "github.com" not in git(repo, "remote", "get-url", "origin", check=False).stdout or not shutil.which("gh"):
        return None
    try:
        p = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number", "--jq", ".[0].number"],
                           cwd=repo, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (p.stdout.strip() or None) if p.returncode == 0 else None


def tidy_branch(repo: Path, branch: str, name: str) -> str:
    """マージ済みの作業ブランチを git branch -d で消す。消さないときは、その理由を返す。リモートのブランチには触らない。"""
    if branch != f"work/{name}":
        return f"ブランチ {branch} は作業ブランチの名前ではないので残しています"
    if git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).returncode != 0:
        return f"ブランチ {branch} はもうありません"
    base = merge_base_ref(repo)
    if git(repo, "merge-base", "--is-ancestor", branch, base, check=False).returncode != 0:
        if pr := merged_pr(repo, branch):
            return (f"ブランチ {branch} は、PR #{pr} がマージ済みですが、履歴が {base} に含まれない（squash merge など）ので"
                    "残しています。不要なら手で消してください")
        return f"ブランチ {branch} は {base} にマージされていないので残しています"
    # NOTE: git branch -d は upstream（無ければ HEAD）に含まれるかで確かめる。元のリポジトリの HEAD が
    # 古い main でも消せるよう、確かめる先を判定に使った base に向けてから -d する。失敗したら元へ戻す。
    keys = [f"branch.{branch}.remote", f"branch.{branch}.merge"]
    saved = {k: git(repo, "config", "--get", k, check=False).stdout.strip() for k in keys}
    git(repo, "branch", f"--set-upstream-to={base}", branch)
    p = git(repo, "branch", "-d", branch, check=False)
    if p.returncode == 0:
        return f"ブランチ {branch} を消しました（{base} にマージ済み）"
    for k, v in saved.items():
        git(repo, "config", "--unset", k, check=False)
        if v:
            git(repo, "config", k, v)
    return f"ブランチ {branch} は git branch -d で消せなかったので残しています: {p.stderr.strip()}"


# ── 通知 ───────────────────────────────────────────────────────


def env_seconds(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def seen(backend: Backend, name: str, text: str) -> bool:
    """送った文が画面に出ているか。長い文は折り返しや省略があるので、先頭の 1 行の一部で確かめる。"""
    head = text.strip().splitlines()[0][:40] if text.strip() else ""
    return bool(head) and head in backend.read(name, 400)


def notify(backend: Backend, name: str, text: str, fallback: str) -> int:
    """name へ送る。確認待ちなら待って送り直し、timeout なら画面で確かめる。fallback は届かなかったときの案内。"""
    deadline = time.monotonic() + env_seconds("HUB_AGENT_NOTIFY_WAIT", NOTIFY_WAIT)
    interval = env_seconds("HUB_AGENT_NOTIFY_INTERVAL", NOTIFY_INTERVAL)
    while True:
        result = backend.send(name, text)
        if result == "ok":
            print(f"{name} へ通知しました")
            return 0
        # timeout は届いている場合があるので、二重に送らないよう画面で確かめる
        if result == "timeout" and seen(backend, name, text):
            print(f"{name} の画面に届いていることを確かめました")
            return 0
        if result not in ("blocked", "timeout") or time.monotonic() >= deadline:
            print(f"{name} へ届きませんでした（{result}）。{fallback}", file=sys.stderr)
            return 1
        time.sleep(interval)


# ── 旧形式（作業ごとに場所を作る形）。移行が済んだら消す ─────────────


def legacy_ensure(backend: Backend, env: Path, meta: dict, doc_dir: Path) -> tuple[list[str], list[dict]]:
    """記録の通りに揃える。(新しく起動したエージェントの名前, 人が起動するエージェントの AgentSpec) を返す。"""
    launch = claude_launch_args(env)
    started: list[str] = []
    manual: list[dict] = []
    alive = {a.get("name") for a in backend.list_agents()}
    worktrees = {w["repo"]: w for w in meta["worktrees"]}
    fresh: dict[str, str] = {}  # 場所のラベル → まだ使っていない最初の置き場
    for agent in meta["agents"]:
        label = agent["workspace"]
        place = one_place(label_map(backend), label)
        title = slug(agent["repo"])
        if not place:
            wt = worktrees.get(agent["repo"])
            if wt:
                repo = (env / agent["repo"]).resolve()
                ensure_checkout(wt, repo)
                res = backend.open_place(label, Path(agent["cwd"]), title,
                                         {"repo": str(repo), "path": wt["path"], "branch": wt["branch"]})
                wt.update(res["data"])
            else:
                res = backend.open_place(label, Path(agent["cwd"]), title)
            place = {"id": res["place"], "label": label}
            fresh[label] = res["pane"]
        if agent["name"] in alive:
            continue
        pane = fresh.pop(label, None) or backend.add_slot(place["id"], Path(agent["cwd"]), title)
        wt = worktrees.get(agent["repo"])
        if wt and agent.get("kind", "claude") == "claude":
            register_trust(env, doc_dir.name, wt)
        kind = agent.get("kind", "claude")
        spec = {"name": agent["name"], "kind": kind, "cwd": agent["cwd"],
                "argv": agent_argv(kind, launch, [], work_access_args(doc_dir))}
        if run_agent(backend, pane, spec) == "manual":
            manual.append(spec)
        else:
            started.append(agent["name"])
    return started, manual


def legacy_brief(env: Path, name: str, path: Path, request: str, meta: dict) -> str:
    head = f"あなたは作業「{name}」の担当です。作業文書は {path} です。まず読んでください。"
    rules = (f"消えて困るものは作業コピー（worktree）に置かないでください。finish で削除されます。メモ（手順・調査メモ・再現方法）は "
             f"{path.parent}/runbook.md に、検証データは {path.parent}/data/ に置いてください。秘密情報はどちらにも書かないでください。"
             "担当のリポジトリのほかに書き込んでよいのは、この作業のディレクトリだけです。\n"
             "進捗と結果は、先にこの status.md の本文（進捗・結果）へ書いてください。front matter の state は、"
             "止まったら blocked にします。そのあと hub-agent notify \"<作業名と要約>\" で司令塔へ通知してください。"
             "届かなかったと表示されたら、少し待って同じコマンドを実行し直してください。"
             "backend が無いと表示されたら、通知は要りません（司令塔が status.md を読みます）。"
             "\nコミットや PR は、環境の rules に従ってください。")
    if any((env / r).resolve() == (env / "hub-env").resolve() for r in meta["repos"]):
        rules += ("\nhub-env の CLAUDE.md は司令塔向けですが、あなたは司令塔ではなく、この作業の担当です。"
                  "worktree の中のファイルを変更してください。")
    return "\n".join((head, request, rules))


def legacy_resume(backend: Backend, env: Path, wd: Path, name: str, path: Path, meta: dict, body: str) -> int:
    try:
        started, manual = legacy_ensure(backend, env, meta, path.parent)
    finally:
        write_status(path, meta, body)
        commit(wd, f"resume {name}")
    text = legacy_brief(env, name, path, "herdr-reset などで中断した作業の再開です。status.md の進捗から続けてください。", meta)
    briefs = {n: text for n in [*started, *(s["name"] for s in manual)]}
    undelivered = deliver(backend, started, manual, briefs, path.parent)
    commit(wd, f"brief {name}")
    print(f"作業 {name} を立て直しました（起動: {', '.join(started) or 'なし'}）")
    return 1 if undelivered else 0


def legacy_finish(backend: Backend, env: Path, wd: Path, name: str, path: Path, meta: dict, body: str) -> int:
    # ── 何かを閉じる前に、止まる条件をすべて調べる ──
    own = {meta["workspace"], *(x["workspace"] for x in meta["agents"]), *(x["workspace"] for x in meta["worktrees"])}
    if backend.current_place() in own:
        raise HubError("作業自身のワークスペースからは finish できません。司令塔で実行してください")
    for wt in meta["worktrees"]:
        tree = Path(wt["path"])
        if tree.exists() and (dirty := dirty_files(tree)):
            raise HubError(f"worktree に未コミットの変更があるので止めました: {tree}\n" + "\n".join(dirty[:10]))
    tree_labels = {x["workspace"] for x in meta["worktrees"]}
    if problems := backend.can_close(own, tree_labels):
        raise HubError("\n".join(problems))

    # ── 閉じる ──
    for wt in meta["worktrees"]:
        backend.close_place(wt["workspace"], wt)
        close_worktree(env, name, wt)
    for label in sorted(own - tree_labels):
        if backend.close_place(label):
            print(f"ワークスペースを閉じました: {label}")
    parent = env / ".hub-worktrees" / name
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
    archive(wd, name, path, meta, body, wd / "archive" / name)
    return 0
