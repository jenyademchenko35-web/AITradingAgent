#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
FRONTEND_DIR="$PROJECT_ROOT/miniapp/frontend"

cd "$FRONTEND_DIR"
npm ci
npm test -- --run

# A non-secret build identifier lets the backend prove which bundle made a request.
BUILD_ID="${VITE_TRADEWATCHER_FRONTEND_BUILD:-${RAILWAY_GIT_COMMIT_SHA:-}}"
if [ -z "$BUILD_ID" ] && command -v git >/dev/null 2>&1; then
    BUILD_ID=$(git -C "$PROJECT_ROOT" rev-parse --short=12 HEAD 2>/dev/null || true)
fi
case "$BUILD_ID" in
    ""|*[!A-Za-z0-9._-]*) BUILD_ID="unknown" ;;
esac
VITE_TRADEWATCHER_FRONTEND_BUILD="$BUILD_ID" npm run build

if [ ! -f "$FRONTEND_DIR/dist/index.html" ]; then
    echo "Mini App build failed: dist/index.html is missing" >&2
    exit 1
fi

echo "Mini App frontend build verified"
