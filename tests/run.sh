#!/usr/bin/env bash
# テストをサイズで呼び分ける。
# 使い方: tests/run.sh [small|medium]
#   small  : tests/test_hub_*.py（フック・backend・hub-work の単体テスト）
#   medium : tests/integration.sh（一時ディレクトリと専用の herdr サーバーでの結合テスト）
#   引数なしなら両方を流す。1 件でも失敗すれば終了コードは 0 以外になる。
set -uo pipefail

TESTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
failed=()

run_small() {
  local t
  for t in "$TESTS"/test_hub_*.py; do
    echo "== small: ${t##*/}"
    python3 "$t" || failed+=("small: ${t##*/}")
  done
}

run_medium() {
  echo "== medium: integration.sh"
  "$TESTS/integration.sh" || failed+=("medium: integration.sh")
}

case "${1:-all}" in
  small) run_small ;;
  medium) run_medium ;;
  all) run_small; run_medium ;;
  *) echo "使い方: tests/run.sh [small|medium]" >&2; exit 2 ;;
esac

if ((${#failed[@]})); then
  echo "失敗:" >&2
  printf '  %s\n' "${failed[@]}" >&2
  exit 1
fi
echo "すべて成功"
