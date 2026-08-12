#!/bin/bash
# Unload only the five explicit production labels; never use pkill or wildcards.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=production_stack_lib.sh
source "$SCRIPT_DIR/production_stack_lib.sh"

for label in "${STACK_LABELS[@]}"; do
  plist="$(label_plist "$label")"
  if launchctl print "$LAUNCH_DOMAIN/$label" >/dev/null 2>&1; then
    launchctl bootout "$LAUNCH_DOMAIN" "$plist"
    echo "Stopped: $label"
  else
    echo "Not loaded: $label"
  fi
done
