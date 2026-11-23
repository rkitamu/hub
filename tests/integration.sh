#!/usr/bin/env bash
# hub のコマンドを、一時ディレクトリの環境と専用の herdr サーバーで通しで試す。
# 使い方: tests/integration.sh [todo-herdr のパス]
# 実際のセッションに触らないよう、XDG_CONFIG_HOME ごと一時ディレクトリへ移し、ソケットがその中でなければ止まる。
# setup の確認には同梱の代替スクリプトを使う。todo-herdr のパスを渡すと、そのコピーを使う（元のファイルには触らない）。
set -uo pipefail

HUB="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TODO_HERDR="${1:-}"
# NOTE: unix ソケットのパス長の上限があるので、短い場所に作る。
T="$(mktemp -d /tmp/hubi.XXXXXX)"
unset HERDR_ENV HERDR_PANE_ID HERDR_TAB_ID HERDR_WORKSPACE_ID HERDR_SOCKET_PATH HERDR_SESSION
export XDG_CONFIG_HOME="$T/config" XDG_STATE_HOME="$T/state" SHELL=/bin/bash
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
export PATH="$T/bin:$HUB/scripts:$PATH"
SOCK="$T/config/herdr/sessions/hubtest/herdr.sock"
mkdir -p "$T/bin" "$T/config/hub" "$T/state" "$T/argv"
export HUB_TEST_ARGV="$T/argv"

cleanup() {
  if [[ "${HERDR_SOCKET_PATH:-}" == "$T/"* ]]; then herdr server stop >/dev/null 2>&1; fi
  [[ -n "${SERVER_PID:-}" ]] && kill "$SERVER_PID" 2>/dev/null
  rm -rf "$T"
}
trap cleanup EXIT

pass=0 fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
ng() { fail=$((fail + 1)); echo "FAIL - $1"; }
t() { local d="$1"; shift; if "$@" >"$T/out" 2>&1; then ok "$d"; else ng "$d"; sed 's/^/       /' "$T/out"; fi; }
tn() { local d="$1"; shift; if "$@" >"$T/out" 2>&1; then ng "$d（成功してしまった）"; sed 's/^/       /' "$T/out"; else ok "$d"; fi; }
has_ws() { herdr workspace list | jq -e --arg l "$1" 'any(.result.workspaces[]; .label == $l)' >/dev/null; }
no_ws() { ! has_ws "$1"; }
has_agent() { herdr agent list | jq -e --arg n "$1" 'any(.result.agents[]; .name == $n)' >/dev/null; }
no_agent() { ! has_agent "$1"; }
count_ws() { [[ "$(herdr workspace list | jq --arg l "$1" '[.result.workspaces[] | select(.label == $l)] | length')" == "$2" ]]; }
wait_for() { local i; for i in $(seq 1 50); do "$@" && return 0; perl -e 'select(undef,undef,undef,0.2)'; done; return 1; }

# ── 偽の claude と環境 ──────────────────────────────────────────
cat >"$T/bin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" >"$HUB_TEST_ARGV/$(basename "$PWD").argv"
echo "fake claude ready"; printf '> '
while IFS= read -r l; do printf '> '; done
EOF
chmod +x "$T/bin/claude"

E="$T/env"
mkdir -p "$E/hub-env" "$E/app" "$E/todo/contrib/herdr" "$E/todo/scripts" "$E/todo/data/notes"
git -C "$E/hub-env" init -q -b main
printf '/work/\n' >"$E/hub-env/.gitignore"
git -C "$E/app" init -q -b main && git -C "$E/app" commit -q --allow-empty -m init
git -C "$E/todo" init -q -b main && git -C "$E/todo" commit -q --allow-empty -m init
if [[ -n "$TODO_HERDR" ]]; then
  cp "$TODO_HERDR" "$E/todo/contrib/herdr/todo-herdr"
else
  cat >"$E/todo/contrib/herdr/todo-herdr" <<'SETUP'
#!/usr/bin/env bash
# setup の代替: ワークスペースを作り、ビューア 2 つとエージェントを起動する
set -euo pipefail
d="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
created="$(herdr workspace create --cwd "$d" --label todo --no-focus)"
view="$(jq -r .result.root_pane.pane_id <<<"$created")"
notes="$(herdr pane split "$view" --direction right --cwd "$d/data/notes" --no-focus | jq -r .result.pane.pane_id)"
agent="$(herdr pane split "$view" --direction down --cwd "$d" --no-focus | jq -r .result.pane.pane_id)"
for _ in $(seq 1 60); do
  herdr agent start todo --kind claude --pane "$agent" -- --agent todo >/dev/null 2>&1 && break
  sleep 0.5
done
herdr pane run "$view" "$d/scripts/todo-view" >/dev/null
herdr pane run "$notes" "$d/scripts/todo-notes" >/dev/null
SETUP
  chmod +x "$E/todo/contrib/herdr/todo-herdr"
fi
for s in todo-view todo-notes; do printf '#!/bin/bash\nexec sleep 600\n' >"$E/todo/scripts/$s"; chmod +x "$E/todo/scripts/$s"; done
soon="$(date -d '+2 days' +%F)"; far="$(date -d '+60 days' +%F)"
cat >"$E/todo/data/active.md" <<EOF
# TODO

## Necessity（重要・緊急）

| id | status | name | start | due | notes |
|----|--------|------|-------|-----|-------|
| 30 | none | 近い期限 | - | $soon | - |
| 31 | none | 遠い期限 | - | $far | - |
EOF
cat >"$E/hub-env/hub.toml" <<'EOF'
session = "hubtest"

[[workspace]]
label = "hub-env"
cwd = "hub-env"
[[workspace.tab]]
label = "Claude"
[[workspace.tab.pane]]
name = "claude"
agent = { name = "hub", args = ["--model", "test-model"] }

[[workspace]]
label = "todo"
cwd = "todo"
setup = "todo/contrib/herdr/todo-herdr"
agents = ["todo"]

[[workspace]]
label = "app"
cwd = "app"
EOF
cat >"$E/hub-env/MAP.md" <<'EOF'
# 地図

## リポジトリ

| リポジトリ | 状態 | 関心事 | 入口のドキュメント |
|---|---|---|---|
| `hub-env` | 稼働 | 司令塔 | - |
| `todo` | 稼働 | TODO | - |
| `app` | 稼働 | アプリ | - |
| `finance` | 未作成 | 家計簿 | - |

## herdr 上のエージェント

| 名前 | herdr の場所 | 起動 | できること |
|---|---|---|---|
| `hub` | hub-env | hub-reset | 司令塔 |
| `todo` | todo | hub-reset todo | TODO |
EOF
echo "$E" >"$T/config/hub/envs"
mkdir "$E/hub-env/rules"
printf '環境ルール-固有文字列\n' >"$E/hub-env/rules/local.md"
sed -e "s|{{HUB_ENV}}|$E/hub-env|g" -e "s|{{HUB}}|$HUB|g" "$HUB/templates/agent-settings.json" >"$E/hub-env/agent-settings.json"

# ── 専用サーバー ───────────────────────────────────────────────
setsid herdr --session hubtest server >"$T/server.log" 2>&1 </dev/null &
SERVER_PID=$!
wait_for test -S "$SOCK" || { echo "サーバーが起動しませんでした"; exit 1; }
export HERDR_SOCKET_PATH="$SOCK" HERDR_SESSION=hubtest
[[ "$HERDR_SOCKET_PATH" == /tmp/hubi.* ]] || { echo "ソケットが一時ディレクトリの外です"; exit 1; }
wait_for herdr workspace list >/dev/null
herdr workspace create --cwd "$T" --label stale --no-focus >/dev/null

echo "# hub-reset: 全体（setup あり）"
t "全体リセットが成功する" hub-reset
t "古いワークスペースが消える" no_ws stale
t "hub-env ができる" has_ws hub-env
t "todo が setup で 1 つだけできる" count_ws todo 1
t "setup が todo エージェントを起動する" has_agent todo
t "仮のワークスペースが残らない" no_ws hub-reset-tmp
viewers() { herdr pane list | jq -r '.result.panes[].pane_id' | while read -r p; do herdr pane process-info --pane "$p" 2>/dev/null; done | grep -c 'sleep 600' | grep -qx 2; }
t "todo-view と todo-notes が動く" wait_for viewers

echo "# rules と agent-settings"
A="$T/argv/hub-env.argv"
t "agent の args を保つ" grep -qx -- 'test-model' "$A"
t "--settings を渡す" grep -qx -- "$E/hub-env/agent-settings.json" "$A"
t "--append-system-prompt-file を渡す" grep -qx -- '--append-system-prompt-file' "$A"
R="$(grep -A1 -- '--append-system-prompt-file' "$A" | tail -1)"
t "生成物は state ディレクトリにある" bash -c "[[ '$R' == '$T/state/hub/envs/'* ]]"
t "環境の rules が含まれる" grep -q '環境ルール-固有文字列' "$R"
launch_args() { python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; import hublib; print("\n".join(hublib.claude_launch_args(Path(sys.argv[2]))))' "$HUB/lib" "$E"; }
no_rules_arg() { local out; out="$(launch_args)" && ! grep -qx -- '--append-system-prompt-file' <<<"$out"; }
has_rules_arg() { launch_args | grep -qx -- '--append-system-prompt-file' && grep -q '環境ルール-固有文字列' "$R"; }
mv "$E/hub-env/rules/local.md" "$T/local.md"
t "rules が空なら引数を渡さない" no_rules_arg
t "rules が空なら生成物を残さない" test ! -e "$R"
rmdir "$E/hub-env/rules"
t "rules が無ければ引数を渡さない" no_rules_arg
mkdir "$E/hub-env/rules"; mv "$T/local.md" "$E/hub-env/rules/local.md"
t "rules を戻すと引数を渡す" has_rules_arg
t "雛形の settings が JSON として正しい" jq -e '.hooks.PreToolUse[0].matcher == "Bash"' "$E/hub-env/agent-settings.json"
t "雛形は work の直接コミットを許可する" bash -c "jq -r '.hooks.PreToolUse[0].hooks[0].command' '$E/hub-env/agent-settings.json' | grep -qF -- '--allow \"$E/hub-env/work\"'"
tn "雛形は hub-env の直接コミットを許可しない" bash -c "jq -r '.hooks.PreToolUse[0].hooks[0].command' '$E/hub-env/agent-settings.json' | grep -qF -- '--allow \"$E/hub-env\"'"
cp "$E/hub-env/agent-settings.json" "$T/settings.bak"; echo '{broken' >"$E/hub-env/agent-settings.json"
tn "壊れた settings ではリセットしない" hub-reset
t "壊れた settings では何も閉じない" has_agent hub
cp "$T/settings.bak" "$E/hub-env/agent-settings.json"

echo "# hub-reset: 単体（setup あり）"
old="$(herdr workspace list | jq -r '.result.workspaces[] | select(.label == "todo") | .workspace_id')"
t "todo だけ作り直せる" hub-reset todo
new="$(herdr workspace list | jq -r '.result.workspaces[] | select(.label == "todo") | .workspace_id')"
t "ワークスペースが新しくなる" test "$old" != "$new"
t "todo は 1 つのまま" count_ws todo 1
t "todo エージェントが戻る" has_agent todo
t "他のワークスペースは残る" has_ws app

echo "# hub-work: 検証"
tn "archive は作業名に使えない" hub-work start archive --repo app
tn "不正な作業名を拒否する" hub-work start ../evil --repo app
tn "存在しないリポジトリを拒否する" hub-work start nope --repo missing
tn "hub-env を対象にできない" hub-work start nope --repo hub-env
t "拒否では文書を作らない" test ! -e "$E/hub-env/work/nope"

echo "# hub-work: start / list"
t "通常の作業を始める" hub-work start plain --repo app --todo 30 --purpose 目的A --request 依頼A
W="$E/hub-env/work"
t "status.md ができる" test -f "$W/plain/status.md"
t "作業ディレクトリは独立した git" test -d "$W/.git"
t "hub-env の git から外れている" git -C "$E/hub-env" check-ignore -q work/
t "front matter に state がある" grep -qx 'state: active' "$W/plain/status.md"
t "本文に目的がある" grep -q '目的A' "$W/plain/status.md"
t "w-plain ができる" has_ws w-plain
t "エージェントができる" has_agent w-plain
tn "同名の作業を拒否する" hub-work start plain --repo app
t "worktree の作業を始める" hub-work start tree --repo app --worktree
t "worktree ができる" test -d "$E/.hub-worktrees/tree/app"
t "作業ブランチができる" git -C "$E/app" rev-parse --verify -q refs/heads/work/tree
t "list に出る" bash -c 'hub-work list | grep -q "^tree"'

echo "# hub-work: 作業ディレクトリと権限"
t "start は runbook.md を作らない" test ! -e "$W/plain/runbook.md"
t "start は data/ を作らない" test ! -e "$W/plain/data"
A="$T/argv/app.argv"
t "worktree の作業に自分の場所を許可" grep -qxF "Edit(/$W/tree/**)" "$A"
t "Write も許可" grep -qxF "Write(/$W/tree/**)" "$A"
t "--add-dir は自分の場所" grep -qxF "$W/tree" "$A"
tn "他の作業の場所は許可しない" grep -qF "$W/plain" "$A"
tn "work 全体は許可しない" grep -qxF "Edit(/$W/**)" "$A"
t "作業エージェントにも rules を渡す" grep -qx -- '--append-system-prompt-file' "$A"
mkdir -p "$W/plain/data"; echo secret >"$W/plain/data/x.csv"; echo memo >"$W/plain/runbook.md"
t "data/ は git から外れる" git -C "$W" check-ignore -q plain/data/x.csv
tn "runbook.md は外れない" git -C "$W" check-ignore -q plain/runbook.md
tn "status.md は外れない" git -C "$W" check-ignore -q plain/status.md
t "archive 内の data/ も外れる" git -C "$W" check-ignore -q archive/plain/data/x.csv
tn "作業名 data の文書は外れない" git -C "$W" check-ignore -q archive/data/status.md

echo "# hub-status / hub-check"
hub-status >"$T/status" 2>&1
t "status: エージェント" grep -q '^w-plain' "$T/status"
t "status: 作業" grep -q '^tree' "$T/status"
t "status: 近い TODO を出す" grep -q '#30' "$T/status"
tn "status: 遠い TODO は出さない" grep -q '#31' "$T/status"
echo dirty >"$E/app/dirty.txt"
t "status: 未コミットを出す" bash -c 'hub-status | grep -q "^app"'
rm "$E/app/dirty.txt"
t "check: ずれなし" hub-check
sed -i '/`app` | 稼働/d' "$E/hub-env/MAP.md"
tn "check: MAP の抜けを検出する" hub-check
git -C "$E/app" status >/dev/null
printf '| `app` | 稼働 | アプリ | - |\n' >"$T/row"
sed -i "/\`todo\` | 稼働/r $T/row" "$E/hub-env/MAP.md"
t "check: 直すと通る" hub-check

echo "# hub-reset: worktree がつながった状態"
t "todo に worktree の作業を始める" hub-work start ttree --repo todo --worktree
hub-reset todo >"$T/reset" 2>&1; rc=$?
t "単体リセットは他の作業を巻き込まず止まる（setup + worktree）" test "$rc" -ne 0
t "止まる理由が worktree のつながり" grep -q 'つながっています' "$T/reset"
t "止まった後も todo は残る" has_ws todo
t "止まった後も worktree の作業は残る" has_ws w-ttree
t "ttree を片付ける" hub-work finish ttree

echo "# hub-work: finish"
echo x >"$E/.hub-worktrees/tree/app/dirty"
tn "未コミットの変更があれば止める" hub-work finish tree
t "止めたとき worktree は残る" test -f "$E/.hub-worktrees/tree/app/dirty"
t "止めたときワークスペースは残る" has_ws w-tree
t "止めたときエージェントは残る" has_agent w-tree
t "止めたとき archive へ移さない" test ! -e "$W/archive/tree"
rm "$E/.hub-worktrees/tree/app/dirty"
t "tree を finish できる" hub-work finish tree
t "worktree が消える" test ! -e "$E/.hub-worktrees/tree"
t "ブランチは残る" git -C "$E/app" rev-parse --verify -q refs/heads/work/tree
t "archive へ移る" test -f "$W/archive/tree/status.md"
t "archive の state は done" grep -qx 'state: done' "$W/archive/tree/status.md"
t "他の作業は残る" has_agent w-plain
tn "archive 内と同名を拒否する" hub-work start tree --repo app

echo "# hub-work: 親になったワークスペース"
mkdir "$E/solo" && git -C "$E/solo" init -q -b main && git -C "$E/solo" commit -q --allow-empty -m init
t "solo で通常の作業を始める" hub-work start psolo --repo solo
t "solo で worktree の作業を始める" hub-work start tsolo --repo solo --worktree
hub-work finish psolo >"$T/finish" 2>&1; rc=$?
t "親の作業の finish は止まる" test "$rc" -ne 0
t "止まる理由が worktree のつながり" grep -q 'つながっている' "$T/finish"
t "親のワークスペースは残る" has_ws w-psolo
t "つながった作業は無事" has_agent w-tsolo
t "子を先に finish できる" hub-work finish tsolo
t "その後で親を finish できる" hub-work finish psolo

echo "# hub-work: herdr が自動で作る親"
t "元のワークスペースが無いリポジトリで worktree の作業を始める" hub-work start auto --repo solo --worktree
t "herdr が親を自動で作る" has_ws solo
t "親が status.md に記録される" grep -q '"source"' "$W/auto/status.md"
t "finish できる" hub-work finish auto
t "自動で作られた親も閉じる" no_ws solo
rm -rf "$E/solo"

echo "# hub-work: 依頼を届けられないとき"
mkdir "$T/failbin"
cat >"$T/failbin/herdr" <<EOF
#!/bin/bash
if [[ "\$1 \$2" == "agent prompt" ]]; then echo '{"error":{"code":"agent_blocked","message":"blocked"}}' >&2; exit 1; fi
exec "$(command -v herdr)" "\$@"
EOF
chmod +x "$T/failbin/herdr"
PATH="$T/failbin:$PATH" hub-work start blocked --repo app --request 消えてはいけない依頼 >"$T/blocked" 2>&1; rc=$?
t "届かなければ失敗を返す" test "$rc" -ne 0
t "再送のコマンドを示す" grep -q 'herdr agent prompt w-blocked' "$T/blocked"
t "依頼文が status.md に残る" grep -q '消えてはいけない依頼' "$W/blocked/status.md"
t "エージェントは残る" has_agent w-blocked
t "blocked を片付ける" hub-work finish blocked

echo "# hub-work: resume（hub-reset の後）"
t "worktree の作業を始める" hub-work start again --repo app --worktree
t "worktree があっても全体リセットできる" hub-reset
t "リセットで作業のワークスペースが閉じる" no_ws w-again
t "リセットしても checkout は残る" test -d "$E/.hub-worktrees/again/app"
t "worktree の作業を立て直す" hub-work resume again
t "ワークスペースが戻る" has_ws w-again
t "エージェントが戻る" has_agent w-again
t "通常の作業を立て直す" hub-work resume plain
t "通常の作業のエージェントが戻る" has_agent w-plain
t "resume は冪等" hub-work resume again
t "二重に作らない" count_ws w-again 1
t "again を finish できる" hub-work finish again
hub-work finish plain >"$T/finish-plain" 2>&1; rc=$?
t "plain を finish できる" test "$rc" -eq 0
t "finish が棚卸しを促す" grep -q '棚卸し' "$T/finish-plain"
t "runbook.md も archive へ移る" test -f "$W/archive/plain/runbook.md"
t "data/ も archive へ移る" test -f "$W/archive/plain/data/x.csv"
t "runbook.md はコミットされる" git -C "$W" ls-files --error-unmatch archive/plain/runbook.md
tn "data/ はコミットされない" git -C "$W" ls-files --error-unmatch archive/plain/data/x.csv

echo "# hub-work: 既存の work リポジトリ"
t "古い形の作業を始める" hub-work start legacy --repo app
mkdir "$W/legacy/data"; echo old >"$W/legacy/data/old.csv"
cp "$W/.gitignore" "$T/ignore.bak"; : >"$W/.gitignore"
git -C "$W" add -A && git -C "$W" commit -q -m "old style"
t "（前提）data が追跡されている" git -C "$W" ls-files --error-unmatch legacy/data/old.csv
t "resume で効く" hub-work resume legacy
tn "追跡が外れる" git -C "$W" ls-files --error-unmatch legacy/data/old.csv
t "手元のファイルは残る" test -f "$W/legacy/data/old.csv"
t "legacy を finish できる" hub-work finish legacy

echo "# setup への引数"
cat >"$E/setup-check" <<EOF
#!/bin/bash
printf '%s' "\$HUB_CLAUDE_ARGS" >"$T/setup-args"
herdr workspace create --cwd "\$PWD" --label envcheck --no-focus >/dev/null
EOF
chmod +x "$E/setup-check"
cp "$E/hub-env/hub.toml" "$T/toml.bak"
printf '\n[[workspace]]\nlabel = "envcheck"\ncwd = "app"\nsetup = "setup-check"\n' >>"$E/hub-env/hub.toml"
t "setup を実行できる" hub-reset envcheck
t "setup に rules の引数が渡る" grep -q -- '--append-system-prompt-file' "$T/setup-args"
envcheck_id="$(herdr workspace list | jq -r '.result.workspaces[] | select(.label == "envcheck") | .workspace_id')"
herdr workspace close "$envcheck_id" >/dev/null
cp "$T/toml.bak" "$E/hub-env/hub.toml"
t "plain のワークスペースが閉じる" no_ws w-plain
t "作業文書がコミットされる" bash -c "git -C '$W' log --oneline | grep -q 'finish plain'"

echo "# hub-reset: 先頭が setup"
python3 - "$E/hub-env/hub.toml" <<'EOF'
import re, sys
p = sys.argv[1]; s = p and open(p).read()
head, *blocks = re.split(r"(?=^\[\[workspace\]\]$)", s, flags=re.M)
blocks[0], blocks[1] = blocks[1], blocks[0]
open(p, "w").write(head + "".join(blocks))
EOF
t "先頭が setup でも全体リセットできる" hub-reset
t "todo が 1 つだけある" count_ws todo 1
t "hub-env がある" has_ws hub-env
t "仮のワークスペースが残らない" no_ws hub-reset-tmp

echo
echo "結果: $pass 件成功 / $fail 件失敗"
[[ "$fail" -eq 0 ]]
