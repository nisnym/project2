#!/usr/bin/env bash
# Boot all six services. Idempotent: run it again and it restarts cleanly.
set -euo pipefail

cd "$(dirname "$0")"
RUN_DIR=".run"
mkdir -p "$RUN_DIR"

# name:port:module
SERVICES=(
  "customer-service:9001:services.customer_service.main:app"
  "transaction-service:9002:services.transaction_service.main:app"
  "budget-service:9003:services.budget_service.main:app"
  "insight-service:9004:services.insight_service.main:app"
  "chat-service:9005:services.chat_service.main:app"
  "gateway:8080:services.gateway.main:app"
)

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
green() { printf "  \033[32m%s\033[0m %s\n" "$1" "$2"; }
red() { printf "  \033[31m%s\033[0m %s\n" "$1" "$2"; }

# ---------------------------------------------------------------- venv
if [ ! -d ".venv" ]; then
  bold "Creating the virtualenv (first run only)…"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv
  else
    python3 -m venv .venv
  fi
fi

PY=".venv/bin/python"

if [ ! -f "$RUN_DIR/.deps-installed" ]; then
  bold "Installing dependencies…"
  if command -v uv >/dev/null 2>&1; then
    VIRTUAL_ENV="$(pwd)/.venv" uv pip install --quiet -e ".[dev]"
  else
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -e ".[dev]"
  fi
  touch "$RUN_DIR/.deps-installed"
fi

# --------------------------------------------------------------- start
./stop.sh >/dev/null 2>&1 || true

bold "Starting services…"
for entry in "${SERVICES[@]}"; do
  name="${entry%%:*}"
  rest="${entry#*:}"
  port="${rest%%:*}"
  module="${rest#*:}"

  # A just-stopped uvicorn can hold its port for a moment. Give it one,
  # then only give up if something else genuinely owns the port.
  for _ in $(seq 1 20); do
    lsof -ti tcp:"$port" >/dev/null 2>&1 || break
    sleep 0.25
  done
  if lsof -ti tcp:"$port" >/dev/null 2>&1; then
    red "✗" "$name — port $port is already in use by another process"
    continue
  fi

  "$PY" -m uvicorn "$module" --host 0.0.0.0 --port "$port" \
    >"$RUN_DIR/$name.log" 2>&1 &
  echo $! > "$RUN_DIR/$name.pid"
done

# ---------------------------------------------------------------- wait
printf "\n"
bold "Waiting for health checks…"
all_up=1
for entry in "${SERVICES[@]}"; do
  name="${entry%%:*}"
  rest="${entry#*:}"
  port="${rest%%:*}"

  up=0
  for _ in $(seq 1 40); do
    if curl -fsS "http://localhost:$port/health" >/dev/null 2>&1; then
      up=1
      break
    fi
    sleep 0.25
  done

  if [ "$up" = "1" ]; then
    green "✓" "$name  →  http://localhost:$port/docs"
  else
    red "✗" "$name failed to start — see $RUN_DIR/$name.log"
    all_up=0
  fi
done

printf "\n"
if [ "$all_up" = "1" ]; then
  bold "All services up."
  echo "  API      http://localhost:8080/docs"
  echo "  Status   http://localhost:8080/health/system"
  echo "  Stop     ./stop.sh"
else
  bold "Some services did not start. Logs are in backend/$RUN_DIR/."
  exit 1
fi
