#!/usr/bin/env bash
# PostCompact hook: reinject critical context after compaction.
# Outputs additionalContext so Claude doesn't lose orientation.

set -euo pipefail

cat <<'EOF'
[Post-compaction context refresh]
- Check your task list for remaining work.
- If you were mid-task, re-read the relevant files before continuing.
- Don't re-do work that's already committed — check git log.
- CLAUDE.md rules still apply: action bias, no permission-seeking, staff-engineer bar.
EOF

exit 0
