#!/usr/bin/env bash
# hub のコマンドを、一時ディレクトリの環境と専用の herdr サーバーで通しで試す。
# 使い方: tests/integration.sh [todo-herdr のパス]
# 実際のセッションに触らないよう、XDG_CONFIG_HOME ごと一時ディレクトリへ移し、ソケットがその中でなければ止まる。
# setup の確認には同梱の代替スクリプトを使う。todo-herdr のパスを渡すと、そのコピーを使う（元のファイルには触らない）。
# 互いに状態を共有しないグループごとに、一時ディレクトリと専用の herdr サーバーを作り、並列に実行する。
set -uo pipefail

HUB="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TODO_HERDR="${1:-}"
# 司令塔の名前と、herdr のワークスペースの見出し
CMDR=commander WSH='[[herdr.workspace]]'
unset HERDR_ENV HERDR_PANE_ID HERDR_TAB_ID HERDR_WORKSPACE_ID HERDR_SOCKET_PATH HERDR_SESSION
export SHELL=/bin/bash
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid

ok() { echo "ok   - $1"; }
ng() { echo "FAIL - $1"; }
t() { local d="$1"; shift; if "$@" >"$T/out" 2>&1; then ok "$d"; else ng "$d"; sed 's/^/       /' "$T/out"; fi; }
tn() { local d="$1"; shift; if "$@" >"$T/out" 2>&1; then ng "$d（成功してしまった）"; sed 's/^/       /' "$T/out"; else ok "$d"; fi; }
has_ws() { herdr workspace list | jq -e --arg l "$1" 'any(.result.workspaces[]; .label == $l)' >/dev/null; }
no_ws() { ! has_ws "$1"; }
has_agent() { herdr agent list | jq -e --arg n "$1" 'any(.result.agents[]; .name == $n)' >/dev/null; }
no_agent() { ! has_agent "$1"; }
count_ws() { [[ "$(herdr workspace list | jq --arg l "$1" '[.result.workspaces[] | select(.label == $l)] | length')" == "$2" ]]; }
wait_for() { local i; for i in $(seq 1 50); do "$@" && return 0; perl -e 'select(undef,undef,undef,0.2)'; done; return 1; }
wait_long() { local i; for i in $(seq 1 300); do "$@" && return 0; perl -e 'select(undef,undef,undef,0.2)'; done; return 1; }
input_has() { [[ -f "$HI" && "$(cat "$HI")" == *"$1"* ]]; }
input_count() { [[ "$(grep -c -F -- "$1" "$HI")" == "$2" ]]; }
ws_id() { herdr workspace list | jq -r --arg l "$1" '.result.workspaces[] | select(.label == $l) | .workspace_id'; }
tab_ids() { herdr tab list --workspace "$(ws_id "$1")" | jq -c '[.result.tabs[].tab_id]'; }
tab_titles() { herdr tab list --workspace "$(ws_id "$1")" | jq -r '[.result.tabs[].label] | join(",")'; }
has_tab() { [[ ",$(tab_titles "$1")," == *",$2,"* ]]; }
no_tab() { ! has_tab "$1" "$2"; }
tab_id() { herdr tab list --workspace "$(ws_id "$1")" | jq -r --arg l "$2" '.result.tabs[] | select(.label == $l) | .tab_id'; }
# lead・crew として実行する: as_lead <作業名> <コマンド>…
as_lead() { local w="$1"; shift; HUB_ROLE=lead HUB_WORK="$w" "$@"; }
as_crew() { local w="$1"; shift; HUB_ROLE=crew HUB_WORK="$w" "$@"; }
screen_has() { herdr agent read "$1" --source recent-unwrapped --lines 200 | grep -qF -- "$2"; }
# 準備の手順。検査の件数には数えず、失敗したときだけ FAIL を出す
prep() { if ! "$@" >"$T/prep" 2>&1; then ng "準備: $*"; sed 's/^/       /' "$T/prep"; fi; }
# 裏で動く herdr-reset が終わったか。裏の実行はログを開いたまま動く
bg_reset_done() { ! fuser -s "$T/state/herdr-reset.log" 2>/dev/null; }

cleanup() {
  if [[ "${HERDR_SOCKET_PATH:-}" == "$T/"* ]]; then herdr server stop >/dev/null 2>&1; fi
  [[ -n "${SERVER_PID:-}" ]] && kill "$SERVER_PID" 2>/dev/null
  rm -rf "$T"
}

# 一時ディレクトリだけを作る
make_tmp() {
# NOTE: unix ソケットのパス長の上限があるので、短い場所に作る。
T="$(mktemp -d /tmp/hubi.XXXXXX)"
trap cleanup EXIT
export XDG_CONFIG_HOME="$T/config" XDG_STATE_HOME="$T/state"
# NOTE: hub-work が信頼の登録で本物の ~/.claude.json を書かないよう、設定ごと移す。
export CLAUDE_CONFIG_DIR="$T/claude"
export PATH="$T/bin:$HUB/scripts:$HUB/contrib/herdr:$PATH"
SOCK="$T/config/herdr/sessions/hubtest/herdr.sock"
mkdir -p "$T/bin" "$T/config/hub" "$T/state" "$T/argv" "$T/claude"
echo '{"projects":{}}' >"$T/claude/.claude.json"
export HUB_TEST_ARGV="$T/argv"
}

# 一時ディレクトリに、偽の claude と環境と専用サーバーを用意する
make_env() {
make_tmp
# ── 偽の claude と環境 ──────────────────────────────────────────
cat >"$T/bin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" >"$HUB_TEST_ARGV/$(basename "$PWD").argv"
env | grep -E '^HUB_(WORK|ROLE)=' | sort >"$HUB_TEST_ARGV/$(basename "$PWD").env"
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

## エージェント

| 名前 | herdr の場所 | 起動 | できること |
|---|---|---|---|
| `commander` | hub-env | herdr-reset | 司令塔 |
| `todo` | todo | herdr-reset todo | TODO |
EOF
# NOTE: 旧形式で書いた hub.toml を migrate-hub-toml で新形式にして使う（書き換えも毎回確かめる）。
"$HUB/contrib/herdr/migrate-hub-toml" "$E/hub-env/hub.toml" --write >/dev/null && rm "$E/hub-env/hub.toml.bak" ||
  { echo "hub.toml を新形式にできませんでした"; exit 1; }
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
W="$E/hub-env/work"
HI="$T/argv/hub-env.input"
}

# hub.toml の先頭を setup のワークスペースにする
swap_first() {
python3 - "$E/hub-env/hub.toml" "$WSH" <<'EOF'
import re, sys
p = sys.argv[1]; s = p and open(p).read()
head, *blocks = re.split(rf"(?=^{re.escape(sys.argv[2])}$)", s, flags=re.M)
blocks[0], blocks[1] = blocks[1], blocks[0]
open(p, "w").write(head + "".join(blocks))
EOF
}

# --handoff の確認に使う道具。herdr-reset の後に呼ぶ
handoff_tools() {
R="$(grep -A1 -- '--append-system-prompt-file' "$T/argv/hub-env.argv" | tail -1)"
HF="$(dirname "$R")/handoff.md"
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
hstub() { local mode="$1"; shift; : >"$T/handoff-prompts"; PATH="$T/handoffbin:$PATH" HANDOFF_STUB="$mode" HERDR_RESET_HANDOFF_WAIT=3 HERDR_RESET_SCREEN_WAIT=1 herdr-reset "$@"; }
prompts_are() { [[ "$(wc -l <"$T/handoff-prompts")" == "$1" ]]; }
SPECIAL=$'--help -x "二重" \'単一\' $HOME `id` \\back\n二行目 ; rm -rf nope && echo x\n\n  字下げ-固有文字列'
}

# ── グループ ───────────────────────────────────────────────

group_base() {
make_env
herdr workspace create --cwd "$T" --label stale --no-focus >/dev/null

echo "# herdr-reset: 全体（setup あり）"
t "全体リセットが成功する" herdr-reset
t "古いワークスペースが消える" no_ws stale
t "hub-env ができる" has_ws hub-env
t "todo が setup で 1 つだけできる" count_ws todo 1
t "setup が todo エージェントを起動する" has_agent todo
t "仮のワークスペースが残らない" no_ws herdr-reset-tmp
viewers() { herdr pane list | jq -r '.result.panes[].pane_id' | while read -r p; do herdr pane process-info --pane "$p" 2>/dev/null; done | grep -c 'sleep 600' | grep -qx 2; }
t "todo-view と todo-notes が動く" wait_for viewers

echo "# rules と agent-settings"
A="$T/argv/hub-env.argv"
t "agent の args を保つ" grep -qx -- 'test-model' "$A"
HS="$(grep -A1 -- '--settings' "$A" | tail -1)"
t "hub に生成した設定を渡す" test "$HS" = "$(dirname "$(grep -A1 -- '--append-system-prompt-file' "$A" | tail -1)")/$CMDR-agent-settings.json"
t "生成した設定は agent-settings のフックを保つ" jq -e '.hooks.PreToolUse[0].matcher == "Bash"' "$HS"
t "生成した設定に記録フックがある" jq -e '.hooks.SessionStart[0].hooks[0].command | contains("hub-session-record")' "$HS"
t "--append-system-prompt-file を渡す" grep -qx -- '--append-system-prompt-file' "$A"
R="$(grep -A1 -- '--append-system-prompt-file' "$A" | tail -1)"
t "生成物は state ディレクトリにある" bash -c "[[ '$R' == '$T/state/hub/envs/'* ]]"
t "環境の rules が含まれる" grep -q '環境ルール-固有文字列' "$R"
launch_args() { python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; import hublib; print("\n".join(hublib.claude_launch_args(Path(sys.argv[2]))))' "$HUB/lib" "$E"; }
no_rules_arg() { local out; out="$(launch_args)" && ! grep -qx -- '--append-system-prompt-file' <<<"$out"; }
has_rules_arg() { launch_args | grep -qx -- '--append-system-prompt-file' && grep -q '環境ルール-固有文字列' "$(dirname "$R")/agent-rules.md"; }
mv "$E/hub-env/rules/local.md" "$T/local.md"
t "rules が空なら引数を渡さない" no_rules_arg
t "rules が空なら常駐エージェント用の生成物を残さない" test ! -e "$(dirname "$R")/agent-rules.md"
rmdir "$E/hub-env/rules"
t "rules が無ければ引数を渡さない" no_rules_arg
mkdir "$E/hub-env/rules"; mv "$T/local.md" "$E/hub-env/rules/local.md"
t "rules を戻すと引数を渡す" has_rules_arg
t "雛形の settings が JSON として正しい" jq -e '.hooks.PreToolUse[0].matcher == "Bash"' "$E/hub-env/agent-settings.json"
t "雛形は hub-commit-guard を登録する" bash -c "jq -r '.hooks.PreToolUse[0].hooks[0].command' '$E/hub-env/agent-settings.json' | grep -qF -- '$HUB/scripts/hub-commit-guard'"
cp "$E/hub-env/agent-settings.json" "$T/settings.bak"; echo '{broken' >"$E/hub-env/agent-settings.json"
tn "壊れた settings ではリセットしない" herdr-reset
t "壊れた settings では何も閉じない" has_agent "$CMDR"
cp "$T/settings.bak" "$E/hub-env/agent-settings.json"


echo "# hub-work: 検証"
tn "archive は作業名に使えない" hub-work start archive
tn "不正な作業名を拒否する" hub-work start ../evil
tn "--repo は受け付けない" hub-work start nope --repo app
tn "lead からは start できない" as_lead x hub-work start nope
t "拒否では文書を作らない" test ! -e "$E/hub-env/work/nope"

echo "# hub-work start: lead のタブ"
t "作業を始める" hub-work start plain --todo 30 --purpose 目的A --request 依頼A-固有
t "status.md ができる" test -f "$W/plain/status.md"
t "作業ディレクトリは独立した git" test -d "$W/.git"
t "hub-env の git から外れている" git -C "$E/hub-env" check-ignore -q work/
t "front matter に state がある" grep -qx 'state: active' "$W/plain/status.md"
t "本文に目的がある" grep -q '目的A' "$W/plain/status.md"
t "司令塔のワークスペースに作業名のタブができる" has_tab hub-env plain
t "作業のワークスペースは作らない" no_ws w-plain
t "lead が起動する" has_agent w-plain
t "lead に依頼が届く" wait_for screen_has w-plain 依頼A-固有
t "lead に HUB_WORK と HUB_ROLE を渡す" bash -c "grep -qx HUB_ROLE=lead '$T/argv/hub-env.env' && grep -qx HUB_WORK=plain '$T/argv/hub-env.env'"
A="$T/argv/hub-env.argv"
t "lead に作業の場所を許可する" grep -qxF "Edit(/$W/plain/**)" "$A"
LP="$(grep -A1 -- '--append-system-prompt-file' "$A" | tail -1)"
t "lead に役割のファイルを渡す" grep -q '# lead の役割' "$LP"
t "lead の役割に rules も入る" grep -q '環境ルール-固有文字列' "$LP"
LS="$(grep -A1 -- '--settings' "$A" | tail -1)"
t "lead の設定は agent-settings のフックを保つ" jq -e '.hooks.PreToolUse[0].matcher == "Bash"' "$LS"
t "lead の書き込みを作業の場所に限る" jq -e --arg d "--allow $W/plain" '.hooks.PreToolUse[1].hooks[0].command | endswith($d)' "$LS"
tn "同名の作業を拒否する" hub-work start plain
t "list に出る" bash -c 'hub-work list | grep -q "^plain"'

echo "# hub-lead assign: crew のタブと worktree"
tn "commander からは assign できない" hub-lead assign app --request x
tn "crew からは assign できない" as_crew plain hub-lead assign app --request x
tn "存在しないリポジトリは拒否する" as_lead plain hub-lead assign missing --request x
t "app に crew を振る" as_lead plain hub-lead assign app --request 振る指示-固有
t "worktree ができる" test -d "$E/.hub-worktrees/plain/app"
t "作業ブランチができる" git -C "$E/app" rev-parse --verify -q refs/heads/work/plain
t "子のワークスペースに作業名のタブができる" has_tab app plain
t "crew が起動する" has_agent w-plain-app
t "crew に指示が届く" wait_for screen_has w-plain-app 振る指示-固有
t "crew に HUB_ROLE=crew を渡す" grep -qx HUB_ROLE=crew "$T/argv/app.env"
CA="$T/argv/app.argv"
t "crew に役割のファイルを渡す" grep -q '# crew の役割' "$(grep -A1 -- '--append-system-prompt-file' "$CA" | tail -1)"
CS="$(grep -A1 -- '--settings' "$CA" | tail -1)"
t "crew の書き込みを worktree と作業の場所に限る" jq -e --arg d "--allow $E/.hub-worktrees/plain/app --allow $W/plain" '.hooks.PreToolUse[1].hooks[0].command | endswith($d)' "$CS"
tn "work 全体は許可しない" grep -qxF "Edit(/$W/**)" "$CA"
t "status.md に crew が記録される" grep -q '"role": "crew"' "$W/plain/status.md"
t "同じリポジトリへの assign は今の crew に送る" as_lead plain hub-lead assign app --request 追加の指示-固有
t "追加の指示が届く" wait_for screen_has w-plain-app 追加の指示-固有
t "crew は 1 人のまま" test "$(grep -o '"role": "crew"' "$W/plain/status.md" | wc -l)" -eq 1

echo "# 役割ごとのコマンド"
t "crew は lead に通知する" as_crew plain hub-crew notify "crew の報告-固有"
t "lead に届く" wait_for screen_has w-plain "crew の報告-固有"
tn "lead は hub-crew を使えない" as_lead plain hub-crew notify x
t "lead は自分の crew に送れる" as_lead plain hub-lead send w-plain-app "lead の指示-固有"
t "lead は常駐エージェントに送れる" as_lead plain hub-lead send todo "lead から todo へ"
tn "lead は commander に send できない" as_lead plain hub-lead send "$CMDR" x
t "lead は crew の画面を読める" bash -c "HUB_ROLE=lead HUB_WORK=plain hub-lead read w-plain-app | grep -q 'lead の指示-固有'"
t "lead は自分の作業の一覧を見られる" bash -c "HUB_ROLE=lead HUB_WORK=plain hub-lead list | grep -q '^w-plain-app'"
tn "crew は hub-agent send を使えない" as_crew plain hub-agent send todo x
t "crew も hub-agent list は使える" as_crew plain hub-agent list
tn "lead は herdr-reset を使えない" as_lead plain herdr-reset app
t "lead の herdr-reset では何も閉じない" has_agent w-plain-app

echo "# hub-work: 作業ディレクトリ"
t "start は runbook.md を作らない" test ! -e "$W/plain/runbook.md"
mkdir -p "$W/plain/data"; echo secret >"$W/plain/data/x.csv"; echo memo >"$W/plain/runbook.md"
t "data/ は git から外れる" git -C "$W" check-ignore -q plain/data/x.csv
tn "runbook.md は外れない" git -C "$W" check-ignore -q plain/runbook.md
tn "status.md は外れない" git -C "$W" check-ignore -q plain/status.md
t "archive 内の data/ も外れる" git -C "$W" check-ignore -q archive/plain/data/x.csv
tn "作業名 data の文書は外れない" git -C "$W" check-ignore -q archive/data/status.md

echo "# hub-status / hub-check"
hub-status >"$T/status" 2>&1
t "status: エージェント" grep -q '^w-plain' "$T/status"
t "status: 作業" grep -q '^plain' "$T/status"
t "status: 近い TODO を出す" grep -q '#30' "$T/status"
tn "status: 遠い TODO は出さない" grep -q '#31' "$T/status"
echo dirty >"$E/app/dirty.txt"
t "status: 未コミットを出す" bash -c 'hub-status | grep -q "^app"'
rm "$E/app/dirty.txt"
t "check: ずれなし" hub-check
sed -i '/`app` | 稼働/d' "$E/hub-env/MAP.md"
tn "check: MAP の抜けを検出する" hub-check
printf '| `app` | 稼働 | アプリ | - |\n' >"$T/row"
sed -i "/\`todo\` | 稼働/r $T/row" "$E/hub-env/MAP.md"
t "check: 直すと通る" hub-check

echo "# herdr-reset: 部分リセットは作業のタブを残す"
t "app を作り直せる" herdr-reset app
t "crew のタブが残る" has_tab app plain
t "crew が残る" has_agent w-plain-app
t "hub.toml のタブは後ろに作り直す" test "$(tab_titles app)" = "plain,app"
t "hub-env を作り直せる" herdr-reset hub-env
t "lead のタブが残る" has_tab hub-env plain
t "lead が残る" has_agent w-plain
t "司令塔は作り直される" wait_for has_agent "$CMDR"

echo "# hub-lead finish"
echo x >"$E/.hub-worktrees/plain/app/dirty"
tn "未コミットの変更があれば止める" as_lead plain hub-lead finish
t "止めたとき worktree は残る" test -f "$E/.hub-worktrees/plain/app/dirty"
t "止めたとき crew のタブは残る" has_tab app plain
t "止めたとき archive へ移さない" test ! -e "$W/archive/plain"
rm "$E/.hub-worktrees/plain/app/dirty"
prep git -C "$E/.hub-worktrees/plain/app" commit -q --allow-empty -m 未マージの変更
LT="$(tab_id hub-env plain)"
HERDR_TAB_ID="$LT" HUB_ROLE=lead HUB_WORK=plain hub-lead finish >"$T/finish-plain" 2>&1; rc=$?
t "lead が自分の作業を閉じられる" test "$rc" -eq 0
t "crew のタブが閉じる" no_tab app plain
t "crew がいなくなる" wait_for no_agent w-plain-app
t "worktree が消える" test ! -e "$E/.hub-worktrees/plain"
t "マージされていないブランチは残る" git -C "$E/app" rev-parse --verify -q refs/heads/work/plain
t "残した理由を表示する" grep -q 'work/plain は main にマージされていないので残しています' "$T/finish-plain"
t "archive へ移る" test -f "$W/archive/plain/status.md"
t "archive の state は done" grep -qx 'state: done' "$W/archive/plain/status.md"
t "runbook.md も archive へ移る" test -f "$W/archive/plain/runbook.md"
t "data/ も archive へ移る" test -f "$W/archive/plain/data/x.csv"
t "runbook.md はコミットされる" git -C "$W" ls-files --error-unmatch archive/plain/runbook.md
tn "data/ はコミットされない" git -C "$W" ls-files --error-unmatch archive/plain/data/x.csv
t "最後に lead のタブが裏で閉じる" wait_for no_tab hub-env plain
t "司令塔は残る" has_agent "$CMDR"
tn "archive 内と同名を拒否する" hub-work start plain

echo "# hub-work resume（全体リセットの後）"
t "作業を始める" hub-work start again
t "crew を振る" as_lead again hub-lead assign app --request x
t "全体リセットできる" herdr-reset
t "全体リセットで作業のタブも閉じる" no_tab hub-env again
t "リセットしても checkout は残る" test -d "$E/.hub-worktrees/again/app"
t "司令塔が立て直す" hub-work resume again
t "lead のタブが戻る" has_tab hub-env again
t "crew のタブが戻る" has_tab app again
t "lead が戻る" has_agent w-again
t "crew が戻る" has_agent w-again-app
t "resume は冪等" hub-work resume again
t "タブを二重に作らない" test "$(tab_titles app | tr , '\n' | grep -cx again)" -eq 1
prep herdr tab close "$(tab_id app again)"
wait_for no_agent w-again-app
t "lead は欠けた crew を立て直せる" as_lead again hub-lead resume
t "crew が戻る（lead から）" has_agent w-again-app
prep git -C "$E/.hub-worktrees/again/app" commit -q --allow-empty -m マージする変更
prep git -C "$E/app" merge -q --no-ff -m merge work/again
hub-work finish again >"$T/finish-again" 2>&1; rc=$?
t "司令塔からも finish できる（予備）" test "$rc" -eq 0
tn "main にマージ済みのブランチは消える" git -C "$E/app" rev-parse --verify -q refs/heads/work/again
t "消したことを表示する" grep -q 'work/again を消しました' "$T/finish-again"
t "lead のタブも閉じる" no_tab hub-env again
t "crew のタブも閉じる" no_tab app again
t "作業文書がコミットされる" bash -c "git -C '$W' log --oneline | grep -q 'finish again'"
}

group_ttree() {
make_env
prep herdr-reset
echo "# herdr-reset: 単体（setup あり）"
old="$(herdr workspace list | jq -r '.result.workspaces[] | select(.label == "todo") | .workspace_id')"
t "todo だけ作り直せる" herdr-reset todo
new="$(herdr workspace list | jq -r '.result.workspaces[] | select(.label == "todo") | .workspace_id')"
t "ワークスペースが新しくなる" test "$old" != "$new"
t "todo は 1 つのまま" count_ws todo 1
t "todo エージェントが戻る" has_agent todo
t "他のワークスペースは残る" has_ws app
echo "# herdr-reset: setup のワークスペースに作業のタブがある"
t "作業を始める" hub-work start ttree
t "todo に crew を振る" as_lead ttree hub-lead assign todo --request x
t "todo に crew のタブができる" has_tab todo ttree
herdr-reset todo >"$T/reset" 2>&1; rc=$?
t "setup の単体リセットは作業を巻き込まず止まる" test "$rc" -ne 0
t "止まる理由が作業のタブ" grep -q '作業のタブ（ttree）があります' "$T/reset"
t "止まった後も crew は残る" has_agent w-ttree-todo
t "ttree を片付ける" hub-work finish ttree
t "片付けた後は作り直せる" herdr-reset todo
}

group_parent() {
make_env
prep herdr-reset
echo "# hub-lead assign: hub.toml にワークスペースの無いリポジトリ"
mkdir "$E/solo" && git -C "$E/solo" init -q -b main && git -C "$E/solo" commit -q --allow-empty -m init
t "作業を始める" hub-work start psolo
t "solo に crew を振る" as_lead psolo hub-lead assign solo --request x
t "ワークスペース solo を作る" has_ws solo
t "作ったことを記録する" grep -q '"created_workspace": true' "$W/psolo/status.md"
prep hub-work start tsolo
t "同じ solo に別の作業も振れる" as_lead tsolo hub-lead assign solo --request y
t "solo は 1 つのまま" count_ws solo 1
hub-check >"$T/check" 2>&1
tn "check: 作業が作ったワークスペースはずれに数えない" grep -q 'ワークスペース solo' "$T/check"
t "先の作業を閉じる" hub-work finish psolo
t "他の作業のタブがあれば solo は残す" has_ws solo
t "後の作業を閉じる" hub-work finish tsolo
t "作業のタブが無くなれば solo を閉じる" no_ws solo
rm -rf "$E/solo"
}

group_blocked() {
make_env
prep herdr-reset
echo "# hub-work: 依頼を届けられないとき"
mkdir "$T/failbin"
cat >"$T/failbin/herdr" <<EOF
#!/bin/bash
if [[ "\$1 \$2" == "agent prompt" ]]; then echo '{"error":{"code":"agent_blocked","message":"blocked"}}' >&2; exit 1; fi
exec "$(command -v herdr)" "\$@"
EOF
chmod +x "$T/failbin/herdr"
PATH="$T/failbin:$PATH" hub-work start blocked --request 消えてはいけない依頼 >"$T/blocked" 2>&1; rc=$?
t "届かなければ失敗を返す" test "$rc" -ne 0
t "再送のコマンドを示す" grep -q 'herdr agent prompt w-blocked' "$T/blocked"
t "依頼文を作業の場所に残す" grep -q '消えてはいけない依頼' "$W/blocked/undelivered-w-blocked.md"
t "依頼文が status.md に残る" grep -q '消えてはいけない依頼' "$W/blocked/status.md"
t "エージェントは残る" has_agent w-blocked
t "blocked を片付ける" hub-work finish blocked
}

group_trust() {
make_env
prep herdr-reset
echo "# hub-work: フォルダ信頼"
CJ="$T/claude/.claude.json"
RE="$(cd "$E" && pwd -P)"
trusted() { jq -e --arg p "$1" '.projects[$p].hasTrustDialogAccepted == true' "$CJ" >/dev/null; }
jq --arg p "$RE/app" '.projects[$p] = {hasTrustDialogAccepted: true} | .keep = 1' "$CJ" >"$CJ.new" && mv "$CJ.new" "$CJ"
prep hub-work start trusta
t "信頼済みのリポジトリに crew を振る" as_lead trusta hub-lead assign app --request x
t "worktree が信頼済みになる" trusted "$RE/.hub-worktrees/trusta/app"
t "他の設定を保つ" jq -e '.keep == 1' "$CJ"
A="$T/argv/app.argv"
tn "git と gh の自動許可を持たない" grep -qE '^Bash\((git|gh) ' "$A"
t "trusta を片付ける" hub-work finish trusta
tn "finish で信頼の登録を外す" trusted "$RE/.hub-worktrees/trusta/app"
mkdir "$E/plainrepo" && git -C "$E/plainrepo" init -q -b main && git -C "$E/plainrepo" commit -q --allow-empty -m init
prep hub-work start trustb
as_lead trustb hub-lead assign plainrepo --request x >"$T/trustb" 2>&1
t "未信頼のリポジトリは警告する" grep -q '信頼済みではありません' "$T/trustb"
tn "未信頼のリポジトリの worktree は登録しない" trusted "$RE/.hub-worktrees/trustb/plainrepo"
t "trustb を片付ける" hub-work finish trustb
prep hub-work start trustc
mkdir "$CJ.lock"
as_lead trustc hub-lead assign app --request x >"$T/trustc" 2>&1
rmdir "$CJ.lock"
t "登録できないときは警告する" grep -q '信頼済みにできませんでした' "$T/trustc"
tn "登録できないときは書かない" trusted "$RE/.hub-worktrees/trustc/app"
t "trustc を片付ける" hub-work finish trustc
rm -rf "$E/plainrepo"
}

group_outer() {
make_env
prep herdr-reset
echo "# hub-work: hub-env と環境の外のリポジトリ"
mkdir -p "$T/outside/hub" && git -C "$T/outside/hub" init -q -b main && git -C "$T/outside/hub" commit -q --allow-empty -m init
git -C "$E/hub-env" add -A >/dev/null && git -C "$E/hub-env" commit -q -m init
prep hub-work start henv
t "hub-env に crew を振れる" as_lead henv hub-lead assign hub-env --request 地図の変更
t "hub-env の worktree ができる" test -f "$E/.hub-worktrees/henv/hub-env/.git"
t "lead と同じワークスペースでは crew のタブ名を分ける" has_tab hub-env henv/hub-env
t "crew の役割を環境変数で渡す" grep -qx HUB_ROLE=crew "$T/argv/hub-env.env"
t "henv を片付ける" hub-work finish henv
t "lead のタブを閉じる" no_tab hub-env henv
t "crew のタブを閉じる" no_tab hub-env henv/hub-env
prep hub-work start outer
t "環境の外の hub に crew を振れる" as_lead outer hub-lead assign ../outside/hub --request x
t "環境の外の worktree ができる" test -f "$E/.hub-worktrees/outer/hub/.git"
tn "存在しない外のパスは拒否する" as_lead outer hub-lead assign ../outside/none --request x
t "outer を片付ける" hub-work finish outer
}

group_legacy() {
make_env
prep herdr-reset
echo "# hub-work: 既存の work リポジトリ"
t "作業を始める" hub-work start legacy
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
printf '\n%s\nlabel = "envcheck"\ncwd = "app"\nsetup = "setup-check"\n' "$WSH" >>"$E/hub-env/hub.toml"
t "setup を実行できる" herdr-reset envcheck
t "setup に rules の引数が渡る" grep -q -- '--append-system-prompt-file' "$T/setup-args"
envcheck_id="$(herdr workspace list | jq -r '.result.workspaces[] | select(.label == "envcheck") | .workspace_id')"
herdr workspace close "$envcheck_id" >/dev/null
cp "$T/toml.bak" "$E/hub-env/hub.toml"
}

group_first_setup() {
make_env
prep herdr-reset
echo "# herdr-reset: 先頭が setup"
swap_first
t "先頭が setup でも全体リセットできる" herdr-reset
t "todo が 1 つだけある" count_ws todo 1
t "hub-env がある" has_ws hub-env
t "仮のワークスペースが残らない" no_ws herdr-reset-tmp
handoff_tools
echo "# herdr-reset --handoff"
: >"$HI"
t "特殊文字の引き継ぎで全体を立て直せる" herdr-reset --handoff "$SPECIAL"
t "引き継ぎ文が壊れずに届く" wait_for input_has "$SPECIAL"
t "前置きが添えられる" input_has '前のセッションの許可は引き継がれていません'
t "確認の前置きが添えられる" input_has '改めてユーザーに確認してください'
t "配送に成功したら handoff.md を消す" test ! -e "$HF"
t "全エージェントを立て直している" has_agent todo

: >"$HI"
t "- で始まる文を値として扱う" herdr-reset hub-env --handoff -h
t "-h が届く" wait_for input_has $'--- 引き継ぎ文 ---\n-h'
t "--handoff=値 の形も使える" bash -c ': >"$1"; herdr-reset hub-env "--handoff=等号の値-固有文字列"' _ "$HI"
t "等号の値が届く" wait_for input_has '等号の値-固有文字列'

before="$(tab_ids hub-env)"
tn "空文字はエラー" herdr-reset hub-env --handoff ""
tn "空白だけはエラー" herdr-reset hub-env --handoff "   "
tn "値の欠落はエラー" herdr-reset hub-env --handoff
tn "二重指定はエラー" herdr-reset hub-env --handoff a --handoff b
t "引数エラーでは何も閉じない" test "$before" = "$(tab_ids hub-env)"
t "引数エラーでは保存しない" test ! -e "$HF"

before="$(tab_ids app)"; hub_before="$(tab_ids hub-env)"
herdr-reset app --handoff 対象外 >"$T/hout" 2>&1; rc=$?
t "hub が対象外ならエラー" test "$rc" -ne 0
t "エラーが hub を示す" grep -q "エージェント $CMDR" "$T/hout"
t "hub が対象外なら対象を閉じない" test "$before" = "$(tab_ids app)"
t "hub が対象外なら hub も閉じない" test "$hub_before" = "$(tab_ids hub-env)"
t "hub が対象外なら保存しない" test ! -e "$HF"
}

group_handoff_fail() {
make_env
prep swap_first
prep herdr-reset
handoff_tools
echo "# herdr-reset --handoff（配送の失敗）"
hstub fail hub-env --handoff '配送失敗-固有文字列' >"$T/hout" 2>&1; rc=$?
t "配送に失敗したら失敗を返す" test "$rc" -ne 0
t "配送に失敗したら残す" test -f "$HF"
t "handoff.md の権限は 600" test "$(stat -c %a "$HF")" = 600
t "handoff.md に引き継ぎ文がある" grep -q '配送失敗-固有文字列' "$HF"
t "失敗のログに場所を出す" grep -qF "$HF" "$T/hout"
tn "失敗のログに引き継ぎ文を出さない" grep -q '配送失敗-固有文字列' "$T/hout"
cp "$HF" "$T/handoff.keep"; before="$(tab_ids hub-env)"
herdr-reset hub-env --handoff 別の文 >"$T/hout" 2>&1; rc=$?
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
}

group_handoff_blocked() {
make_env
prep swap_first
prep herdr-reset
handoff_tools
echo "# herdr-reset --handoff（blocked）"
: >"$HI"
t "blocked の間は待って送る" hstub blocked-once hub-env --handoff 'blocked解除-固有文字列'
t "blocked の解除後に届く" wait_for input_has 'blocked解除-固有文字列'
t "blocked のあと消す" test ! -e "$HF"
hstub blocked hub-env --handoff 'blocked継続-固有文字列' >"$T/hout" 2>&1; rc=$?
t "blocked が続けば失敗を返す" test "$rc" -ne 0
t "blocked が続けば残す" test -f "$HF"
rm -f "$HF"
}

group_handoff_bg() {
make_env
prep swap_first
prep herdr-reset
handoff_tools
echo "# herdr-reset --handoff（立て直しの失敗と裏の実行）"
cp "$E/hub-env/hub.toml" "$T/toml.handoff"
printf '\n%s\nlabel = "broken"\ncwd = "app"\nsetup = "broken-setup"\n' "$WSH" >>"$E/hub-env/hub.toml"
printf '#!/bin/bash\nexit 1\n' >"$E/broken-setup"; chmod +x "$E/broken-setup"
herdr-reset --handoff '立て直し失敗-固有文字列' >"$T/hout" 2>&1; rc=$?
t "立て直しに失敗したら失敗を返す" test "$rc" -ne 0
t "立て直しに失敗したら残す" test -f "$HF"
t "立て直しの失敗でも場所を出す" grep -qF "$HF" "$T/hout"
rm -f "$HF" "$E/broken-setup"; cp "$T/toml.handoff" "$E/hub-env/hub.toml"
t "失敗のあと元の構成へ戻せる" herdr-reset

: >"$HI"; : >"$T/handoff-env"; : >"$T/handoff-prompts"
own="$(ws_id hub-env)"
PATH="$T/handoffbin:$PATH" HERDR_WORKSPACE_ID="$own" herdr-reset --handoff $'裏実行-固有文字列\n二行目' >"$T/hout" 2>&1; rc=$?
t "自分のタブを閉じるときは裏で続ける" test "$rc" -eq 0
t "裏で続けることを知らせる" grep -q '裏で立て直します' "$T/hout"
t "裏の実行でも引き継ぎが届く" wait_long input_has $'裏実行-固有文字列\n二行目'
t "裏の実行でも配送後に消す" wait_long test ! -e "$HF"
t "裏の実行のログに成功が出る" wait_long grep -q "引き継ぎを $CMDR へ届けました" "$T/state/herdr-reset.log"
tn "裏の実行のログに引き継ぎ文を出さない" grep -q '裏実行-固有文字列' "$T/state/herdr-reset.log"
tn "環境変数に引き継ぎ文を出さない" grep -q '裏実行-固有文字列' "$T/handoff-env"
t "裏の実行は 1 回だけ送る" prompts_are 1

: >"$HI"
before_files="$(ls -A "$(dirname "$HF")")"
t "--handoff なしでも単体で立て直せる" herdr-reset hub-env
t "--handoff なしでも全体を立て直せる" herdr-reset
perl -e 'select(undef,undef,undef,0.5)'
tn "--handoff なしでは何も送らない" test -s "$HI"
t "--handoff なしでは handoff.md を作らない" test ! -e "$HF"
t "--handoff なしでは state のファイルが増えない" test "$before_files" = "$(ls -A "$(dirname "$HF")")"
t "--help に使い方がある" bash -c 'herdr-reset --help | grep -q -- --handoff'
}

group_resume() {
make_env
prep herdr-reset
prep swap_first
handoff_tools
echo "# herdr-reset --resume"
SF="$(dirname "$HF")/$CMDR-session.json"
AH="$T/argv/hub-env.argv"; AA="$T/argv/todo.argv"
sid_now() { jq -r .session_id "$SF"; }
resumes() { grep -A1 -x -- '--resume' "$1" | tail -1 | grep -qxF "$2"; }
no_resume() { ! grep -qx -- '--resume' "$1"; }
all_ws() { herdr workspace list | jq -c '[.result.workspaces[].workspace_id]'; }
t "（前提）全体を立て直す" herdr-reset
t "hub のセッション ID が記録される" test -f "$SF"
t "記録の権限は 600" test "$(stat -c %a "$SF")" = 600
sid="$(sid_now)"
: >"$HI"; rm -f "$AA"
t "--resume で全体を立て直せる" herdr-reset --resume
t "他のエージェントも起動し直される" wait_for test -f "$AA"
t "hub は記録された ID で戻る" resumes "$AH" "$sid"
t "他のエージェントは新規に起動する" no_resume "$AA"
t "resume 後も ID が変わらない" test "$(sid_now)" = "$sid"
perl -e 'select(undef,undef,undef,0.5)'
tn "resume では前置きを送らない" test -s "$HI"
t "--resume で hub だけのワークスペースを立て直せる" herdr-reset hub-env --resume
t "単体でも記録された ID で戻る" resumes "$AH" "$sid"
rm -f "$AH"; own="$(ws_id hub-env)"
HERDR_WORKSPACE_ID="$own" herdr-reset --resume >"$T/rout" 2>&1; rc=$?
t "自分のタブを閉じるときは裏で続ける" test "$rc" -eq 0
t "裏の実行でも同じ ID で戻る" wait_long bash -c "grep -A1 -x -- --resume '$AH' 2>/dev/null | tail -1 | grep -qxF '$sid'"
wait_long has_agent "$CMDR"; wait_long has_ws app; wait_long has_agent todo
wait_long bg_reset_done

before="$(all_ws)"; tabs="$(tab_ids hub-env)"
tn "--handoff との同時指定はエラー" herdr-reset --handoff 文 --resume
tn "順序が逆でもエラー" herdr-reset --resume --handoff 文
tn "hub が対象外ならエラー" herdr-reset app --resume
cp "$SF" "$T/session.bak"; rm "$SF"
herdr-reset --resume >"$T/rout" 2>&1; rc=$?
t "ID が無ければ止まる" test "$rc" -ne 0
t "ID が無いことを知らせる" grep -q '記録されていません' "$T/rout"
echo '{broken' >"$SF"
tn "記録が壊れていれば止まる" herdr-reset --resume
cp "$T/session.bak" "$SF"
TR="$(jq -r .transcript_path "$SF")"; mv "$TR" "$T/transcript.bak"
herdr-reset --resume >"$T/rout" 2>&1; rc=$?
t "履歴が無ければ止まる" test "$rc" -ne 0
t "履歴が無いことを知らせる" grep -q '履歴が見つかりません' "$T/rout"
mv "$T/transcript.bak" "$TR"
t "止まったときワークスペースを閉じない" test "$before" = "$(all_ws)"
t "止まったときタブを閉じない" test "$tabs" = "$(tab_ids hub-env)"
t "--resume なしは新規に起動する" herdr-reset
t "--resume なしでは再開の引数を渡さない" no_resume "$AH"
t "--resume なしでは新しい ID が記録される" test "$(sid_now)" != "$sid"
t "--help に --resume がある" bash -c 'herdr-reset --help | grep -q -- --resume'
}

group_wtshape() {
make_env
prep herdr-reset
echo "# hub-work: ワークスペースとタブの形"
mkdir "$E/solo" && git -C "$E/solo" init -q -b main && git -C "$E/solo" commit -q --allow-empty -m init
# ワークスペースの並びと、それぞれのタブを出す
shape() {
  herdr workspace list | jq -r '.result.workspaces[] | [.workspace_id, .label] | @tsv' |
    while IFS=$'\t' read -r id label; do
      printf '%s\t%s\n' "$label" "$(herdr tab list --workspace "$id" | jq -r '[.result.tabs[].label] | join(",")')"
    done
}
same_shape() { diff <(shape) - >"$T/shape.diff" || { cat "$T/shape.diff"; return 1; }; }
t "作業を始める" hub-work start wa
t "app に振る" as_lead wa hub-lead assign app --request x
t "solo に振る" as_lead wa hub-lead assign solo --request y
t "相談だけの作業を始める" hub-work start wb
t "start と assign 後の形" same_shape <<EOF
hub-env	Claude,wa,wb
todo	1
app	app,wa
solo	wa
EOF
prep herdr-reset
t "全体リセット後の形" same_shape <<EOF
hub-env	Claude
todo	1
app	app
EOF
t "wa を立て直す" hub-work resume wa
t "wb を立て直す" hub-work resume wb
t "resume 後の形" same_shape <<EOF
hub-env	Claude,wa,wb
todo	1
app	app,wa
solo	wa
EOF
t "wa を finish できる" hub-work finish wa
t "wb を finish できる" hub-work finish wb
t "finish 後の形" same_shape <<EOF
hub-env	Claude
todo	1
app	app
EOF
t "checkout が消える" test ! -e "$E/.hub-worktrees/wa"
t "worktree の登録が残らない" bash -c "! git -C '$E/app' worktree list | grep -q hub-worktrees && ! git -C '$E/solo' worktree list | grep -q hub-worktrees"
}

group_notify() {
make_env
prep herdr-reset
handoff_tools
echo "# hub-agent: 通知と送受信（司令塔は $CMDR）"
nstub() { local mode="$1"; shift; : >"$T/handoff-prompts"; PATH="$T/handoffbin:$PATH" HANDOFF_STUB="$mode" HUB_AGENT_NOTIFY_WAIT=2 HUB_AGENT_NOTIFY_INTERVAL=0.2 hub-agent notify "$@"; }
: >"$HI"
t "司令塔へ通知できる" nstub none "通知-固有1"
t "司令塔に届く" wait_for input_has "通知-固有1"
t "確認待ちなら待って送り直す" nstub blocked-once "通知-固有2"
t "送り直した通知が届く" wait_for input_has "通知-固有2"
tn "確認待ちが続けば失敗を返す" nstub blocked "通知-固有3"
t "timeout でも画面にあれば成功" nstub sent-timeout "通知-固有4"
t "timeout で届いていれば送り直さない" prompts_are 1
t "二重に届かない" input_count "通知-固有4" 1
tn "届かない宛先なら失敗を返す" nstub fail "通知-固有5"
t "届かない宛先には送り直さない" prompts_are 1
t "送れる" hub-agent send todo "送信-固有"
t "一覧に出る" bash -c "hub-agent list | grep -q '^todo'"
t "画面を読める" bash -c "hub-agent read $CMDR --lines 40 | grep -q 通知-固有1"
hub-agent launch "$CMDR" --print >"$T/launch" 2>&1
t "起動コマンドに agent の args がある" grep -q -- 'test-model' "$T/launch"
t "司令塔の起動コマンドに記録の設定がある" grep -q -- "$CMDR-agent-settings.json" "$T/launch"
t "起動コマンドに rules がある" grep -q -- '--append-system-prompt-file' "$T/launch"
tn "知らないエージェントは起動しない" hub-agent launch nobody --print
t "司令塔に役割のファイルを渡す" grep -q '# 司令塔（commander）の役割' "$(grep -A1 -- '--append-system-prompt-file' "$T/argv/hub-env.argv" | tail -1)"
tn "lead は hub-agent notify を使えない" as_lead x hub-agent notify x
}

group_none() {
make_tmp
cat >"$T/bin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" >"$HUB_TEST_ARGV/$(basename "$PWD").argv"
env | grep -E '^HUB_(WORK|ROLE)=' | sort >"$HUB_TEST_ARGV/$(basename "$PWD").env"
settings=""; a=("$@")
for i in "${!a[@]}"; do [[ "${a[$i]}" == --settings ]] && settings="${a[$((i + 1))]}"; done
if [[ -n "$settings" ]]; then
  sid="$(cat /proc/sys/kernel/random/uuid)"; tr="$HUB_TEST_ARGV/$sid.jsonl"; : >"$tr"
  jq -r '.hooks.SessionStart[]?.hooks[]?.command' "$settings" | while IFS= read -r c; do
    jq -n --arg s "$sid" --arg t "$tr" --arg c "$PWD" '{hook_event_name:"SessionStart",session_id:$s,transcript_path:$t,cwd:$c,source:"startup"}' | bash -c "$c"
  done
fi
EOF
chmod +x "$T/bin/claude"
E="$T/env"; W="$E/hub-env/work"
mkdir -p "$E/hub-env" "$E/app"
git -C "$E/hub-env" init -q -b main && printf '/work/\n' >"$E/hub-env/.gitignore"
git -C "$E/app" init -q -b main && git -C "$E/app" commit -q --allow-empty -m init
cat >"$E/hub-env/hub.toml" <<'EOF'
backend = "none"

[[agent]]
name = "commander"
cwd = "hub-env"
EOF
cat >"$E/hub-env/MAP.md" <<'EOF'
# 地図

## リポジトリ

| リポジトリ | 状態 | 関心事 | 入口のドキュメント |
|---|---|---|---|
| `hub-env` | 稼働 | 司令塔 | - |
| `app` | 稼働 | アプリ | - |

## エージェント

| 名前 | cwd | 起動 | できること |
|---|---|---|---|
| `commander` | hub-env | hub-agent launch commander | 司令塔 |
EOF
echo "$E" >"$T/config/hub/envs"
cd "$E/hub-env"
echo "# backend なし（herdr を使わない）"
hub-work start nb --request 'backendなし依頼-固有' >"$T/nb" 2>&1; rc=$?
t "作業を始められる" test "$rc" -eq 0
t "status.md ができる" test -f "$W/nb/status.md"
t "起動していないことを知らせる" grep -q 'backend が無いので' "$T/nb"
t "依頼文を brief に残す" grep -q 'backendなし依頼-固有' "$W/nb/brief-w-nb.md"
HE="$(cd "$E/hub-env" && pwd)"
t "起動コマンドは hub-env で lead を起動する" grep -qF "cd $HE && env HUB_WORK=nb HUB_ROLE=lead claude" "$T/nb"
line="$(grep -F '&& env HUB_WORK' "$T/nb" | head -1)"
(cd / && bash -c "$line" </dev/null)
A="$T/argv/hub-env.argv"
t "示したコマンドで起動できる" test -f "$A"
t "lead に役割が渡る" grep -qx HUB_ROLE=lead "$T/argv/hub-env.env"
t "作業の場所の許可を渡す" grep -qxF "Edit(/$W/nb/**)" "$A"
t "依頼文を最初のプロンプトとして渡す" grep -q 'backendなし依頼-固有' "$A"
as_lead nb hub-lead assign app --request 'crew依頼-固有' >"$T/nba" 2>&1; rc=$?
t "crew を振れる" test "$rc" -eq 0
t "worktree ができる" test -d "$E/.hub-worktrees/nb/app"
t "起動コマンドは worktree で crew を起動する" grep -qF "cd $E/.hub-worktrees/nb/app && env HUB_WORK=nb HUB_ROLE=crew claude" "$T/nba"
as_crew nb hub-crew notify x >/dev/null 2>&1; rc=$?
t "crew の notify は backend が無いことを返す" test "$rc" -eq 3
t "list の状態は不明" bash -c "hub-work list | grep -q 'w-nb=不明'"
t "status は確認できないことを示す" bash -c "hub-status | grep -q 'backend が無いので確認できません'"
hub-check >"$T/check" 2>&1; rc=$?
t "check: ずれなし" test "$rc" -eq 0
t "check: 照合していないことを示す" grep -q '照合していません' "$T/check"
hub-agent notify 'x' >/dev/null 2>&1; rc=$?
t "notify は backend が無いことを返す" test "$rc" -eq 3
hub-agent launch commander --print >"$T/launch" 2>&1
t "司令塔の起動コマンドに記録の設定がある" grep -q 'commander-agent-settings.json' "$T/launch"
(cd "$E/app" && hub-agent launch commander </dev/null >/dev/null 2>&1)
t "司令塔を起動できる" test -f "$T/argv/hub-env.argv"
t "司令塔のセッションが記録される" bash -c "ls '$T'/state/hub/envs/*/commander-session.json >/dev/null"
t "lead が finish できる" as_lead nb hub-lead finish
t "worktree が消える" test ! -e "$E/.hub-worktrees/nb"
t "archive へ移る" test -f "$W/archive/nb/status.md"
cd /
}

group_install() {
make_tmp
echo "# install.sh --env: README"
I="$T/sample-env"
mkdir "$I"
install_env() { HOME="$T/install-home" XDG_CONFIG_HOME="$T/install-config" "$HUB/install.sh" --env "$I"; }
t "環境を作れる" install_env
t "README ができる" test -f "$I/hub-env/README.md"
t "README の見出しに環境名が入る" grep -qxF '# hub-env — sample-env' "$I/hub-env/README.md"
tn "README にプレースホルダーが残らない" grep -qF '{{' "$I/hub-env/README.md"
t "README に頼み方の表がある" grep -qF '## 頼み方' "$I/hub-env/README.md"
t "CLAUDE.md は地図を読み込む" grep -qx '@MAP.md' "$I/hub-env/CLAUDE.md"
tn "CLAUDE.md にプレースホルダーが残らない" grep -qF '{{' "$I/hub-env/CLAUDE.md"
t "delegate スキルは作らない" test ! -e "$I/hub-env/.claude/skills/delegate"
t "sync-guide は配置しない" test ! -e "$I/hub-env/.claude/skills/sync-guide"
echo "# install.sh: コマンドのリンク"
B="$T/install-home/.local/bin"
t "scripts のコマンドをリンクする" test -L "$B/hub-work"
t "hub-lead と hub-crew もリンクする" bash -c "test -L '$B/hub-lead' && test -L '$B/hub-crew'"
t "contrib/herdr の herdr-reset をリンクする" bash -c "[[ \"\$(readlink '$B/herdr-reset')\" == '$HUB/contrib/herdr/herdr-reset' ]]"
t "backend.py はリンクしない" test ! -e "$B/backend.py"
t "__pycache__ はリンクしない" test ! -e "$B/__pycache__"
ln -s "$HUB/scripts/hub-reset" "$B/hub-reset"; ln -s "$T/elsewhere" "$B/elsewhere"
t "もう一度入れられる" install_env
t "先の無くなった hub のリンクを外す" test ! -L "$B/hub-reset"
t "hub の外を指すリンクは残す" test -L "$B/elsewhere"
t "--uninstall で外せる" env HOME="$T/install-home" "$HUB/install.sh" --uninstall
t "--uninstall で herdr-reset も外す" test ! -e "$B/herdr-reset"
printf '手元の README\n' >"$I/hub-env/README.md"
t "既存の環境にもう一度実行できる" install_env
t "既存の README を上書きしない" grep -qxF '手元の README' "$I/hub-env/README.md"
}

# ── 実行 ───────────────────────────────────────────────────
SUITES=(base ttree parent wtshape blocked trust outer legacy first_setup handoff_fail handoff_blocked handoff_bg resume notify none install)
# HUB_TEST_GROUPS="base wtshape" のように、一部のグループだけを流せる
[[ -n "${HUB_TEST_GROUPS:-}" ]] && read -ra SUITES <<<"$HUB_TEST_GROUPS"
LOGS="$(mktemp -d /tmp/hubl.XXXXXX)"
trap 'rm -rf "$LOGS"' EXIT
for g in "${SUITES[@]}"; do
  ( SECONDS=0; "group_$g"; echo "## end $SECONDS" ) >"$LOGS/$g" 2>&1 </dev/null &
done
wait
pass=0 fail=0
for g in "${SUITES[@]}"; do
  echo "## グループ $g"
  grep -v '^## end' "$LOGS/$g"
  pass=$((pass + $(grep -c '^ok   - ' "$LOGS/$g")))
  fail=$((fail + $(grep -c '^FAIL - ' "$LOGS/$g")))
  end="$(tail -n 1 "$LOGS/$g")"
  if [[ "$end" == "## end "* ]]; then echo "（${end#\#\# end } 秒）"; else fail=$((fail + 1)); echo "FAIL - グループ $g が途中で終わりました"; fi
done

echo
echo "結果: $pass 件成功 / $fail 件失敗"
[[ "$fail" -eq 0 ]]
