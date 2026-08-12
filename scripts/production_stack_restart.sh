#!/bin/bash
# Restart only loaded, scoped production labels.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=production_stack_lib.sh
source "$SCRIPT_DIR/production_stack_lib.sh"

for label in "${STACK_LABELS[@]}"; do
  if ! launchctl print "$LAUNCH_DOMAIN/$label" >/dev/null 2>&1; then
    echo "Not loaded: $label (use production_stack_start.sh)" >&2
    exit 2
  fi
  launchctl kickstart -k "$LAUNCH_DOMAIN/$label"
  echo "Restarted: $label"
done
