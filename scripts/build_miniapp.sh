#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
FRONTEND_DIR="$PROJECT_ROOT/miniapp/frontend"

cd "$FRONTEND_DIR"
npm ci
npm test -- --run
npm run build

if [ ! -f "$FRONTEND_DIR/dist/index.html" ]; then
    echo "Mini App build failed: dist/index.html is missing" >&2
    exit 1
fi

echo "Mini App frontend build verified"
