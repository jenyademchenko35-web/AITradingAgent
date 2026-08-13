#!/bin/bash
# Shared, scoped helpers for the five LaunchAgents of the production stack.
# This file deliberately never refers to the deprecated watchdog label.
set -euo pipefail

# The defaults are the production locations. Environment overrides exist solely
# for read-only diagnostics and their isolated tests; management scripts still
# require explicit user invocation before taking any action.
PRODUCTION_ROOT="${PRODUCTION_ROOT:-/Users/jeynademcenko/AITradingAgentUpdated}"
LAUNCH_AGENTS_DIR="${LAUNCH_AGENTS_DIR:-${HOME}/Library/LaunchAgents}"
LAUNCH_DOMAIN="${LAUNCH_DOMAIN:-gui/$(id -u)}"
STACK_LABELS=(
  "com.aitradingagent.production.agent"
  "com.aitradingagent.production.telegram"
  "com.aitradingagent.production.market"
  "com.aitradingagent.production.news"
  "com.aitradingagent.production.runtime-publisher"
)

label_plist() {
  printf '%s/%s.plist\n' "$LAUNCH_AGENTS_DIR" "$1"
}

require_production_root() {
  if [[ ! -d "$PRODUCTION_ROOT" || ! -x "$PRODUCTION_ROOT/venv/bin/python" ]]; then
    echo "Production root or venv is unavailable: $PRODUCTION_ROOT" >&2
    exit 2
  fi
}

service_name() {
  case "$1" in
    *.agent) echo "Agent" ;;
    *.telegram) echo "Telegram" ;;
    *.market) echo "Market" ;;
    *.news) echo "News" ;;
    *.runtime-publisher) echo "Runtime Publisher" ;;
    *) echo "$1" ;;
  esac
}
