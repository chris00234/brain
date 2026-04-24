#!/bin/bash
# Claude Code UserPromptSubmit hook (v3) — active recall per turn.
#
# v3 changes (2026-04-14):
#   - READS STDIN: extracts prompt, session_id, cwd from the hook JSON payload.
#     Previously the payload was discarded, so brain never saw Chris's prompt.
#   - Per-turn active recall: curls POST /recall/active on every turn with the
#     extracted prompt. Returns intent-routed canonical + semantic + proactive
#     blocks. Hard 1.2 s timeout, fail-open.
#   - First-turn-only baseline: the heavyweight boot_context.py subprocess (which
#     loads identity/state/focus/recent sessions/atoms-due/proactive/messages) only
#     runs on the first turn of a session or when the 5-min payload cache misses.
#     Subsequent turns hit the running uvicorn directly — no Python cold start.
#   - Doorbell read: if brain_loop has queued urgent context for this session,
#     it lives at /tmp/.brain_doorbell.<session_id>.jsonl; this script prints and
#     clears it so brain-initiated context reaches Claude mid-session.
#
# v2 baseline behavior preserved as the first-turn path:
#   - 5-min TTL payload cache
#   - Sentinel <system-reminder> on degraded fetches
#   - 2-second wall-clock budget on the Python subprocess
#   - CWD-based canonical notes via /recall (kept as extra context layer)

set -uo pipefail

CACHE_FILE="/tmp/.claude_boot_context.cache"
CACHE_TS="/tmp/.claude_boot_context.ts"
CACHE_TTL=300  # 5 minutes
BRAIN_PY="${BRAIN_PYTHON:-/Users/chrischo/server/brain/.venv/bin/python}"
BRAIN_URL="${BRAIN_URL:-http://127.0.0.1:8791}"
NOW=$(date +%s)

# ── Step 1: Read hook payload from stdin ─────────────────────────
# Claude Code sends a JSON envelope with session_id, transcript_path, hook_event_name,
# cwd, and prompt. Field naming is inconsistent across hooks (post_session uses
# session_id, pre_compact uses sessionId) so accept either.
PAYLOAD=""
if [ ! -t 0 ]; then
  PAYLOAD=$(cat 2>/dev/null || echo "")
fi

SESSION_ID=""
PROMPT=""
CWD_RAW=""
if [ -n "$PAYLOAD" ] && command -v jq >/dev/null 2>&1; then
  SESSION_ID=$(printf '%s' "$PAYLOAD" | jq -r '(.session_id // .sessionId // "") | tostring | .[0:128]' 2>/dev/null || echo "")
  PROMPT=$(printf '%s' "$PAYLOAD" | jq -r '(.prompt // .user_message // "") | tostring | .[0:8000]' 2>/dev/null || echo "")
  CWD_RAW=$(printf '%s' "$PAYLOAD" | jq -r '(.cwd // "") | tostring | .[0:512]' 2>/dev/null || echo "")
fi

# Fallback session id derived from the TTY so turn counting still works if hook
# payload is missing fields. Not durable across shells but prevents collisions
# within a single interactive run.
if [ -z "$SESSION_ID" ]; then
  SESSION_ID="anon-$$"
fi

# Turn counter — lives in /tmp, scoped per session id.
TURN_FILE="/tmp/.claude_turn_${SESSION_ID}"
TURN_IDX=$(cat "$TURN_FILE" 2>/dev/null || echo 0)
# Strip any whitespace/newlines from the counter (cat can return junk if file corrupted).
TURN_IDX=$(printf '%s' "$TURN_IDX" | tr -d '[:space:]')
[ -z "$TURN_IDX" ] && TURN_IDX=0
# 2026-04-18: also reject non-numeric garbage (jq --argjson expects a raw JSON
# number; anything else silently produces an empty REQ_BODY and the active-recall
# block quietly skips every turn for the rest of the session).
[[ "$TURN_IDX" =~ ^[0-9]+$ ]] || TURN_IDX=0
echo $((TURN_IDX + 1)) > "$TURN_FILE"

# 2026-04-18: pre-create the bearer-header tempfile once and share it across
# both recall blocks below. Previous code called mktemp twice with separate
# traps — the second `trap` overwrote the first, orphaning the initial
# tempfile in /tmp/ on exit. Over time, bearer-secret tempfiles accumulated.
SECRET_FILE_CB="$HOME/.openclaw/credentials/.personal_webhook_secret"
HEADER_FILE_CB=""
if [ -r "$SECRET_FILE_CB" ]; then
  HEADER_FILE_CB=$(mktemp -t claude_boot_hdr_XXXXXX)
  trap 'rm -f "$HEADER_FILE_CB" 2>/dev/null' EXIT HUP INT TERM
  { printf 'Authorization: Bearer '; cat "$SECRET_FILE_CB"; printf '\n'; } > "$HEADER_FILE_CB"
  chmod 600 "$HEADER_FILE_CB"
fi

emit_sentinel() {
  local reason="$1"
  local age_s="${2:-}"
  # Log EVERY degraded serve to a structured log — SLO job reads recent lines
  # to alert on sustained degradation. Fails open; never blocks the hook.
  local log_dir="/Users/chrischo/server/brain/logs"
  local log_file="$log_dir/degraded_serves.log"
  if [ -d "$log_dir" ]; then
    local now_iso
    now_iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    printf '%s\treason=%s\tage_s=%s\tsession=%s\tturn=%s\n' \
      "$now_iso" "$reason" "${age_s:-0}" "${SESSION_ID:-?}" "${TURN_IDX:-?}" \
      >> "$log_file" 2>/dev/null || true
  fi
  if [ -n "$age_s" ]; then
    local age_min=$(( age_s / 60 ))
    printf '<system-reminder>\n[brain DEGRADED: serving cached boot context from %d minute(s) ago — %s]\n</system-reminder>\n' \
      "$age_min" "$reason"
  else
    printf '<system-reminder>\n[brain DEGRADED: brain unreachable, operating without context (%s)]\n</system-reminder>\n' \
      "$reason"
  fi
}

# ── Step 2: Session-start baseline (first turn or cache miss only) ─────
# The expensive path: spawns boot_context.py which loads identity/state/focus/
# recent sessions/atoms-due-review/proactive alerts/pending messages. This takes
# 200-400 ms of Python import + the actual search fanout. Cached for 5 min.
BASELINE_NEEDED=0
if [ "$TURN_IDX" = "0" ]; then
  BASELINE_NEEDED=1
elif [ ! -f "$CACHE_FILE" ] || [ ! -f "$CACHE_TS" ]; then
  BASELINE_NEEDED=1
else
  LAST_CACHE=$(cat "$CACHE_TS" 2>/dev/null || echo 0)
  AGE_CACHE=$(( NOW - LAST_CACHE ))
  if [ "$AGE_CACHE" -ge "$CACHE_TTL" ]; then
    BASELINE_NEEDED=1
  fi
fi

RESULT=""
if [ "$BASELINE_NEEDED" = "1" ]; then
  # First-turn-only path: spawn boot_context.py to populate the 5-min cache.
  # Budget is 4 s (up from 2 s in v2) because cold import + search fanout runs
  # ~1.8 s on an M4 Max and per-turn active recall no longer depends on this
  # path — it only has to complete before the user's very first answer, not
  # every subsequent one.
  #
  # Pass the user's current prompt so RAG sections get reranked by intent.
  # Only enabled on turn 0 (when BASELINE_NEEDED due to fresh session); on a
  # cache-miss refresh we still pass it — it makes the new baseline match what
  # Chris is actually working on right now.
  PROMPT_ARGS=()
  if [ -n "$PROMPT" ]; then
    PROMPT_ARGS=(--prompt "$PROMPT")
  fi
  # Budget raised 4s→8s 2026-04-23 after boot_context_degraded_1h SLO
  # breached at 27 serves/hr — subagent flood + brain-under-load pushed
  # the 2-3s cold start over 4s. Measured cold-start is ~2.4s; 8s gives
  # 3x headroom without blocking agents who truly need fresh context.
  if command -v timeout >/dev/null 2>&1; then
    RESULT=$(timeout 8 "$BRAIN_PY" /Users/chrischo/server/brain/brain_core/boot_context.py claude --limit 2 "${PROMPT_ARGS[@]}" 2>/dev/null || true)
  else
    RESULT=$("$BRAIN_PY" /Users/chrischo/server/brain/brain_core/boot_context.py claude --limit 2 "${PROMPT_ARGS[@]}" 2>/dev/null &
             BG=$!
             ( sleep 8 && kill -9 $BG 2>/dev/null ) &
             KILLER=$!
             wait $BG 2>/dev/null
             kill $KILLER 2>/dev/null
             true)
  fi

  if [ -n "$RESULT" ] && [ "$RESULT" != "No relevant boot context found. Starting fresh." ]; then
    printf '%s\n' "$RESULT"
    printf '%s' "$RESULT" > "$CACHE_FILE"
    echo "$NOW" > "$CACHE_TS"
  else
    # Fresh fetch failed or empty. Try cache fallback.
    if [ -f "$CACHE_FILE" ] && [ -f "$CACHE_TS" ]; then
      LAST=$(cat "$CACHE_TS" 2>/dev/null || echo 0)
      AGE=$(( NOW - LAST ))
      if [ "$AGE" -lt "$CACHE_TTL" ]; then
        cat "$CACHE_FILE"
        emit_sentinel "brain timeout — cached payload" "$AGE"
      else
        emit_sentinel "cache expired (${AGE}s > ${CACHE_TTL}s)"
      fi
    else
      emit_sentinel "no cache available"
    fi
  fi
else
  # 2026-04-17 injection-noise fix: the full baseline (15 context blocks,
  # recent sessions, recent agent conversations, ~15KB) was being printed
  # on EVERY turn. Claude already has the baseline from turn 0 — repeating
  # it wastes context + makes UserPromptSubmit output visually noisy.
  #
  # New behavior: turn 1+ skips baseline entirely. Only per-turn active
  # recall (prompt-relevant blocks) and doorbell messages are injected.
  # Baseline only re-emits when cache expires AND a refresh actually
  # happened (BASELINE_NEEDED=1 path above), giving Claude an up-to-date
  # snapshot of focus/sessions/messages every ~5 minutes.
  :
fi

# ── Step 3: Per-turn active recall ────────────────────────────────
# Every turn, regardless of cache state, call brain with the actual user prompt.
# This is the layer 1 fix — brain finally sees what Chris is asking about.
#
# Endpoint: POST /recall/active (deployed in Phase 1.2). Until that ships, this
# block silently no-ops (curl returns 404 → jq returns empty → nothing printed).
if [ -n "$PROMPT" ]; then
  # 2026-04-16 R-6: secret passed via header file to avoid process-table
  # exposure. Shared tempfile + trap set up once at the top of the script.
  SECRET=""
  [ -n "$HEADER_FILE_CB" ] && SECRET="present"
  if [ -n "$SECRET" ]; then
    REQ_BODY=$(printf '%s' "$PAYLOAD" | jq -nc \
      --arg p "$PROMPT" \
      --arg s "$SESSION_ID" \
      --argjson t "$TURN_IDX" \
      --arg a "claude" \
      --arg c "${CWD_RAW:-$(pwd)}" \
      '{prompt:$p, session_id:$s, turn_idx:$t, agent:$a, cwd:$c}' 2>/dev/null || echo "")
    if [ -n "$REQ_BODY" ]; then
      ACTIVE_RESP=$(curl -sS --max-time 1.2 \
        -H "@$HEADER_FILE_CB" \
        -H "Content-Type: application/json" \
        --data "$REQ_BODY" \
        "${BRAIN_URL}/recall/active" 2>/dev/null || echo "")
      if [ -n "$ACTIVE_RESP" ]; then
        printf '%s' "$ACTIVE_RESP" | jq -r '
          if (.blocks // []) | length == 0 then empty
          else
            "### Brain Active Recall — per-turn injection",
            (.blocks[]? | "- **\(.title // "untitled")** [\(.source // "?")] \(.content // "" | gsub("\n"; " ") | .[:300])"),
            ""
          end
        ' 2>/dev/null || true
      fi
    fi
  fi
fi

# ── Step 4: Brain doorbell ───────────────────────────────────────
# brain_loop writes urgent context here when it decides Chris needs to see
# something now. File is consumed (read + deleted) on each turn.
DOORBELL="/tmp/.brain_doorbell.${SESSION_ID}.jsonl"
if [ -f "$DOORBELL" ]; then
  # Format each line as an injection block. Lines are newline-delimited JSON.
  if command -v jq >/dev/null 2>&1; then
    jq -r '
      "### ⚠ Brain Doorbell — \(.source // "brain_loop") [\(.priority // "medium")]",
      "**\(.title // "untitled")**",
      (.content // ""),
      ""
    ' "$DOORBELL" 2>/dev/null || cat "$DOORBELL"
  else
    cat "$DOORBELL"
  fi
  rm -f "$DOORBELL"
fi

# ── Step 5: Working-directory canonical notes (preserved from v2) ────
# A second targeted recall that uses the cwd name as the query. Kept because it
# surfaces project-specific canonical notes that the agent baseline queries miss.
# Will be retired once active_recall routes handle project intents.
CWD=$(pwd)
CWD_NAME=$(basename "$CWD")

if [ -n "$CWD_NAME" ] && [ "$CWD_NAME" != "~" ] && [ "$CWD_NAME" != "/" ] && [ "$CWD_NAME" != "chrischo" ]; then
  # 2026-04-16 R-6: secret passed via header file to avoid process-table
  # exposure. Shared tempfile + trap set up once at the top of the script.
  SECRET=""
  [ -n "$HEADER_FILE_CB" ] && SECRET="present"
  if [ -n "$SECRET" ]; then
    export BRAIN_BOOT_CONTEXT="$RESULT"
    ENCODED_CWD=$(printf '%s' "$CWD_NAME" | "$BRAIN_PY" -c "import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip(), safe=''))" 2>/dev/null)
    [ -z "$ENCODED_CWD" ] && ENCODED_CWD="$CWD_NAME"
    curl -s --max-time 2 \
      -H "@$HEADER_FILE_CB" \
      "${BRAIN_URL}/recall?q=${ENCODED_CWD}&collection=canonical&n=5" 2>/dev/null | \
      "$BRAIN_PY" -c "
import sys, json, os
try:
    d = json.load(sys.stdin)
    results = d.get('results', [])
    existing_context = os.environ.get('BRAIN_BOOT_CONTEXT', '')
    novel = []
    for r in results:
        title = (r.get('title') or '?')[:60]
        if title and title not in existing_context:
            novel.append((title, r))
        if len(novel) >= 3:
            break
    if novel:
        print('### Working Directory Context (canonical notes matching cwd)')
        for title, r in novel:
            content = (r.get('content') or '')[:300]
            print(f'- **{title}**: {content}')
        print('')
except Exception:
    pass
" 2>/dev/null || true
  fi
fi

# ── Step 6: Touch wake file for brain_loop (fires within 1s) ─────
# brain_loop's file watcher fires tick() on mtime change. This gives brain a
# chance to react to Chris's new prompt immediately instead of waiting up to 60s.
# File created by Phase 2; touching it now is a no-op if the watcher isn't up yet.
touch /tmp/.brain_loop_wake 2>/dev/null || true

exit 0
