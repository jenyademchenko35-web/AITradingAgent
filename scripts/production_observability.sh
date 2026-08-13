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
CURRENT_ISSUE_WINDOW_SECONDS=1800
# Naive application log timestamps are interpreted using the server-local
# timezone unless an explicit IANA timezone is supplied for a deployment.
LOG_TIMESTAMP_TIMEZONE="${OBSERVABILITY_LOG_TIMEZONE:-LOCAL}"
MAX_TRACKED_CHANGE_PATHS=8
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
news_state="NORMAL"

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
    printf 'CPU snapshot %s%% | RSS %.1f MB | elapsed %s' "$cpu" "$(awk -v value="$rss" 'BEGIN { print value / 1024 }')" "$elapsed"
  fi
}

latest_monitor_telemetry() {
  local log line latest
  # StateManager writes this log beside live_monitor_state.json.  Keep the
  # logs/ fallback for older deployments without treating it as the primary.
  for log in "$PRODUCTION_ROOT/live_monitor.log" "$LOG_DIR/live_monitor.log"; do
    [[ -r "$log" ]] || continue
    latest=""
    while IFS= read -r line; do
      telemetry_line_is_valid "$line" && latest="$line"
    done < <(tail -n "$LOG_TAIL_LINES" "$log" 2>/dev/null)
    [[ -n "$latest" ]] && { printf '%s' "$latest"; return 0; }
  done
  printf 'unavailable'
}

field_from_telemetry() {
  local telemetry="$1" name="$2"
  awk -v key="$name" '{ for (i = 1; i <= NF; i++) { split($i, item, "="); if (item[1] == key) { print item[2]; exit } } }' <<<"$telemetry"
}

is_nonnegative_integer() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

is_nonnegative_number() {
  [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]]
}

# This reviewed catalog is intentionally explicit.  It is derived from the
# runtime-artifact section of .gitignore, the Research Orchestrator registry,
# and the producers of the report/summary files below.  Some historical
# artifacts are still tracked on production, so Git alone cannot distinguish
# them from code.  Do not classify an arbitrary *_report.json or
# *_summary.txt as runtime output: unknown paths must remain code/config
# dirtiness.
is_runtime_tracked_artifact() {
  local path="$1"
  # The .gitignore exception is intentionally not hidden as runtime dirtiness.
  [[ "$path" == "reports/consistency_report.json" ]] && return 1
  case "$path" in
    logs/*.log|*_dry_run.csv|\
    active_setups_v3.json|bot_config.json|last_notification.json|\
    decision_learning.json|decision_snapshot.json|\
    decision_debug.csv|decision_diagnostics.csv|decision_explanations.csv|\
    candidate_decisions.csv|candidate_shadow_trades.csv|candidate_shadow_open_trades.json|\
    decision_features.csv|reports/candidate_laboratory.json|reports/candidate_comparison.json|\
    diagnostics_report.json|signals_v3.csv|agent_v3_stats.json|ai_coach_report.json|\
    dashboard_state.json|live_monitor_state.json|live_price_history.csv|live_monitor.log|\
    protective_filter_dry_run.csv|sl_quality_protective_dry_run.csv|\
    confidence_sl_quality_d_dry_run.csv|trade_close_notifications.json|\
    market_news_feed.json|market_news_feed.csv|market_news_history.csv|\
    market_news_sources.json|market_news_health.json|market_news_observer.log|\
    market_heatmap.csv|trade_market_context.csv|post_trade_intelligence.json|\
    post_trade_intelligence.csv|trade_memory_matches.csv|news_impact_shadow.csv|\
    news_trade_memory.csv|news_statistics.csv|strategy_metrics.csv|\
    strategy_shadow_trades.csv|strategy_comparison.csv|strategy_comparison.json|\
    hypothesis_comparison.csv|hypothesis_shadow_trades.csv|replay_patterns.csv|\
    research_consensus.csv|research_orchestrator.log|research_orchestrator_evidence.csv|\
    research_orchestrator_conflicts.csv|research.db|research.db-wal|research.db-shm|\
    research_lab_v2_status.json|research_lab_v2_shadow_open.json|\
    research_lab_shadow_history.csv|research_lab_v2_runtime_override.json|\
    runtime_snapshot.json|runtime_ingest/*|signal_episodes.jsonl|signal_outcomes.jsonl|\
    signal_outcome_recoveries.jsonl|signal_evaluation_state.json|\
    signal_evaluation_report.json|shadow_replay_trades.csv|trade_metrics_audit.json|\
    trade_metrics_audit_summary.txt|normalized_trade_metrics.csv|\
    adaptive_research_state.json|.adaptive_research.lock|.watchdog.lock)
      return 0
      ;;
  esac

  # Generated reports/summaries with known project producers.  Keep this list
  # explicit so an unexpected report or summary file remains visible as a
  # tracked code/config change.
  case "$path" in
    ai_coach_report.json|ai_coach_summary.txt|\
    adaptive_research_report.json|adaptive_research_summary.txt|\
    blocked_trade_simulation_report.json|blocked_trade_simulation_summary.txt|\
    blocked_trade_simulation_ALL_report.json|blocked_trade_simulation_ALL_summary.txt|\
    blocked_trade_simulation_Momentum_report.json|blocked_trade_simulation_Momentum_summary.txt|\
    blocked_trade_simulation_Risk_report.json|blocked_trade_simulation_Risk_summary.txt|\
    blocked_trade_simulation_Structure_report.json|blocked_trade_simulation_Structure_summary.txt|\
    blocked_trade_simulation_Trend_report.json|blocked_trade_simulation_Trend_summary.txt|\
    calibration_report.json|confidence_calibration_experiment_report.json|\
    confidence_calibration_experiment_summary.txt|data_quality_report.json|\
    data_quality_summary.txt|dataset_quality_report.json|dataset_quality_summary.txt|\
    decision_pipeline_profile_report.json|decision_pipeline_profile_summary.txt|\
    decision_score_decomposition_report.json|decision_score_decomposition_summary.txt|\
    dry_run_outcome_report.json|experiment_promotion_report.json|\
    filter_effectiveness_report.json|filter_effectiveness_summary.txt|\
    hypothesis_report.json|hypothesis_summary.txt|learning_report.json|\
    market_heatmap_report.json|market_intelligence_report.json|\
    market_intelligence_summary.txt|market_regime_advisor_report.json|\
    market_regime_report.json|market_news_summary.txt|min_edge_calibration_report.json|\
    min_edge_calibration_summary.txt|min_edge_outcome_report.json|\
    min_edge_outcome_summary.txt|momentum_decomposition_report.json|\
    momentum_decomposition_summary.txt|post_loss_decomposition_report.json|\
    post_loss_decomposition_summary.txt|post_trade_analysis_report.json|\
    post_trade_analysis_summary.txt|research_consensus_report.json|\
    research_consensus_summary.txt|research_dashboard_summary.txt|research_hub_report.json|\
    research_hub_summary.txt|research_orchestrator_report.json|\
    runtime_data_foundation_report.json|runtime_data_foundation_summary.txt|\
    score_zero_audit_report.json|score_zero_audit_summary.txt|\
    shadow_replay_report.json|shadow_replay_summary.txt|\
    sl_entry_quality_experiment_report.json|sl_entry_quality_experiment_summary.txt|\
    strategy_calibration_experiments_report.json|strategy_calibration_experiments_summary.txt|\
    strategy_calibration_report.json|strategy_calibration_summary.txt|\
    strategy_experiments_report.json|strategy_experiments_summary.txt|\
    strategy_lab_report.json|strategy_lab_summary.txt|strategy_replay_report.json|\
    strategy_research_report.json|strategy_research_summary.txt|\
    trade_comparator_report.json|trade_comparator_summary.txt|\
    trade_intelligence_summary.txt|reports/trade_intelligence_summary.txt|\
    trade_loss_report.json|trade_memory_report.json|trade_memory_summary.txt|\
    trade_opportunity_expansion_report.json|trade_pattern_discovery_report.json|\
    trade_pattern_discovery_summary.txt|trade_replay_report.json|\
    trend_momentum_conflict_report.json|trend_momentum_conflict_summary.txt|\
    trend_momentum_conflict_v2_report.json|trend_momentum_conflict_v2_summary.txt|\
    walk_forward_report.json|reports/walk_forward.json|reports/signal_quality_summary.txt)
      return 0
      ;;
  esac
  return 1
}

bounded_path_list() {
  local item result="" count=0
  for item in "$@"; do
    (( count >= MAX_TRACKED_CHANGE_PATHS )) && break
    result+="${result:+, }${item}"
    ((count += 1))
  done
  printf '%s' "$result"
}

telemetry_line_is_valid() {
  local line="$1" timestamp status cycle_ms ticker_calls appended compacted history_bytes hits misses
  # Do not accept arbitrary field-bearing text as monitor telemetry. The
  # monitor emits one timestamped event per cycle; requiring and parsing its
  # prefix lets the bounded tail select the latest real event after noise.
  if [[ "$line" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2}[T\ ]{1}[0-9]{2}:[0-9]{2}:[0-9]{2}([.,][0-9]+)?(Z|[+-][0-9]{2}:?[0-9]{2})?) ]]; then
    timestamp="${BASH_REMATCH[1]}"
  else
    return 1
  fi
  [[ -n "$(parse_log_timestamp_epoch "$timestamp")" ]] || return 1
  [[ "$line" == *"status="* && "$line" == *"cycle_ms="* ]] || return 1
  status="$(field_from_telemetry "$line" status)"
  cycle_ms="$(field_from_telemetry "$line" cycle_ms)"
  ticker_calls="$(field_from_telemetry "$line" ticker_calls)"
  appended="$(field_from_telemetry "$line" history_appended)"
  compacted="$(field_from_telemetry "$line" history_compacted)"
  history_bytes="$(field_from_telemetry "$line" history_bytes)"
  hits="$(field_from_telemetry "$line" cache_hits)"
  misses="$(field_from_telemetry "$line" cache_misses)"
  [[ -n "$status" ]] || return 1
  is_nonnegative_number "$cycle_ms" || return 1
  for value in "$ticker_calls" "$appended" "$history_bytes" "$hits" "$misses"; do
    is_nonnegative_integer "$value" || return 1
  done
  [[ "$compacted" == "True" || "$compacted" == "False" ]]
}

research_evidence() {
  local python="$PRODUCTION_ROOT/venv/bin/python" output
  [[ -x "$python" ]] || { printf 'UNAVAILABLE|Research Python unavailable'; return 0; }
  [[ -f "$PRODUCTION_ROOT/research.db" ]] || { printf 'UNAVAILABLE|Research DB unavailable'; return 0; }
  if ! output="$(cd "$PRODUCTION_ROOT" 2>/dev/null && "$python" -c '
from research_lab_v2.health import evidence_watch_progress
from research_lab_v2.trace_outcome import current_pipeline_summary
import sys
report = current_pipeline_summary(sys.argv[1])
next_item = evidence_watch_progress(report).get("next_milestone") or {}
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

parse_log_timestamp_epoch() {
  local timestamp="$1"
  python3 -c '
from datetime import datetime
from zoneinfo import ZoneInfo
import sys

raw, configured_timezone = sys.argv[1], sys.argv[2]
try:
    parsed = datetime.fromisoformat(raw.replace(",", ".").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        timezone = datetime.now().astimezone().tzinfo if configured_timezone == "LOCAL" else ZoneInfo(configured_timezone)
        parsed = parsed.replace(tzinfo=timezone)
    print(int(parsed.timestamp()))
except (TypeError, ValueError):
    pass
' "$timestamp" "$LOG_TIMESTAMP_TIMEZONE" 2>/dev/null || true
}

recent_tail_findings() {
  local current="" unverified="" log name line timestamp epoch now age
  now="$(date +%s 2>/dev/null || true)"
  for name in agent telegram market news runtime_publisher; do
    log="$LOG_DIR/launchd_${name}_error.log"
    [[ -r "$log" ]] || continue
    while IFS= read -r line; do
      [[ -n "$line" ]] || continue
      timestamp=""
      if [[ "$line" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2}[T\ ]{1}[0-9]{2}:[0-9]{2}:[0-9]{2}([.,][0-9]+)?(Z|[+-][0-9]{2}:?[0-9]{2})?) ]]; then
        timestamp="${BASH_REMATCH[1]}"
        epoch="$(parse_log_timestamp_epoch "$timestamp")"
      else
        epoch=""
      fi
      if [[ -z "$epoch" || -z "$now" ]]; then
        unverified+="${name}: ${line}"$'\n'
        continue
      fi
      age=$((now - epoch))
      # Future-dated lines are not claimed to be current either.
      if (( age < 0 || age > CURRENT_ISSUE_WINDOW_SECONDS )); then
        continue
      fi
      current+="${name}: ${line}"$'\n'
      if [[ "$name" == "news" ]]; then
        news_state="DEGRADED"
      else
        add_degraded "fresh ${name} error tail finding"
      fi
    done < <(tail -n "$LOG_TAIL_LINES" "$log" 2>/dev/null | grep -E 'Traceback|ERROR|CRITICAL|Conflict|PARSER_ERROR|database is locked|NaN|Infinity' | tail -n 3)
  done
  if [[ -n "${current//$'\n'/}" ]]; then
    printf 'Current:\n%s' "$current"
  else
    printf 'Current: None\n'
  fi
  if [[ -n "${unverified//$'\n'/}" ]]; then
    printf 'Unverified historical tail:\n%s' "$unverified"
  fi
}

echo "AITradingAgent Production Observability"
echo
echo "Git"
git_branch="$(git -C "$PRODUCTION_ROOT" branch --show-current 2>/dev/null || true)"
git_head="$(git -C "$PRODUCTION_ROOT" rev-parse --short HEAD 2>/dev/null || true)"
git_subject="$(git -C "$PRODUCTION_ROOT" log -1 --format=%s 2>/dev/null || true)"
if tracked_diff="$(git -C "$PRODUCTION_ROOT" diff --name-only --no-renames HEAD 2>/dev/null)"; then
  code_paths=()
  runtime_paths=()
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    if is_runtime_tracked_artifact "$path"; then
      runtime_paths+=("$path")
    else
      code_paths+=("$path")
    fi
  done <<<"$tracked_diff"
  if (( ${#code_paths[@]} > 0 )); then
    code_dirty_state="YES"
  else
    code_dirty_state="NO"
  fi
else
  code_dirty_state="UNKNOWN"
  code_paths=()
  runtime_paths=()
  add_degraded "Git tracked diff unavailable"
fi
printf 'Branch: %s\n' "${git_branch:-UNKNOWN}"
printf 'HEAD: %s%s\n' "${git_head:-UNKNOWN}" "${git_subject:+ $git_subject}"
printf 'Code dirty: %s\n' "$code_dirty_state"
if (( ${#code_paths[@]} > 0 )); then
  printf 'Code changes: %s\n' "$(bounded_path_list "${code_paths[@]}")"
fi
printf 'Runtime tracked changes: %s\n' "${#runtime_paths[@]}"
if (( ${#runtime_paths[@]} > 0 )); then
  printf 'Runtime paths: %s\n' "$(bounded_path_list "${runtime_paths[@]}")"
fi
[[ "$code_dirty_state" == "YES" ]] && add_degraded "tracked source/config changes present"
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
if [[ -n "$research_status_age" ]]; then
  printf 'Research status: %ss\n' "$research_status_age"
else
  echo "Research status: unavailable"
fi
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
  # `tracked` was added as an optional monitor diagnostic after the initial
  # telemetry contract.  Its absence must not turn otherwise valid production
  # telemetry into an unavailable/OBSERVE state.
  for numeric_field in cycle_ms ticker_calls appended history_bytes hits misses; do
    value="${!numeric_field}"
    if [[ -z "$value" ]]; then
      telemetry_reason="missing $numeric_field"
      break
    elif [[ "$numeric_field" == "cycle_ms" ]] && ! is_nonnegative_number "$value"; then
      telemetry_reason="non-numeric $numeric_field"
      break
    elif [[ "$numeric_field" != "cycle_ms" ]] && ! is_nonnegative_integer "$value"; then
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
    printf 'Cycle: %sms | tracked: %s | ticker calls: %s\n' "$cycle_ms" "${tracked:-—}" "$ticker_calls"
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
echo "Recent tail findings"
recent_tail_findings

echo
echo "Overall"
printf 'Production: %s\n' "$production_state"
printf 'Research: %s\n' "$research_state"
printf 'Performance: %s\n' "$performance_state"
printf 'News: %s\n' "$news_state"
if (( ${#degraded_reasons[@]} > 0 )); then
  printf 'Reasons: %s\n' "${degraded_reasons[*]}"
fi
