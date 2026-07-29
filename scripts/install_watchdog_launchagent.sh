#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LABEL="com.aitradingagent.watchdog"
SOURCE_PLIST="${SCRIPT_DIR}/${LABEL}.plist"
TARGET_DIR="${HOME}/Library/LaunchAgents"
TARGET_PLIST="${TARGET_DIR}/${LABEL}.plist"
USER_DOMAIN="gui/$(id -u)"

mkdir -p "${TARGET_DIR}" "${PROJECT_ROOT}/logs"
sed "s|/Users/jeynademcenko/AITradingAgent|${PROJECT_ROOT}|g" "${SOURCE_PLIST}" > "${TARGET_PLIST}"

launchctl bootout "${USER_DOMAIN}" "${TARGET_PLIST}" 2>/dev/null || true
launchctl bootstrap "${USER_DOMAIN}" "${TARGET_PLIST}"
launchctl enable "${USER_DOMAIN}/${LABEL}"
launchctl kickstart -k "${USER_DOMAIN}/${LABEL}"
launchctl print "${USER_DOMAIN}/${LABEL}"
