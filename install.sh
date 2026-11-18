#!/usr/bin/env bash
#   ./install.sh               scripts/ のコマンドを ~/.local/bin にリンクする
#   ./install.sh --env <dir>   上に加えて、<dir> に hub-env を雛形から作り（既にあるファイルはそのまま）、~/.config/hub/envs に登録する
#   ./install.sh --uninstall   ~/.local/bin のリンクを外す
set -euo pipefail

HUB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/bin"
ENVS_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/hub/envs"

usage() { sed -n '2,4p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

mode="install"
env_dir=""
case "${1:-}" in
  "") ;;
  --uninstall) mode="uninstall" ;;
  --env) env_dir="$(cd "$2" && pwd)" ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

# ── コマンド ────────────────────────────────────────────────────
echo "== hub: コマンド"
mkdir -p "$DEST"
for f in "$HUB"/scripts/*; do
  link="$DEST/$(basename "$f")"
  if [[ "$mode" == "uninstall" ]]; then
    [[ "$(readlink "$link")" == "$f" ]] && rm "$link" && echo "removed  $link"
    continue
  fi
  if [[ -e "$link" && ! -L "$link" ]]; then
    echo "error: $link はリンクではありません" >&2
    exit 1
  fi
  ln -sfn "$f" "$link"
  echo "linked   $link -> $f"
done

[[ -n "$env_dir" ]] || exit 0

# ── 環境 ───────────────────────────────────────────────────────
echo "== hub: 環境 $env_dir"
HUB_ENV="$env_dir/hub-env"
mkdir -p "$HUB_ENV/.claude"

create() {  # create <hub-env からの相対パス> < 中身。既にあれば作らない
  if [[ -e "$HUB_ENV/$1" ]]; then
    cat >/dev/null
    echo "exists   hub-env/$1"
  else
    cat >"$HUB_ENV/$1"
    echo "created  hub-env/$1"
  fi
}
sed -e "s|{{HUB}}|$HUB|g" -e "s|{{ENV_NAME}}|$(basename "$env_dir")|g" "$HUB/templates/CLAUDE.md" | create CLAUDE.md
create MAP.md <"$HUB/templates/MAP.md"
create hub.toml <"$HUB/templates/hub.toml"
printf '{\n  "permissions": {\n    "allow": ["Read(/%s/**)", "Read(/%s/**)"]\n  }\n}\n' "$env_dir" "$HUB" \
  | create .claude/settings.local.json

mkdir -p "$(dirname "$ENVS_FILE")"
touch "$ENVS_FILE"
if grep -qxF "$env_dir" "$ENVS_FILE"; then
  echo "exists   $ENVS_FILE に登録済み"
else
  echo "$env_dir" >>"$ENVS_FILE"
  echo "added    $env_dir -> $ENVS_FILE"
fi

