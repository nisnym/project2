#!/usr/bin/env bash
# One command, whole product. Boots the six backend services, then runs the
# SPA in the foreground. Ctrl-C stops everything.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

"$ROOT/backend/start.sh"

cleanup() {
  printf "\nStopping services…\n"
  "$ROOT/backend/stop.sh" || true
}
trap cleanup EXIT INT TERM

cd "$ROOT/frontend"
if [ ! -d node_modules ]; then
  printf "\nInstalling frontend dependencies (first run only)…\n"
  npm install
fi

printf "\nStarting the app on http://localhost:5173\n\n"
npm run dev
