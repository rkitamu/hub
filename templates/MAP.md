# <環境名> の地図

この環境に並ぶリポジトリと、それぞれのエージェントの一覧、子ごとの連携ルール。

<!-- hub の install.sh --env が、hub の templates/MAP.md から作った。hub-env の CLAUDE.md が読み込む。
     hub-env と hub の行はそのまま使える。子リポジトリの行は、コメントにある todo の例を参考に、自分の子リポジトリに合わせて足す。 -->

## リポジトリ

| リポジトリ | 状態 | 関心事 | 入口のドキュメント |
|---|---|---|---|
| `hub-env` | 稼働 | 司令塔の作業場所。地図・`hub.toml`・司令塔の設定 | `README.md` |
<!-- 例（作者の手元の todo リポジトリ）。子リポジトリごとに 1 行足す:
| `todo` | 稼働 | TODO（アイゼンハワー・マトリクス）。データは Markdown | `CLAUDE.md`, `.claude/agents/todo.md` |
-->

## herdr 上のエージェント

| 名前 | herdr の場所 | 起動 | できること |
|---|---|---|---|
| `hub` | ワークスペース `hub-env` / タブ `Claude`（cwd は `hub-env`） | `herdr-reset` | 地図をもとに全体の質問に答える、他のエージェントへの指示、横断的な改善 |
<!-- 例（hub.toml のコメントにある todo ワークスペースの例に対応）。hub.toml に agent を書いたら、ここにも 1 行足す:
| `todo` | ワークスペース `todo` / タブ `ToDo` の右ペイン | `herdr-reset todo`（`claude --agent todo`） | TODO の追加・更新・完了、今日やることのピック。作業そのものはしない |
-->

- これらは herdr のセッション `<hub.toml の session>` に置く。
- 指示: `herdr agent prompt <名前> "<依頼>" --wait`、結果の確認: `herdr agent read <名前> --source recent-unwrapped --lines 120`（`--wait` が返すのは状態の JSON だけなので、返答は `read` で読む）
- 一覧に無ければ、上の「起動」列のコマンドで立ち上げる。

## 子ごとの連携ルール

<!-- 子をまたぐ段取りをここに書く。TODO の作業を任せる流れは、スキル delegate（.claude/skills/delegate/SKILL.md）にある -->

<!-- hub-check はこのファイルの「リポジトリ」と「herdr 上のエージェント」の表を hub.toml・実際の状態と照合する。
     まだ無いリポジトリは、状態の列に「未作成」か「構想」と書く。 -->
