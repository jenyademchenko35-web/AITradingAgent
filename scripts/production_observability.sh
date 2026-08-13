#!/bin/bash
# Read-only production observability.  This script never manages processes,
# contacts a network service, or writes runtime / Research Lab data.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=production_stack_lib.sh
source "$SCRIPT_DIR/production_stack_lib.sh"

RUNTIME_MAX_AGE_SECONDS=900
LIVE_MONITOR_MAX_AGE_SECONDS=30
DEFAULT_LOG_TAIL_LINES=80
MAX_LOG_TAIL_LINES=500
raw_log_tail_lines="${OBSERVABILITY_LOG_TAIL_LINES:-$DEFAULT_LOG_TAIL_LINES}"
if [[ ! "$raw_log_tail_lines" =~ ^[0-9]+$ ]] || (( 10#$raw_log_tail_lines == 0 )); then
  LOG_TAIL_LINES="$DEFAULT_LOG_TAIL_LINES"
elif (( ${#raw_log_tail_lines} > 6 || 10#$raw_log_tail_lines > MAX_LOG_TAIL_LINES )); then
  LOG_TAIL_LINES="$MAX_LOG_TAIL_LINES"
else
  LOG_TAIL_LINES="$((10#$raw_log_tail_lines))"
fi
LOG_DIR="${PRODUCTION_LOG_DIR:-$PRODUCTION_ROOT/logs}"

degraded_reasons=()
production_state="HEALTHY"
performance_state="NORMAL"

add_degraded() {
  degraded_reasons+=("$1")
  production_state="DEGRADED"
}

state_for_observability() {
  local label="$1" details pid
  if ! details="$(launchctl print "$LAUNCH_DOMAIN/$label" 2>/dev/null)"; then
    printf 'STOPPED|\n'
    return 0
  fi
  pid="$(awk '/^[[:space:]]*pid = / {gsub(";", "", $3); print $3; exit}' <<<"$details")"
  if [[ -n "$pid" ]]; then
    printf 'RUNNING|%s\n' "$pid"
  else
    printf 'LOADED_NO_PID|\n'
  fi
}

safe_file_age() {
  local file="$1" now modified
  [[ -f "$file" ]] || return 1
  now="$(date +%s 2>/dev/null)" || return 1
  modified="$(stat -f %m "$file" 2>/dev/null)" || return 1
  printf '%s' "$((now - modified))"
}

resource_snapshot() {
  local pid="$1" sample cpu rss elapsed
  [[ -n "$pid" ]] || { printf 'unavailable'; return 0; }
  sample="$(ps -p "$pid" -o %cpu=,rss=,etime= 2>/dev/null)" || { printf 'unavailable'; return 0; }
  cpu="$(awk '{print $1; exit}' <<<"$sample")"
  rss="$(awk '{print $2; exit}' <<<"$sample")"
  elapsed="$(awk '{print $3; exit}' <<<"$sample")"
  if [[ -z "$cpu" || -z "$rss" || -z "$elapsed" ]]; then
    printf 'unavailable'
  else
    printf 'CPU %s%% | RSS %.1f MB | elapsed %s' "$cpu" "$(awk -v value="$rss" 'BEGIN { print value / 1024 }')" "$elapsed"
  fi
}

latest_monitor_telemetry() {
  local log="$LOG_DIR/live_monitor.log" line
  if [[ ! -r "$log" ]]; then
    printf 'unavailable'
    return 0
  fi
  line="$(tail -n "$LOG_TAIL_LINES" "$log" 2>/dev/null | awk '/cycle_ms=/ {line=$0} END {print line}')"
  if [[ -z "$line" ]]; then
    printf 'unavailable'
  else
    printf '%s' "$line"
  fi
}

field_from_telemetry() {
  local telemetry="$1" name="$2"
  awk -v key="$name" '{ for (i = 1; i <= NF; i++) { split($i, item, "="); if (item[1] == key) { print item[2]; exit } } }' <<<"$telemetry"
}

is_nonnegative_integer() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

research_evidence() {
  local python="$PRODUCTION_ROOT/venv/bin/python" output
  [[ -x "$python" ]] || { printf 'UNAVAILABLE|Research Python unavailable'; return 0; }
  [[ -f "$PRODUCTION_ROOT/research.db" ]] || { printf 'UNAVAILABLE|Research DB unavailable'; return 0; }
  if ! output="$(cd "$PRODUCTION_ROOT" 2>/dev/null && "$python" -c '
from research_lab_v2.trace_outcome import current_pipeline_summary
import sys
report = current_pipeline_summary(sys.argv[1])
next_item = report.get("next_milestone") or {}
print("|".join(str(report.get(key, "")) for key in (
    "status", "historical_unresolved", "fully_joined", "partial", "broken", "current_pipeline_regression"
)) + "|" + str(next_item.get("label", "NONE")) + "|" + str(next_item.get("progress", 0)) + "|" + str(next_item.get("target", 0)))
' "$PRODUCTION_ROOT/research.db" 2>/dev/null)"; then
    printf 'UNAVAILABLE|Research projection failed'
    return 0
  fi
  [[ -n "$output" ]] || { printf 'UNAVAILABLE|Research projection returned no data'; return 0; }
  printf '%s' "$output"
}

json_scalar() {
  local file="$1" key="$2" python="$PRODUCTION_ROOT/venv/bin/python"
  [[ -x "$python" && -r "$file" ]] || { printf 'unavailable'; return 0; }
  "$python" -c '
import json
import sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        value = json.load(handle).get(sys.argv[2])
    print(value if value not in (None, "") else "unavailable")
except (OSError, ValueError, AttributeError):
    print("unavailable")
' "$file" "$key" 2>/dev/null || printf 'unavailable'
}

recent_tail_findings() {
  local found="" log name
  for name in agent telegram market news runtime_publisher; do
    log="$LOG_DIR/launchd_${name}_error.log"
    [[ -r "$log" ]] || continue
    found+="$(tail -n "$LOG_TAIL_LINES" "$log" 2>/dev/null | grep -E 'Traceback|ERROR|CRITICAL|Conflict|database is locked|NaN|Infinity' | tail -n 3 | sed "s#^#${name}: #")"$'\n'
  done
  if [[ -n "${found//$'\n'/}" ]]; then
    printf '%s' "$found"
  else
    printf 'None'
  fi
}

echo "AITradingAgent Production Observability"
echo
echo "Git"
git_branch="$(git -C "$PRODUCTION_ROOT" branch --show-current 2>/dev/null || true)"
git_head="$(git -C "$PRODUCTION_ROOT" rev-parse --short HEAD 2>/dev/null || true)"
git_subject="$(git -C "$PRODUCTION_ROOT" log -1 --format=%s 2>/dev/null || true)"
if dirty_tracked="$(git -C "$PRODUCTION_ROOT" status --porcelain --untracked-files=no 2>/dev/null)"; then
  dirty_tracked_state="$([[ -n "$dirty_tracked" ]] && echo YES || echo NO)"
else
  dirty_tracked_state="UNKNOWN"
  add_degraded "Git status unavailable"
fi
printf 'Branch: %s\n' "${git_branch:-UNKNOWN}"
printf 'HEAD: %s%s\n' "${git_head:-UNKNOWN}" "${git_subject:+ $git_subject}"
printf 'Dirty tracked: %s\n' "$dirty_tracked_state"
[[ -n "$git_branch" && -n "$git_head" ]] || add_degraded "Git metadata unavailable"

echo
echo "Stack"
pids=()
for label in "${STACK_LABELS[@]}"; do
  state_pid="$(state_for_observability "$label")"
  state="${state_pid%%|*}"
  pid="${state_pid#*|}"
  pids+=("$pid")
  printf '%s: %s%s\n' "$(service_name "$label")" "$state" "${pid:+ pid=$pid}"
  [[ "$state" == "RUNNING" ]] || add_degraded "$(service_name "$label") is $state"
done
telegram_count="$({ ps -axo command= 2>/dev/null || true; } | awk '/[t]elegram_bot_v4\.py/ {count++} END {print count+0}')"
printf 'Telegram instances: %s\n' "$telegram_count"
[[ "$telegram_count" == "1" ]] || add_degraded "Telegram instances=$telegram_count (expected 1)"
if launchctl print "$LAUNCH_DOMAIN/com.aitradingagent.watchdog" >/dev/null 2>&1; then
  echo "Old watchdog: ON"
  add_degraded "old watchdog is ON"
else
  echo "Old watchdog: OFF"
fi

echo
echo "Resources"
for index in "${!STACK_LABELS[@]}"; do
  resource="$(resource_snapshot "${pids[$index]}")"
  printf '%s: %s\n' "$(service_name "${STACK_LABELS[$index]}")" "$resource"
  [[ "$resource" != "unavailable" ]] || add_degraded "$(service_name "${STACK_LABELS[$index]}") resource snapshot unavailable"
done

echo
echo "Freshness"
for item in "Runtime snapshot|runtime_snapshot.json|$RUNTIME_MAX_AGE_SECONDS" "Live monitor|live_monitor_state.json|$LIVE_MONITOR_MAX_AGE_SECONDS"; do
  IFS='|' read -r label file max_age <<<"$item"
  age="$(safe_file_age "$PRODUCTION_ROOT/$file" 2>/dev/null || true)"
  if [[ -z "$age" ]]; then
    printf '%s: unavailable\n' "$label"
    add_degraded "$label unavailable"
  else
    printf '%s: %ss\n' "$label" "$age"
    (( age <= max_age )) || add_degraded "$label stale (${age}s > ${max_age}s)"
  fi
done
if [[ -f "$PRODUCTION_ROOT/research.db" ]]; then
  echo "Research DB: present"
else
  echo "Research DB: missing"
  add_degraded "Research DB missing"
fi
research_status_age="$(safe_file_age "$PRODUCTION_ROOT/research_lab_v2_status.json" 2>/dev/null || true)"
printf 'Research status: %s\n' "${research_status_age:+${research_status_age}s}${research_status_age:-unavailable}"
printf 'Latest agent cycle: %s\n' "$(json_scalar "$PRODUCTION_ROOT/runtime_snapshot.json" generated_at)"
printf 'Research Lab processed: %s\n' "$(json_scalar "$PRODUCTION_ROOT/research_lab_v2_status.json" last_processed_cycle)"

echo
echo "Market monitor"
telemetry="$(latest_monitor_telemetry)"
if [[ "$telemetry" == "unavailable" ]]; then
  echo "Telemetry: unavailable"
  performance_state="OBSERVE"
else
  telemetry_reason=""
  status="$(field_from_telemetry "$telemetry" status)"
  cycle_ms="$(field_from_telemetry "$telemetry" cycle_ms)"
  tracked="$(field_from_telemetry "$telemetry" tracked)"
  ticker_calls="$(field_from_telemetry "$telemetry" ticker_calls)"
  appended="$(field_from_telemetry "$telemetry" history_appended)"
  compacted="$(field_from_telemetry "$telemetry" history_compacted)"
  history_bytes="$(field_from_telemetry "$telemetry" history_bytes)"
  hits="$(field_from_telemetry "$telemetry" cache_hits)"
  misses="$(field_from_telemetry "$telemetry" cache_misses)"
  for numeric_field in cycle_ms tracked ticker_calls appended history_bytes hits misses; do
    value="${!numeric_field}"
    if [[ -z "$value" ]]; then
      telemetry_reason="missing $numeric_field"
      break
    elif ! is_nonnegative_integer "$value"; then
      telemetry_reason="non-numeric $numeric_field"
      break
    fi
  done
  if [[ -z "$telemetry_reason" && -z "$status" ]]; then
    telemetry_reason="missing status"
  elif [[ -z "$telemetry_reason" && "$compacted" != "True" && "$compacted" != "False" ]]; then
    telemetry_reason="invalid history_compacted"
  fi
  if [[ -n "$telemetry_reason" ]]; then
    printf 'Telemetry: unavailable (%s)\n' "$telemetry_reason"
    performance_state="OBSERVE"
  else
    printf 'Cycle: %sms | tracked: %s | ticker calls: %s\n' "$cycle_ms" "$tracked" "$ticker_calls"
    printf 'History append: %s | compaction: %s | bytes: %s\n' "$appended" "$compacted" "$history_bytes"
    printf 'Cache: %s hit / %s miss\n' "$hits" "$misses"
  fi
fi

echo
echo "Research Evidence"
evidence="$(research_evidence)"
if [[ "$evidence" == UNAVAILABLE\|* || -z "$evidence" ]]; then
  evidence_reason="${evidence#UNAVAILABLE|}"
  echo "Evidence: unavailable (${evidence_reason:-Research projection unavailable})"
  research_state="UNAVAILABLE"
else
  IFS='|' read -r evidence_status historical fully partial broken regression next_label next_progress next_target <<<"$evidence"
  if [[ "$evidence_status" != "OK" ]]; then
    echo "Evidence: unavailable (Research projection status: ${evidence_status:-UNKNOWN})"
    research_state="UNAVAILABLE"
  elif ! is_nonnegative_integer "$historical" || ! is_nonnegative_integer "$fully" || \
     ! is_nonnegative_integer "$partial" || ! is_nonnegative_integer "$broken" || \
     ! is_nonnegative_integer "$next_progress" || ! is_nonnegative_integer "$next_target" || \
     [[ "$regression" != "True" && "$regression" != "False" || -z "$next_label" ]]; then
    echo "Evidence: unavailable (Research projection output invalid)"
    research_state="UNAVAILABLE"
  else
    printf 'Fully joined: %s\n' "$fully"
    printf 'Partial/Broken: %s/%s\n' "$partial" "$broken"
    printf 'Historical debt: %s\n' "$historical"
    printf 'Next: %s — %s/%s\n' "$next_label" "$next_progress" "$next_target"
    if [[ "$regression" == "True" ]]; then
      research_state="CURRENT_PIPELINE_REGRESSION"
    else
      research_state="WAITING_FOR_EVIDENCE"
    fi
  fi
fi

echo
echo "Recent tail findings (timestamps not verified)"
findings="$(recent_tail_findings)"
printf '%s\n' "$findings"

echo
echo "Overall"
printf 'Production: %s\n' "$production_state"
printf 'Research: %s\n' "$research_state"
printf 'Performance: %s\n' "$performance_state"
if (( ${#degraded_reasons[@]} > 0 )); then
  printf 'Reasons: %s\n' "${degraded_reasons[*]}"
fi
