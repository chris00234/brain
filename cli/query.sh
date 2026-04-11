#!/bin/zsh
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <query> [collection]"
  echo "Collections: knowledge | experience | context"
  exit 1
fi

QUERY="$1"
COLLECTION="${2:-knowledge}"
/opt/homebrew/bin/python3 echo 'DEPRECATED: search_test.py removed — use brain/cli/eval_compare.py' "$QUERY" "$COLLECTION"
