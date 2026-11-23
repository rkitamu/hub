# hub

hub は、1 つのディレクトリに並ぶリポジトリを、herdr の上でまとめて扱うための道具。このディレクトリを「環境」と呼ぶ。

環境ごとに `hub-env` というディレクトリを作り、そこで開いた Claude が、地図 `MAP.md` を読んで環境全体の司令塔になる。hub 自体はどこに clone してもよく、1 つを全部の環境で共有する。

```
~/src/hub/            hub（道具。1 つだけ）
~/personal/           環境
├── hub-env/          司令塔の作業場所: CLAUDE.md・MAP.md・hub.toml・.claude/settings.local.json
├── todo/             子リポジトリ（例）
└── ...
~/work/               別の環境
├── hub-env/
└── ...
```

## 前提

- `~/.local/bin` に PATH が通っている
- herdr・[uv](https://docs.astral.sh/uv/) が入っている

## セットアップ

### 1. hub を clone して、コマンドを入れる

```sh
git clone <hub> ~/src/hub
~/src/hub/install.sh
```

`hub-reset`・`hub-work`・`hub-status`・`hub-check`・`hub-guard`・`hub-commit-guard` が `~/.local/bin` にリンクされる。

### 2. 環境を用意する

```sh
mkdir -p <環境> && cd <環境>
git clone <todo> todo   # 子リポジトリの例
~/src/hub/install.sh --env <環境>
```

環境に `hub-env` ができ、`~/.config/hub/envs` に環境が登録される。

### 3. `hub-env/hub.toml` と `hub-env/MAP.md` を編集する

雛形には、司令塔の `hub-env` ワークスペースだけが有効な最小構成で入っている。そのままでも `hub-reset` で動く。

`todo` ワークスペース・`todo` エージェント・`scripts/todo-view` などは、作者の手元の todo リポジトリを前提にした例で、コメントアウトしてある。自分の子リポジトリに合わせて書き換えるか、消す。

### 4. herdr を開いて `hub-reset` を実行する

```sh
herdr --session <セッション名>
hub-reset
```

セッション名は `hub.toml` の `session` に書いたもの。

## hub-env

| ファイル | 中身 |
|---|---|
| `CLAUDE.md` | hub の `commander.md`（司令塔の共通ルール）と `MAP.md` を読み込む |
| `MAP.md` | 地図。子リポジトリとエージェント、子同士の連携ルール |
| `hub.toml` | herdr のセッション名と、ワークスペース・タブ・ペインの構成。書き方は雛形のコメントにある |
| `.claude/settings.local.json` | 司令塔の Claude Code の設定。環境と hub を読む許可と、`hub-guard` フック |
| `.claude/skills/delegate/` | TODO の作業を任せる段取りのスキル |
| `.gitignore` | `work/` を hub-env の git から外す |
| `work/` | 作業文書。hub-env とは別の、手元専用の git リポジトリ |
| `rules/*.md` | この環境のエージェント全員に渡すルール（任意） |
| `agent-settings.json` | この環境のエージェント全員に `--settings` で渡す設定（任意）。雛形は `hub-commit-guard` を登録する |

`install.sh --env` は既にあるファイルを上書きしない。既存の環境に足すときは、`settings.local.json` の `hooks` と `.gitignore` を手で足す（`agent-settings.json` とスキルは、無ければ作られる）。

## コマンド

```sh
hub-reset         # ワークスペースを全部閉じて、hub.toml の通りに作り直す
hub-reset todo    # todo ワークスペース（hub.toml の label）だけを作り直す

hub-work start <作業名> --repo <repo> [--todo <id>] [--worktree] [--purpose <目的>] [--request <依頼文>]
hub-work finish <作業名>   # 記録されたものだけを閉じ、archive へ移す
hub-work resume <作業名>   # hub-reset の後などに、status.md から立て直す
hub-work list [--all]

hub-status [--days 7]      # エージェント・未コミット・進行中の作業・期限の近い TODO
hub-check                  # MAP.md・hub.toml・リポジトリ・エージェントのずれを検査（ずれがあれば exit 1）
```

`hub.toml` の `session` に書いたセッションのペインで実行する。

### 作業（hub-work）

作業ごとに `<work_dir>/<作業名>/status.md` を作る。`work_dir` は `hub.toml` で変えられ、既定は `hub-env/work`。

```markdown
---
state: active
todo: 30
repos: ["todo"]
workspace: "w-example"
agents: [{"name": "w-example", "repo": "todo", "cwd": "...", "workspace": "w-example", "kind": "claude"}]
worktrees: []
---

# example

## 目的

## 進捗

## 結果
```

作業ディレクトリには、`status.md`（必須）のほかに `runbook.md`（任意。手順・調査メモ・再現方法）と `data/`（任意。検証データ）を置ける。`start` が作るのは `status.md` だけで、`finish` はディレクトリごと `archive/` へ移す。`status.md` と `runbook.md` は自動でコミットされ、`data/` は work の `.gitignore` で除外される（archive 内も同じ。既存のリポジトリにも次の `hub-work` で効く）。秘密情報はどちらにも書かない。

作業エージェントには起動時に `--add-dir` と `--allowedTools` で、その作業のディレクトリへの書き込みだけを許可する。

- `state` は `active` / `blocked` / `done`。front matter の値は JSON で書く（YAML としても読める）。
- `--worktree` を付けると、`<環境>/.hub-worktrees/<作業名>/<repo>` にブランチ `work/<作業名>` の worktree を作る。herdr の worktree は 1 つが 1 ワークスペースなので、複数リポジトリでは 2 つ目以降が `w-<作業名>--<repo>` になる。
- `finish` は worktree に未コミットの変更があれば、何も閉じずに止まる。ブランチは消さない。`.gitignore` された未追跡ファイルは未コミットの変更に数えず、worktree と一緒に消える。
- 作業のワークスペースが他の worktree の親になっているときも、他の作業を巻き込まないよう止まる。
- `archive` は作業名に使えない。`archive/` にある名前も再利用できない。

### setup（スクリプトで組み立てるワークスペース）

```toml
[[workspace]]
label = "todo"
cwd = "todo"
setup = "todo/contrib/herdr/todo-herdr"
agents = ["todo"]
```

`hub-reset` は既存のワークスペースを閉じてからスクリプトを実行する。そのため `setup` のワークスペースを単体でリセットすると、サイドバーの末尾へ移る。

### rules と agent-settings.json

`hub-reset` と `hub-work` が起動する claude に、次を渡す。`~/.claude` には何も書かない。agent の `args` とは併用できる（`--settings` と `--append-system-prompt-file` を `args` に書くと衝突として止まる）。

- `--append-system-prompt-file`: `hub/rules/*.md`、`<hub-env>/rules/*.md` の順に連結した生成ファイル。`$XDG_STATE_HOME/hub/envs/<環境>-<ハッシュ>/agent-rules.md` に置く
- `--settings`: `<hub-env>/agent-settings.json`（あれば）

反映されるのは、次に起動したエージェントから。`setup` のスクリプトが自分で起動するエージェントには自動では届かないので、スクリプト側で環境変数 `HUB_CLAUDE_ARGS`（シェル用に引用済み）を claude の引数に足す。

### hub-commit-guard（main / master へのコミットを拒否するフック）

Bash のコマンドを解析し、main / master への `git commit` を拒否する。`git -C`・`--git-dir`・`GIT_DIR=`・`cd`・複合コマンド・サブシェル・`$(...)`・`bash -c`・`eval`・`env` / `sudo` / `xargs` / `find -exec`・git のエイリアスを辿る。`git switch -c x && git commit` は切り替え先で判定する。判定できないときは拒否する。

`git merge` と `git merge --continue` は止めないので、remote の無いリポジトリはローカルでマージできる。remote があるリポジトリは PR を介す（`git push` はこのフックの対象外）。作業文書や TODO のデータのように、ローカルでのみバージョン管理するリポジトリだけ `--allow <パス>` で除外する。remote が無いだけのコードや設定のリポジトリは除外しない。雛形は `hub-env/work` だけを除外する。`hub-work` の記録のコミットは Bash を通らないので、このフックの影響を受けない。スクリプトファイルの中身と、実行時に組み立てられる文字列は対象外で、これも誤操作の防止であり、セキュリティの境界ではない。

### hub-guard（子リポジトリの編集を拒否するフック）

司令塔の Edit / Write / NotebookEdit について、`..` とシンボリックリンクを解決した実際の書き込み先が hub-env の外なら拒否する。判定できないときも拒否する。

**Bash 経由の書き込みは対象外。** `echo > ../todo/x` のような操作は止められないので、これは誤操作の防止であり、セキュリティの境界ではない。`work_dir` を hub-env の外にすると、司令塔はそこも Edit / Write で編集できない。

## テスト

```sh
python3 tests/test_hub_guard.py          # hub-guard の単体テスト
python3 tests/test_hub_commit_guard.py   # hub-commit-guard の単体テスト
tests/integration.sh              # 一時ディレクトリの環境と専用の herdr サーバーで通しのテスト
```

`integration.sh` は `XDG_CONFIG_HOME` ごと一時ディレクトリへ移して専用サーバーを立て、実際のセッションには触らない。

## ライセンス

[MIT](LICENSE)
