# hub

hub は、1 つのディレクトリに並ぶリポジトリを、Claude のエージェントでまとめて扱うための道具。このディレクトリを「環境」と呼ぶ。エージェントを置いて動かす仕組み（backend）には、herdr を使える（`contrib/herdr`）。

環境ごとに `hub-env` というディレクトリを作り、そこに司令塔（commander）を置く。司令塔は依頼を作業にして、作業ごとの lead を立てる。lead はユーザーと直接話して計画を決め、子リポジトリごとに crew を立てて変更を振る。hub 自体はどこに clone してもよく、1 つを全部の環境で共有する。

| 役割 | いる場所 | 使うコマンド | 決まり |
|---|---|---|---|
| commander | `hub-env` ワークスペースの司令塔のタブ | `hub-work`・`hub-agent` | `roles/commander.md` |
| lead | 司令塔と同じワークスペースの、作業名のタブ（cwd は hub-env） | `hub-lead` | `roles/lead.md` |
| crew | 子リポジトリのワークスペースのタブ（cwd はその子の worktree） | `hub-crew` | `roles/crew.md` |

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
- [uv](https://docs.astral.sh/uv/) が入っている
- backend に herdr を使うなら、herdr が入っている

## セットアップ

### 1. hub を clone して、コマンドを入れる

```sh
git clone <hub> ~/src/hub
~/src/hub/install.sh
git -C ~/src/hub config core.hooksPath .githooks   # hub の main / master への直接コミットと push を拒否する
```

次のコマンドが `~/.local/bin` にリンクされる。

- `hub-work`
- `hub-lead`
- `hub-crew`
- `hub-agent`
- `hub-status`
- `hub-check`
- `hub-guard`
- `hub-commit-guard`
- `hub-session-record`
- `herdr-reset`（`contrib/herdr`）
- `migrate-hub-toml`（`contrib/herdr`）

### 2. 環境を用意する

```sh
mkdir -p <環境> && cd <環境>
git clone <todo> todo   # 子リポジトリの例
~/src/hub/install.sh --env <環境>
```

環境に `hub-env` ができ、`~/.config/hub/envs` に環境が登録される。

### 3. `hub-env/hub.toml` と `hub-env/MAP.md` を編集する

`hub.toml` には、エージェントの定義（`[[agent]]`）と、backend の設定（herdr なら `[herdr]` のレイアウト）を書く。雛形には、司令塔 `commander` と、その `hub-env` ワークスペースだけが有効な最小構成で入っている。書き方は雛形のコメントにある。

`todo` ワークスペース・`todo` エージェント・`scripts/todo-view` などは、作者の手元の todo リポジトリを前提にした例で、コメントアウトしてある。自分の子リポジトリに合わせて書き換えるか、消す。

### 4. 司令塔を起動する

backend が herdr なら、herdr を開いて `herdr-reset` を実行する。セッション名は `hub.toml` の `[herdr]` の `session` に書いたもの。

```sh
herdr --session <セッション名>
herdr-reset
```

backend を使わない（`backend = "none"`）なら、端末で `hub-agent launch commander` を実行する。

## hub-env

司令塔への頼み方は、各環境の `hub-env/README.md` を参照する。

| ファイル | 中身 |
|---|---|
| `CLAUDE.md` | 環境の説明と、`MAP.md` の読み込み |
| `MAP.md` | 地図。子リポジトリとエージェント、子同士の連携ルール |
| `hub.toml` | backend、エージェントの定義（`[[agent]]`）、herdr のセッション名とワークスペース・タブ・ペインの構成（`[herdr]`）。書き方は雛形のコメントにある |
| `README.md` | この環境の司令塔への頼み方の表 |
| `.claude/settings.local.json` | 司令塔の Claude Code の設定。環境と hub を読む許可と、`hub-guard` フック |
| `.gitignore` | `work/` を hub-env の git から外す |
| `work/` | 作業文書。hub-env とは別の、手元専用の git リポジトリ |
| `rules/*.md` | この環境のエージェント全員に渡すルール（任意） |
| `agent-settings.json` | この環境のエージェント全員に `--settings` で渡す設定（任意）。雛形は `hub-commit-guard` を登録する |

`install.sh --env` は既にあるファイルを上書きしない。既存の環境に足すときは、`settings.local.json` の `hooks` と `.gitignore` を手で足す（`agent-settings.json`、`README.md` は、無ければ作られる）。

## コマンド

```sh
# commander
hub-work start <作業名> [--todo <id>] [--purpose <目的>] [--request <指示文>]   # 作業名のタブに lead を立てる
hub-work finish <作業名>   # 記録されたものだけを閉じ、archive へ移す（lead がいないときの予備）
hub-work resume <作業名>   # herdr-reset の後などに、status.md から lead と crew を立て直す
hub-work list [--all]

hub-agent list                       # 動いているエージェントと状態
hub-agent send <名前> "<依頼>"        # エージェントに送る
hub-agent read <名前> [--lines <N>]   # エージェントの画面を読む
hub-agent launch <名前> [--print]     # [[agent]] の通りに、この端末で起動する

# lead（作業名は HUB_WORK から取る）
hub-lead assign <repo> --request "<指示文>"   # 子リポジトリの worktree に crew を立てて振る
hub-lead send <名前> "<指示>"                 # 自分の crew か常駐エージェントに送る
hub-lead read <名前> [--lines <N>]
hub-lead notify "<要約>"                      # 司令塔へ通知する
hub-lead list | resume | finish

# crew
hub-crew notify "<要約>"                      # lead へ通知する

hub-status [--days 7]      # エージェント・未コミット・進行中の作業・期限の近い TODO
hub-check                  # MAP.md・hub.toml・リポジトリ・エージェントのずれを検査（ずれがあれば exit 1）
```

環境の中で実行する。herdr なら、`[herdr]` の `session` に書いたセッションのペインでも実行できる。環境変数 `HUB_ENV` に環境のパスを指定してもよい。

### 作業（hub-work）

作業ごとに `<hub-env>/work/<作業名>/status.md` を作る。

```markdown
---
state: active
todo: 30
repos: ["todo"]
workspace: "hub-env"
agents: [{"name": "w-example", "role": "lead", "repo": "hub-env", "cwd": "...", "workspace": "hub-env", "tab": "example", "kind": "claude"},
         {"name": "w-example-todo", "role": "crew", "repo": "todo", "cwd": "...", "workspace": "todo", "tab": "example", "kind": "claude"}]
worktrees: [{"repo": "todo", "path": "...", "branch": "work/example", "workspace": "todo"}]
---

# example

## 目的

## 進捗

## 結果
```

作業ディレクトリには、`status.md`（必須。lead が書く）のほかに `crew-<repo>.md`（crew の進捗と結果）、`runbook.md`（任意。手順・調査メモ・再現方法）と `data/`（任意。検証データ）を置ける。`status.md` などは自動でコミットされ、`data/` は work の `.gitignore` で除外される（archive 内も同じ。既存のリポジトリにも次の `hub-work` で効く）。秘密情報はどれにも書かない。

- `state` は `active` / `blocked` / `done`。front matter の値は JSON で書く（YAML としても読める）。
- `hub-work start` は、司令塔（commander）と同じワークスペースに、作業名のタブで lead（`w-<作業名>`）を立てる。cwd は hub-env。司令塔が動いていなければ止まる。
- `hub-lead assign <repo>` は、`<環境>/.hub-worktrees/<作業名>/<repo>` にブランチ `work/<作業名>` の worktree を作り、その子のワークスペース（`[[herdr.workspace]]` の `cwd` がそのリポジトリのもの。無ければラベル `<repo>` で作る）に作業名のタブで crew（`w-<作業名>-<repo>`）を立てる。`--repo` には hub-env と、環境の外のリポジトリ（例: `../src/hub`）も指定できる。1 つの作業で 1 つのリポジトリに crew は 1 人で、同じリポジトリにもう一度振ると、今の crew に指示を送る。
- 元のリポジトリが信頼済みなら、worktree も Claude Code の信頼済みフォルダとして扱う。依頼が届かなかったときは、再送のコマンドを表示する。
- lead と crew には、`HUB_WORK=<作業名>` と `HUB_ROLE=lead|crew` を渡す。`hub-work`・`hub-agent`（`list` を除く）・`herdr-reset` は `HUB_ROLE` があれば止まり、`hub-lead`・`hub-crew` は役割が合わなければ止まる。誤操作の防止であり、セキュリティの境界ではない。
- lead の Edit / Write は作業のディレクトリの中だけ、crew は担当の worktree と作業のディレクトリの中だけに制限する（`hub-guard --allow` を登録した設定を `--settings` で渡す）。
- `hub-lead finish` は、全 crew の worktree に未コミットの変更があれば何も閉じずに止まる。crew のタブと worktree を閉じ、作業ブランチ `work/<作業名>` がマージ済みならローカルから消し（`git branch -d`）、archive へ移したあと、最後に lead のタブを閉じる。マージ先は、origin があれば origin の既定ブランチ、無ければローカルの main。マージされていないときや判定できないときは残し、その旨を表示する。リモートのブランチは消さない。`.gitignore` された未追跡ファイルは未コミットの変更に数えず、worktree と一緒に消える。`hub-work finish <作業名>` も同じことをする。
- crew は `hub-crew notify` で lead に、lead は hub-env や hub の道具に影響する変更をマージしたときだけ `hub-lead notify` で司令塔に知らせる。
- `archive` は作業名に使えない。`archive/` にある名前も再利用できない。
- 作業ごとにワークスペースを作る旧形式の記録（`agents[]` に `role` が無いもの）は、`hub-work finish`・`hub-work resume` だけができる。

### rules と agent-settings.json

`hub-work`・`hub-agent launch`・`herdr-reset` が起動する claude に、次を渡す。agent の `args` とは併用できる（`--settings` と `--append-system-prompt-file` を `args` に書くと衝突として止まる）。

- `--append-system-prompt-file`: 役割のファイル（commander・lead・crew なら hub の `roles/<役割>.md`）と `<hub-env>/rules/*.md` を連結した生成ファイル。`$XDG_STATE_HOME/hub/envs/<環境>-<ハッシュ>/` に、役割ごとに `<役割>-prompt.md`、常駐エージェント用に `agent-rules.md`（rules が無ければ渡さない）を置く
- `--settings`: `<hub-env>/agent-settings.json`（あれば）

反映されるのは、次に起動したエージェントから。`setup` のスクリプトが自分で起動するエージェントには自動では届かないので、スクリプト側で環境変数 `HUB_CLAUDE_ARGS`（シェル用に引用済み）を claude の引数に足す。

### backend なし（backend = "none"）

`hub-work start` と `hub-lead assign` は worktree と `status.md` を作り、エージェントは起動せずに、起動コマンドを表示する。依頼文は作業ディレクトリの `brief-<エージェント>.md` に置かれ、表示されたコマンドが最初のプロンプトとして渡す。通知は届かないので、司令塔と lead は `status.md` と `crew-<repo>.md` を読んで進み具合を確かめる。

## herdr で使う（contrib/herdr）

`hub.toml` に `backend = "herdr"` と `[herdr]` を書く。

```sh
herdr-reset         # ワークスペースを全部閉じて、hub.toml の通りに作り直す（作業のタブも閉じる。hub-work resume で立て直す）
herdr-reset todo    # todo ワークスペース（[[herdr.workspace]] の label）だけを作り直す（作業のタブは残す）
herdr-reset [<label>...] --handoff "<引き継ぎ文>"   # 立て直したあと、新しい司令塔へ引き継ぐ
herdr-reset [<label>...] --resume   # 司令塔のセッションを復帰して立て直す
```

自動では実行されない。OS の再起動などで herdr がレイアウトだけを戻したときも、`herdr-reset` を手で実行する。

部分リセットでは、進行中の作業の lead・crew のタブを残し、hub.toml のタブをその後ろに作り直す。setup のワークスペースはまるごと作り直すので、作業のタブがあれば止まる。

### 引き継ぎ（herdr-reset --handoff）

`herdr-reset --handoff "<引き継ぎ文>"` で、立て直したあとの司令塔へ作業を引き継げる。

```sh
herdr-reset --handoff "目的: ...
現状: ...
次の手順: ..."
```

### setup（スクリプトで組み立てるワークスペース）

```toml
[[agent]]
name = "todo"
cwd = "todo"
launched_by = "setup"

[[herdr.workspace]]
label = "todo"
cwd = "todo"
setup = "todo/contrib/herdr/todo-herdr"
agents = ["todo"]
```

`herdr-reset` は既存のワークスペースを閉じてからスクリプトを実行する。そのため `setup` のワークスペースを単体でリセットすると、サイドバーの末尾へ移る。

### 旧形式の hub.toml の書き換え

`[[agent]]` の無い旧形式（`session` と `[[workspace]]` を直に書く形）は読めないので、次で新形式に書き換える。司令塔の名前 `hub` は `commander` になる。

```sh
migrate-hub-toml <hub-env>/hub.toml           # 新形式を表示する
migrate-hub-toml <hub-env>/hub.toml --write   # hub.toml.bak を残して書き換える
```

## フック

### hub-commit-guard（git フックを飛ばす操作を拒否するフック）

Bash のコマンド文字列を調べ、git フックを飛ばす次のものを含めば拒否する。

- `--no-verify`
- `git commit -n`（`-an` のような束ねを含む）
- `core.hooksPath`（`-c`・`--config-env`・`GIT_CONFIG_*`・`git config` による指定）。`git config --get core.hooksPath` のように読むだけなら許可する

文字列の検査なので、コミットメッセージ中の文字列なども拒否する。これは誤操作の防止であり、セキュリティの境界ではない。

main / master への直接コミットや push は止めない。それは各リポジトリの git フックで止める。

### hub-guard（書き込み先を制限するフック）

Edit / Write / NotebookEdit について、`..` とシンボリックリンクを解決した実際の書き込み先で判定する。判定できないときも拒否する。

- `hub-guard --root <hub-env>`: 司令塔用。hub-env の外なら拒否する。
- `hub-guard --allow <dir>…`: lead・crew 用。どの `<dir>` の中でもなければ拒否する。

**Bash 経由の書き込みは対象外。** `echo > ../todo/x` のような操作は止められないので、これは誤操作の防止であり、セキュリティの境界ではない。

## テスト

```sh
tests/run.sh small    # 単体テスト
tests/run.sh medium   # 結合テスト
tests/run.sh          # 両方
```

- small: `tests/test_hub_*.py`。フック、backend の選択と環境の特定、hub.toml の読み込みと書き換え、偽の backend での hub-work・hub-lead・hub-crew の単体テスト。
- medium: `tests/integration.sh`。一時ディレクトリの環境と専用の herdr サーバーで、hub のコマンドを通しで試す。実際のセッションには触らない。`HUB_TEST_GROUPS="base wtshape"` で一部のグループだけを流せる。
- large: 本物の Claude や実際のセッションでの確認。`run.sh` の対象外で、手で行う。

## ライセンス

[MIT](LICENSE)
