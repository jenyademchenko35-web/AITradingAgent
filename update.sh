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

start_process() {
  local name="$1"
  shift
  nohup "$@" >> "$LOG_DIR/$name.log" 2>&1 &
  echo "$name started: $!"
}

echo "Stopping existing AITradingAgent services..."
echo "Agent is not managed by update.sh; use the controlled systemd handoff procedure."
stop_process "telegram_bot_v4.py"
stop_process "market_news_observer.py"
stop_process "live_market_monitor.py"

sleep 2

echo "Starting AITradingAgent services..."
start_process "telegram_bot_v4" "$PY" -u "$BASE_DIR/telegram_bot_v4.py"
start_process "market_news_observer" "$PY" -u "$BASE_DIR/market_news_observer.py" --loop --interval 1800
start_process "live_market_monitor" "$PY" -u "$BASE_DIR/live_market_monitor.py" --interval 3 --provider auto

echo
echo "Expected running processes:"
echo "- multi_timeframe_agent_v3.py (managed separately; unchanged by this script)"
echo "- telegram_bot_v4.py"
echo "- market_news_observer.py"
echo "- live_market_monitor.py"
echo
pgrep -af "multi_timeframe_agent_v3.py|telegram_bot_v4.py|market_news_observer.py|live_market_monitor.py" || true
