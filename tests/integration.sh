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
# NOTE: hub-work が信頼の登録で本物の ~/.claude.json を書かないよう、設定ごと移す。
export CLAUDE_CONFIG_DIR="$T/claude"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
export PATH="$T/bin:$HUB/scripts:$PATH"
SOCK="$T/config/herdr/sessions/hubtest/herdr.sock"
mkdir -p "$T/bin" "$T/config/hub" "$T/state" "$T/argv" "$T/claude"
echo '{"projects":{}}' >"$T/claude/.claude.json"
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
# 本物と同じ形の入力で SessionStart フックを実行する。--resume なら同じ ID を使う
sid="$(cat /proc/sys/kernel/random/uuid)"; settings=""; a=("$@")
for i in "${!a[@]}"; do
  [[ "${a[$i]}" == --resume ]] && sid="${a[$((i + 1))]}"
  [[ "${a[$i]}" == --settings ]] && settings="${a[$((i + 1))]}"
done
tr="$HUB_TEST_ARGV/transcripts/$sid.jsonl"; mkdir -p "${tr%/*}"; : >>"$tr"
if [[ -n "$settings" ]]; then
  jq -r '.hooks.SessionStart[]?.hooks[]?.command' "$settings" | while IFS= read -r c; do
    jq -n --arg s "$sid" --arg t "$tr" --arg c "$PWD" '{hook_event_name:"SessionStart",session_id:$s,transcript_path:$t,cwd:$c,source:"startup"}' | bash -c "$c"
  done
fi
echo "fake claude ready"; printf '> '
while IFS= read -r l; do printf '%s\n' "$l" >>"$HUB_TEST_ARGV/$(basename "$PWD").input"; printf '> '; done
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
HS="$(grep -A1 -- '--settings' "$A" | tail -1)"
t "hub に生成した設定を渡す" test "$HS" = "$(dirname "$(grep -A1 -- '--append-system-prompt-file' "$A" | tail -1)")/hub-agent-settings.json"
t "生成した設定は agent-settings のフックを保つ" jq -e '.hooks.PreToolUse[0].matcher == "Bash"' "$HS"
t "生成した設定に記録フックがある" jq -e '.hooks.SessionStart[0].hooks[0].command | contains("hub-session-record")' "$HS"
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
t "雛形は hub-commit-guard を登録する" bash -c "jq -r '.hooks.PreToolUse[0].hooks[0].command' '$E/hub-env/agent-settings.json' | grep -qF -- '$HUB/scripts/hub-commit-guard'"
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
tn "hub-env は --worktree なしでは対象にできない" hub-work start nope --repo hub-env --request x
tn "hub-env は --request なしでは対象にできない" hub-work start nope --repo hub-env --worktree
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
t "Read も許可" grep -qxF "Read(/$W/tree/**)" "$A"
tn "--add-dir は使わない" grep -qx -- '--add-dir' "$A"
tn "他の作業の場所は許可しない" grep -qF "$W/plain" "$A"
tn "work 全体は許可しない" grep -qxF "Edit(/$W/**)" "$A"
t "作業エージェントにも rules を渡す" grep -qx -- '--append-system-prompt-file' "$A"
t "hub 以外には agent-settings をそのまま渡す" grep -qx -- "$E/hub-env/agent-settings.json" "$A"
tn "hub 以外には記録フックの設定を渡さない" grep -q hub-agent-settings "$A"
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

echo "# hub-work: フォルダ信頼"
CJ="$T/claude/.claude.json"
RE="$(cd "$E" && pwd -P)"
trusted() { jq -e --arg p "$1" '.projects[$p].hasTrustDialogAccepted == true' "$CJ" >/dev/null; }
jq --arg p "$RE/app" '.projects[$p] = {hasTrustDialogAccepted: true} | .keep = 1' "$CJ" >"$CJ.new" && mv "$CJ.new" "$CJ"
t "信頼済みのリポジトリで worktree の作業を始める" hub-work start trusta --repo app --worktree
t "worktree が信頼済みになる" trusted "$RE/.hub-worktrees/trusta/app"
t "他の設定を保つ" jq -e '.keep == 1' "$CJ"
A="$T/argv/app.argv"
tn "git と gh の自動許可を持たない" grep -qE '^Bash\((git|gh) ' "$A"
t "trusta を片付ける" hub-work finish trusta
tn "finish で信頼の登録を外す" trusted "$RE/.hub-worktrees/trusta/app"
mkdir "$E/plainrepo" && git -C "$E/plainrepo" init -q -b main && git -C "$E/plainrepo" commit -q --allow-empty -m init
hub-work start trustb --repo plainrepo --worktree >"$T/trustb" 2>&1
t "未信頼のリポジトリは警告する" grep -q '信頼済みではありません' "$T/trustb"
tn "未信頼のリポジトリの worktree は登録しない" trusted "$RE/.hub-worktrees/trustb/plainrepo"
t "trustb を片付ける" hub-work finish trustb
mkdir "$CJ.lock"
hub-work start trustc --repo app --worktree >"$T/trustc" 2>&1
rmdir "$CJ.lock"
t "登録できないときは警告する" grep -q '信頼済みにできませんでした' "$T/trustc"
tn "登録できないときは書かない" trusted "$RE/.hub-worktrees/trustc/app"
t "trustc を片付ける" hub-work finish trustc
rm -rf "$E/plainrepo"

echo "# hub-work: hub-env と環境の外のリポジトリ"
mkdir -p "$T/outside/hub" && git -C "$T/outside/hub" init -q -b main && git -C "$T/outside/hub" commit -q --allow-empty -m init
git -C "$E/hub-env" add -A >/dev/null && git -C "$E/hub-env" commit -q -m init
t "hub-env を worktree で対象にできる" hub-work start henv --repo hub-env --worktree --request 地図の変更
t "hub-env の worktree ができる" test -f "$E/.hub-worktrees/henv/hub-env/.git"
t "役割を依頼文で渡す" bash -c "herdr agent read w-henv --source recent-unwrapped --lines 40 | grep -q 司令塔ではなく"
t "henv を片付ける" hub-work finish henv
t "環境の外の hub を対象にできる" hub-work start outer --repo ../outside/hub --worktree
t "環境の外の worktree ができる" test -f "$E/.hub-worktrees/outer/hub/.git"
t "outer を片付ける" hub-work finish outer
t "環境の外の hub を worktree なしで対象にできる" hub-work start outerb --repo ../outside/hub
t "outerb を片付ける" hub-work finish outerb
tn "存在しない外のパスは拒否する" hub-work start outerc --repo ../outside/none

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

echo "# hub-reset --handoff"
HF="$(dirname "$R")/handoff.md"
HI="$T/argv/hub-env.input"
REAL_HERDR="$(command -v herdr)"
mkdir "$T/handoffbin"
# agent prompt の結果を HANDOFF_STUB で差し替え、呼び出しを数える。全呼び出しの引数と環境も記録する
cat >"$T/handoffbin/herdr" <<EOF
#!/bin/bash
env >>"$T/handoff-env"
if [[ "\$1 \$2" == "agent prompt" ]]; then
  echo x >>"$T/handoff-prompts"
  n="\$(wc -l <"$T/handoff-prompts")"
  case "\${HANDOFF_STUB:-}" in
    fail) echo '{"error":{"code":"agent_not_found","message":"x"}}' >&2; exit 1 ;;
    timeout) echo '{"error":{"code":"timeout","message":"x"}}' >&2; exit 1 ;;
    sent-timeout) "$REAL_HERDR" "\$@" >/dev/null; echo '{"error":{"code":"timeout","message":"x"}}' >&2; exit 1 ;;
    blocked) echo '{"error":{"code":"agent_blocked","message":"x"}}' >&2; exit 1 ;;
    blocked-once) if [[ "\$n" == 1 ]]; then echo '{"error":{"code":"agent_blocked","message":"x"}}' >&2; exit 1; fi ;;
  esac
fi
exec "$REAL_HERDR" "\$@"
EOF
chmod +x "$T/handoffbin/herdr"
hstub() { local mode="$1"; shift; : >"$T/handoff-prompts"; PATH="$T/handoffbin:$PATH" HANDOFF_STUB="$mode" HUB_RESET_HANDOFF_WAIT=3 hub-reset "$@"; }
prompts_are() { [[ "$(wc -l <"$T/handoff-prompts")" == "$1" ]]; }
ws_id() { herdr workspace list | jq -r --arg l "$1" '.result.workspaces[] | select(.label == $l) | .workspace_id'; }
tab_ids() { herdr tab list --workspace "$(ws_id "$1")" | jq -c '[.result.tabs[].tab_id]'; }
wait_long() { local i; for i in $(seq 1 300); do "$@" && return 0; perl -e 'select(undef,undef,undef,0.2)'; done; return 1; }
input_has() { [[ -f "$HI" && "$(cat "$HI")" == *"$1"* ]]; }
input_count() { [[ "$(grep -c -F -- "$1" "$HI")" == "$2" ]]; }
SPECIAL=$'--help -x "二重" \'単一\' $HOME `id` \\back\n二行目 ; rm -rf nope && echo x\n\n  字下げ-固有文字列'

: >"$HI"
t "特殊文字の引き継ぎで全体を立て直せる" hub-reset --handoff "$SPECIAL"
t "引き継ぎ文が壊れずに届く" wait_for input_has "$SPECIAL"
t "前置きが添えられる" input_has '前のセッションの許可は引き継がれていません'
t "確認の前置きが添えられる" input_has '改めてユーザーに確認してください'
t "配送に成功したら handoff.md を消す" test ! -e "$HF"
t "全エージェントを立て直している" has_agent todo

: >"$HI"
t "- で始まる文を値として扱う" hub-reset hub-env --handoff -h
t "-h が届く" wait_for input_has $'--- 引き継ぎ文 ---\n-h'
t "--handoff=値 の形も使える" bash -c ': >"$1"; hub-reset hub-env "--handoff=等号の値-固有文字列"' _ "$HI"
t "等号の値が届く" wait_for input_has '等号の値-固有文字列'

before="$(tab_ids hub-env)"
tn "空文字はエラー" hub-reset hub-env --handoff ""
tn "空白だけはエラー" hub-reset hub-env --handoff "   "
tn "値の欠落はエラー" hub-reset hub-env --handoff
tn "二重指定はエラー" hub-reset hub-env --handoff a --handoff b
t "引数エラーでは何も閉じない" test "$before" = "$(tab_ids hub-env)"
t "引数エラーでは保存しない" test ! -e "$HF"

before="$(tab_ids app)"; hub_before="$(tab_ids hub-env)"
hub-reset app --handoff 対象外 >"$T/hout" 2>&1; rc=$?
t "hub が対象外ならエラー" test "$rc" -ne 0
t "エラーが hub を示す" grep -q 'エージェント hub' "$T/hout"
t "hub が対象外なら対象を閉じない" test "$before" = "$(tab_ids app)"
t "hub が対象外なら hub も閉じない" test "$hub_before" = "$(tab_ids hub-env)"
t "hub が対象外なら保存しない" test ! -e "$HF"

hstub fail hub-env --handoff '配送失敗-固有文字列' >"$T/hout" 2>&1; rc=$?
t "配送に失敗したら失敗を返す" test "$rc" -ne 0
t "配送に失敗したら残す" test -f "$HF"
t "handoff.md の権限は 600" test "$(stat -c %a "$HF")" = 600
t "handoff.md に引き継ぎ文がある" grep -q '配送失敗-固有文字列' "$HF"
t "失敗のログに場所を出す" grep -qF "$HF" "$T/hout"
tn "失敗のログに引き継ぎ文を出さない" grep -q '配送失敗-固有文字列' "$T/hout"
cp "$HF" "$T/handoff.keep"; before="$(tab_ids hub-env)"
hub-reset hub-env --handoff 別の文 >"$T/hout" 2>&1; rc=$?
t "未配送があると止まる" test "$rc" -ne 0
t "止まる理由に場所を示す" grep -qF "$HF" "$T/hout"
t "未配送のファイルを上書きしない" cmp "$HF" "$T/handoff.keep"
t "未配送があれば何も閉じない" test "$before" = "$(tab_ids hub-env)"
rm "$HF"

: >"$HI"
hstub timeout hub-env --handoff 'timeout未着-固有文字列' >"$T/hout" 2>&1; rc=$?
t "timeout で画面に無ければ失敗を返す" test "$rc" -ne 0
t "timeout で画面に無ければ再送しない" prompts_are 1
t "timeout で画面に無ければ残す" test -f "$HF"
rm -f "$HF"
: >"$HI"
hstub sent-timeout hub-env --handoff 'timeout到着-固有文字列' >"$T/hout" 2>&1; rc=$?
t "timeout でも画面にあれば成功" test "$rc" -eq 0
t "timeout でも届いていれば再送しない" prompts_are 1
t "timeout でも届いていれば消す" test ! -e "$HF"
t "二重に届かない" input_count 'timeout到着-固有文字列' 1

: >"$HI"
t "blocked の間は待って送る" hstub blocked-once hub-env --handoff 'blocked解除-固有文字列'
t "blocked の解除後に届く" wait_for input_has 'blocked解除-固有文字列'
t "blocked のあと消す" test ! -e "$HF"
hstub blocked hub-env --handoff 'blocked継続-固有文字列' >"$T/hout" 2>&1; rc=$?
t "blocked が続けば失敗を返す" test "$rc" -ne 0
t "blocked が続けば残す" test -f "$HF"
rm -f "$HF"

cp "$E/hub-env/hub.toml" "$T/toml.handoff"
printf '\n[[workspace]]\nlabel = "broken"\ncwd = "app"\nsetup = "broken-setup"\n' >>"$E/hub-env/hub.toml"
printf '#!/bin/bash\nexit 1\n' >"$E/broken-setup"; chmod +x "$E/broken-setup"
hub-reset --handoff '立て直し失敗-固有文字列' >"$T/hout" 2>&1; rc=$?
t "立て直しに失敗したら失敗を返す" test "$rc" -ne 0
t "立て直しに失敗したら残す" test -f "$HF"
t "立て直しの失敗でも場所を出す" grep -qF "$HF" "$T/hout"
rm -f "$HF" "$E/broken-setup"; cp "$T/toml.handoff" "$E/hub-env/hub.toml"
t "失敗のあと元の構成へ戻せる" hub-reset

: >"$HI"; : >"$T/handoff-env"; : >"$T/handoff-prompts"
own="$(ws_id hub-env)"
PATH="$T/handoffbin:$PATH" HERDR_WORKSPACE_ID="$own" hub-reset --handoff $'裏実行-固有文字列\n二行目' >"$T/hout" 2>&1; rc=$?
t "自分のタブを閉じるときは裏で続ける" test "$rc" -eq 0
t "裏で続けることを知らせる" grep -q '裏で立て直します' "$T/hout"
t "裏の実行でも引き継ぎが届く" wait_long input_has $'裏実行-固有文字列\n二行目'
t "裏の実行でも配送後に消す" wait_long test ! -e "$HF"
t "裏の実行のログに成功が出る" wait_long grep -q '引き継ぎを hub へ届けました' "$T/state/hub-reset.log"
tn "裏の実行のログに引き継ぎ文を出さない" grep -q '裏実行-固有文字列' "$T/state/hub-reset.log"
tn "環境変数に引き継ぎ文を出さない" grep -q '裏実行-固有文字列' "$T/handoff-env"
t "裏の実行は 1 回だけ送る" prompts_are 1

: >"$HI"
before_files="$(ls -A "$(dirname "$HF")")"
t "--handoff なしでも単体で立て直せる" hub-reset hub-env
t "--handoff なしでも全体を立て直せる" hub-reset
perl -e 'select(undef,undef,undef,0.5)'
tn "--handoff なしでは何も送らない" test -s "$HI"
t "--handoff なしでは handoff.md を作らない" test ! -e "$HF"
t "--handoff なしでは state のファイルが増えない" test "$before_files" = "$(ls -A "$(dirname "$HF")")"
t "--help に使い方がある" bash -c 'hub-reset --help | grep -q -- --handoff'

echo "# hub-reset --resume"
SF="$(dirname "$HF")/hub-session.json"
AH="$T/argv/hub-env.argv"; AA="$T/argv/todo.argv"
sid_now() { jq -r .session_id "$SF"; }
resumes() { grep -A1 -x -- '--resume' "$1" | tail -1 | grep -qxF "$2"; }
no_resume() { ! grep -qx -- '--resume' "$1"; }
all_ws() { herdr workspace list | jq -c '[.result.workspaces[].workspace_id]'; }
t "（前提）全体を立て直す" hub-reset
t "hub のセッション ID が記録される" test -f "$SF"
t "記録の権限は 600" test "$(stat -c %a "$SF")" = 600
sid="$(sid_now)"
: >"$HI"; rm -f "$AA"
t "--resume で全体を立て直せる" hub-reset --resume
t "他のエージェントも起動し直される" wait_for test -f "$AA"
t "hub は記録された ID で戻る" resumes "$AH" "$sid"
t "他のエージェントは新規に起動する" no_resume "$AA"
t "resume 後も ID が変わらない" test "$(sid_now)" = "$sid"
perl -e 'select(undef,undef,undef,0.5)'
tn "resume では前置きを送らない" test -s "$HI"
t "--resume で hub だけのワークスペースを立て直せる" hub-reset hub-env --resume
t "単体でも記録された ID で戻る" resumes "$AH" "$sid"
rm -f "$AH"; own="$(ws_id hub-env)"
HERDR_WORKSPACE_ID="$own" hub-reset --resume >"$T/rout" 2>&1; rc=$?
t "自分のタブを閉じるときは裏で続ける" test "$rc" -eq 0
t "裏の実行でも同じ ID で戻る" wait_long bash -c "grep -A1 -x -- --resume '$AH' 2>/dev/null | tail -1 | grep -qxF '$sid'"
wait_long has_agent hub; wait_long has_ws app; wait_long has_agent todo
perl -e 'select(undef,undef,undef,1)'

before="$(all_ws)"; tabs="$(tab_ids hub-env)"
tn "--handoff との同時指定はエラー" hub-reset --handoff 文 --resume
tn "順序が逆でもエラー" hub-reset --resume --handoff 文
tn "hub が対象外ならエラー" hub-reset app --resume
cp "$SF" "$T/session.bak"; rm "$SF"
hub-reset --resume >"$T/rout" 2>&1; rc=$?
t "ID が無ければ止まる" test "$rc" -ne 0
t "ID が無いことを知らせる" grep -q '記録されていません' "$T/rout"
echo '{broken' >"$SF"
tn "記録が壊れていれば止まる" hub-reset --resume
cp "$T/session.bak" "$SF"
TR="$(jq -r .transcript_path "$SF")"; mv "$TR" "$T/transcript.bak"
hub-reset --resume >"$T/rout" 2>&1; rc=$?
t "履歴が無ければ止まる" test "$rc" -ne 0
t "履歴が無いことを知らせる" grep -q '履歴が見つかりません' "$T/rout"
mv "$T/transcript.bak" "$TR"
t "止まったときワークスペースを閉じない" test "$before" = "$(all_ws)"
t "止まったときタブを閉じない" test "$tabs" = "$(tab_ids hub-env)"
t "--resume なしは新規に起動する" hub-reset
t "--resume なしでは再開の引数を渡さない" no_resume "$AH"
t "--resume なしでは新しい ID が記録される" test "$(sid_now)" != "$sid"
t "--help に --resume がある" bash -c 'hub-reset --help | grep -q -- --resume'

echo "# install.sh --env: README"
I="$T/sample-env"
mkdir "$I"
install_env() { HOME="$T/install-home" XDG_CONFIG_HOME="$T/install-config" "$HUB/install.sh" --env "$I"; }
t "環境を作れる" install_env
t "README ができる" test -f "$I/hub-env/README.md"
t "README の見出しに環境名が入る" grep -qxF '# hub-env — sample-env' "$I/hub-env/README.md"
tn "README にプレースホルダーが残らない" grep -qF '{{' "$I/hub-env/README.md"
t "README に頼み方の表がある" grep -qF '## 司令塔への頼み方' "$I/hub-env/README.md"
t "sync-guide は配置しない" test ! -e "$I/hub-env/.claude/skills/sync-guide"
printf '手元の README\n' >"$I/hub-env/README.md"
t "既存の環境にもう一度実行できる" install_env
t "既存の README を上書きしない" grep -qxF '手元の README' "$I/hub-env/README.md"

echo
echo "結果: $pass 件成功 / $fail 件失敗"
[[ "$fail" -eq 0 ]]
