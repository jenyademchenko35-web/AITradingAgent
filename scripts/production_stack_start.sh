#!/bin/bash
# Bootstrap only the five explicit production labels from already-installed plists.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=production_stack_lib.sh
source "$SCRIPT_DIR/production_stack_lib.sh"
require_production_root

for label in "${STACK_LABELS[@]}"; do
  plist="$(label_plist "$label")"
  [[ -f "$plist" ]] || { echo "Missing installed plist: $plist" >&2; exit 2; }
  if launchctl print "$LAUNCH_DOMAIN/$label" >/dev/null 2>&1; then
    echo "Already loaded: $label"
  else
    launchctl bootstrap "$LAUNCH_DOMAIN" "$plist"
    echo "Started: $label"
  fi
done
