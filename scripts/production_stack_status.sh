#!/bin/bash
# Read-only status report. It never starts, stops, or alters a process.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=production_stack_lib.sh
source "$SCRIPT_DIR/production_stack_lib.sh"

# The agent publishes the canonical snapshot every five minutes. Three intervals
# allow for a normal delayed cycle without reporting a healthy stack as stale.
RUNTIME_SNAPSHOT_HEALTHY_MAX_AGE_SECONDS=900

state_for() {
  local label="$1" details pid
  if ! details="$(launchctl print "$LAUNCH_DOMAIN/$label" 2>/dev/null)"; then
    echo "STOPPED"
    return
  fi
  pid="$(awk '/^[[:space:]]*pid = / {gsub(";", "", $3); print $3; exit}' <<<"$details")"
  if [[ -n "$pid" ]]; then
    printf 'RUNNING pid=%s' "$pid"
  else
    echo "LOADED_NO_PID"
  fi
}

file_age_seconds() {
  local file="$1" now modified
  [[ -f "$file" ]] || return 1
  now="$(date +%s)"
  modified="$(stat -f %m "$file")" || return 1
  printf '%s' "$((now - modified))"
}

add_degraded_reason() {
  local reason="$1"
  degraded_reasons+=("$reason")
}

degraded_reasons=()

echo "AITradingAgent Production Stack"
echo
for label in "${STACK_LABELS[@]}"; do
  state="$(state_for "$label")"
  printf '%s: %s\n' "$(service_name "$label")" "$state"
  [[ "$state" == RUNNING* ]] || add_degraded_reason "$(service_name "$label") is $state"
done
echo
telegram_count="$( { ps -axo command= 2>/dev/null || true; } | awk '/[t]elegram_bot_v4\.py/ {count++} END {print count+0}')"
echo "Telegram instances: $telegram_count"
[[ "$telegram_count" == "1" ]] || add_degraded_reason "Telegram instances=$telegram_count (expected 1)"
if launchctl print "$LAUNCH_DOMAIN/com.aitradingagent.watchdog" >/dev/null 2>&1; then
  echo "Old watchdog: ON (migration blocked)"
  add_degraded_reason "old watchdog is ON"
else
  echo "Old watchdog: OFF"
fi
if [[ -d "$PRODUCTION_ROOT" ]]; then
  git_branch="$(git -C "$PRODUCTION_ROOT" branch --show-current 2>/dev/null || true)"
  git_head="$(git -C "$PRODUCTION_ROOT" rev-parse --short HEAD 2>/dev/null || true)"
  if [[ -n "$git_branch" ]]; then
    echo "Git branch: $git_branch"
  else
    echo "Git branch: UNKNOWN"
    add_degraded_reason "Git branch is unavailable"
  fi
  if [[ -n "$git_head" ]]; then
    echo "Git HEAD: $git_head"
  else
    echo "Git HEAD: UNKNOWN"
    add_degraded_reason "Git HEAD is unavailable"
  fi
  if [[ -f "$PRODUCTION_ROOT/research.db" ]]; then
    echo "Research DB: present"
  else
    echo "Research DB: missing"
    add_degraded_reason "Research DB is missing"
  fi
  runtime_snapshot="$PRODUCTION_ROOT/runtime_snapshot.json"
  if [[ ! -f "$runtime_snapshot" ]]; then
    echo "Runtime snapshot: missing"
    add_degraded_reason "runtime snapshot is missing"
  else
    snapshot_age="$(file_age_seconds "$runtime_snapshot" 2>/dev/null || true)"
    if [[ -z "$snapshot_age" ]]; then
      echo "Runtime snapshot: unreadable"
      add_degraded_reason "runtime snapshot is unreadable"
    else
      echo "Runtime snapshot: ${snapshot_age}s ago"
      if (( snapshot_age > RUNTIME_SNAPSHOT_HEALTHY_MAX_AGE_SECONDS )); then
        add_degraded_reason "runtime snapshot is stale (${snapshot_age}s > ${RUNTIME_SNAPSHOT_HEALTHY_MAX_AGE_SECONDS}s)"
      fi
    fi
  fi
else
  echo "Production root: missing ($PRODUCTION_ROOT)"
  add_degraded_reason "production root is missing"
fi

echo
if (( ${#degraded_reasons[@]} == 0 )); then
  echo "Overall: HEALTHY"
else
  echo "Overall: DEGRADED"
  printf 'Reason: %s\n' "${degraded_reasons[@]}"
fi
