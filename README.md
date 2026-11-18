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

`hub-reset` が `~/.local/bin` にリンクされる。

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
| `.claude/settings.local.json` | 司令塔の Claude Code の設定。環境と hub を読む許可 |

## コマンド

```sh
hub-reset         # ワークスペースを全部閉じて、hub.toml の通りに作り直す
hub-reset todo    # todo ワークスペース（hub.toml の label）だけを作り直す
```

`hub.toml` の `session` に書いたセッションのペインで実行する。

## ライセンス

[MIT](LICENSE)
