#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE_DIR"

PY="$BASE_DIR/venv/bin/python"
LOG_DIR="$BASE_DIR/logs"
mkdir -p "$LOG_DIR"

if [ ! -x "$PY" ]; then
  echo "venv python not found: $PY"
  exit 1
fi

stop_process() {
  local pattern="$1"
  pkill -f "$pattern" 2>/dev/null || true
}

wait_for_pids_exit() {
  local pids="$1"
  local timeout_seconds="$2"
  local deadline=$((SECONDS + timeout_seconds))
  local pid
  local alive

  [ -z "$pids" ] && return 0
  while true; do
    alive=0
    for pid in $pids; do
      if kill -0 "$pid" 2>/dev/null; then
        alive=1
      fi
    done
    [ "$alive" -eq 0 ] && return 0
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "agent did not exit within ${timeout_seconds}s; refusing overlapping restart" >&2
      return 1
    fi
    sleep 1
  done
}

start_process() {
  local name="$1"
  shift
  nohup "$@" >> "$LOG_DIR/$name.log" 2>&1 &
  echo "$name started: $!"
}

start_agent_process() {
  nohup "$@" >> "$LOG_DIR/agent_v3.log" 2>&1 &
  local pid=$!
  local status=0
  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    wait "$pid" || status=$?
    [ "$status" -ne 0 ] || status=1
    echo "agent_v3 failed startup (exit $status); singleton may be owned by another launcher" >&2
    return "$status"
  fi
  echo "agent_v3 started: $pid"
}

echo "Stopping existing AITradingAgent services..."
AGENT_PIDS="$(pgrep -f "multi_timeframe_agent_v3.py" 2>/dev/null || true)"
stop_process "multi_timeframe_agent_v3.py"
wait_for_pids_exit "$AGENT_PIDS" 30
stop_process "telegram_bot_v4.py"
stop_process "market_news_observer.py"
stop_process "live_market_monitor.py"

sleep 2

echo "Starting AITradingAgent services..."
start_agent_process "$PY" -u "$BASE_DIR/multi_timeframe_agent_v3.py" --loop --interval 300
start_process "telegram_bot_v4" "$PY" -u "$BASE_DIR/telegram_bot_v4.py"
start_process "market_news_observer" "$PY" -u "$BASE_DIR/market_news_observer.py" --loop --interval 1800
start_process "live_market_monitor" "$PY" -u "$BASE_DIR/live_market_monitor.py" --interval 3 --provider auto

echo
echo "Expected running processes:"
echo "- multi_timeframe_agent_v3.py"
echo "- telegram_bot_v4.py"
echo "- market_news_observer.py"
echo "- live_market_monitor.py"
echo
pgrep -af "multi_timeframe_agent_v3.py|telegram_bot_v4.py|market_news_observer.py|live_market_monitor.py" || true
