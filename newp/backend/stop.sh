#!/usr/bin/env bash
# Stop everything start.sh started.
set -uo pipefail

cd "$(dirname "$0")"
RUN_DIR=".run"

[ -d "$RUN_DIR" ] || exit 0

for pidfile in "$RUN_DIR"/*.pid; do
  [ -e "$pidfile" ] || continue
  name="$(basename "$pidfile" .pid)"
  pid="$(cat "$pidfile")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null
    printf "  stopped %s (pid %s)\n" "$name" "$pid"
  fi
  rm -f "$pidfile"
done

# Anything left holding our ports (a stray uvicorn from a previous shell).
for port in 9001 9002 9003 9004 9005 8080; do
  pids="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
  for pid in $pids; do
    if ps -p "$pid" -o command= 2>/dev/null | grep -q "uvicorn"; then
      kill "$pid" 2>/dev/null && printf "  stopped stray uvicorn on :%s\n" "$port"
    fi
  done
done
