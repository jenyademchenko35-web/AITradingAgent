#!/bin/bash
# Read-only status report. It never starts, stops, or alters a process.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=production_stack_lib.sh
source "$SCRIPT_DIR/production_stack_lib.sh"

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

file_freshness() {
  local file="$1" now modified age
  if [[ ! -f "$file" ]]; then
    echo "missing"
    return
  fi
  now="$(date +%s)"
  modified="$(stat -f %m "$file")"
  age=$((now - modified))
  printf '%ss ago' "$age"
}

echo "AITradingAgent Production Stack"
echo
for label in "${STACK_LABELS[@]}"; do
  printf '%s: %s\n' "$(service_name "$label")" "$(state_for "$label")"
done
echo
telegram_count="$( { ps -axo command= 2>/dev/null || true; } | awk '/[t]elegram_bot_v4\.py/ {count++} END {print count+0}')"
echo "Telegram instances: $telegram_count"
if launchctl print "$LAUNCH_DOMAIN/com.aitradingagent.watchdog" >/dev/null 2>&1; then
  echo "Old watchdog: ON (migration blocked)"
else
  echo "Old watchdog: OFF"
fi
if [[ -d "$PRODUCTION_ROOT" ]]; then
  echo "Git branch: $(git -C "$PRODUCTION_ROOT" branch --show-current 2>/dev/null || echo UNKNOWN)"
  echo "Git HEAD: $(git -C "$PRODUCTION_ROOT" rev-parse --short HEAD 2>/dev/null || echo UNKNOWN)"
  [[ -f "$PRODUCTION_ROOT/research.db" ]] && echo "Research DB: present" || echo "Research DB: missing"
  echo "Runtime snapshot: $(file_freshness "$PRODUCTION_ROOT/runtime_snapshot.json")"
else
  echo "Production root: missing ($PRODUCTION_ROOT)"
fi
