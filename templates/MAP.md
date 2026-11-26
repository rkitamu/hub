# <環境名> の地図

この環境に並ぶリポジトリと、それぞれのエージェントの一覧、子ごとの連携ルール。

<!-- hub の install.sh --env が、hub の templates/MAP.md から作った。hub-env の CLAUDE.md が読み込む（commander も lead も読む）。
     hub-env と hub の行はそのまま使える。子リポジトリの行は、コメントにある todo の例を参考に、自分の子リポジトリに合わせて足す。 -->

## リポジトリ

| リポジトリ | 状態 | 関心事 | 入口のドキュメント |
|---|---|---|---|
| `hub-env` | 稼働 | 司令塔の作業場所。地図・`hub.toml`・エージェントの設定 | `README.md` |
<!-- 例（作者の手元の todo リポジトリ）。子リポジトリごとに 1 行足す:
| `todo` | 稼働 | TODO（アイゼンハワー・マトリクス）。データは Markdown | `CLAUDE.md`, `.claude/agents/todo.md` |
-->

## エージェント

| 名前 | cwd | 起動 | できること |
|---|---|---|---|
| `commander` | `hub-env`（herdr ではワークスペース `hub-env` / タブ `Claude`） | `herdr-reset` | 地図をもとに全体の質問に答える、依頼や相談を作業にして lead を立てる（`hub-work start`）、常駐エージェントへの指示 |
<!-- 例（hub.toml のコメントにある todo の例に対応）。hub.toml に [[agent]] を書いたら、ここにも 1 行足す:
| `todo` | `todo`（herdr ではワークスペース `todo` / タブ `ToDo` の右ペイン） | `herdr-reset todo`（`claude --agent todo`） | TODO の追加・更新・完了、今日やることのピック。作業そのものはしない |
-->

- 作業ごとの lead（`w-<作業名>`）と crew（`w-<作業名>-<repo>`）は、`hub-work start` と `hub-lead assign` が立てるので、この表には書かない。
- 指示: `hub-agent send <名前> "<依頼>"`、結果の確認: `hub-agent read <名前> --lines 120`（lead は `hub-lead send` / `read`）
- 一覧（`hub-agent list`）に無ければ、上の「起動」列のコマンドで立ち上げる。backend が無い環境では `hub-agent launch <名前>` で起動する。

## 子ごとの連携ルール

<!-- 子をまたぐ段取りをここに書く。作業の始め方と進め方は、hub の roles/commander.md・roles/lead.md・roles/crew.md にある -->

<!-- hub-check はこのファイルの「リポジトリ」と「エージェント」の表を hub.toml・実際の状態と照合する。
     まだ無いリポジトリは、状態の列に「未作成」か「構想」と書く。 -->
