#!/usr/bin/env bash
# Generic Earl backend launcher for Granian.
# Honors EARL_ROLE (default "all" = current dev-box behavior: everything).
#   EARL_ROLE=api     -> user-facing routers only (v1/mobile, chat, games, ...)
#   EARL_ROLE=compute -> ingest/stats/writeups/admin + the task scheduler
#   EARL_ROLE=all     -> everything + scheduler (dev box)
cd "$(dirname "$0")"
export EARL_ROLE="${EARL_ROLE:-all}"
export EARL_PORT="${EARL_PORT:-8001}"
export EARL_WORKERS="${EARL_WORKERS:-8}"
VIRTUAL_ENV_DIR="$(dirname "$0")/../venv"
if [ -f "$VIRTUAL_ENV_DIR/bin/activate" ]; then
    source "$VIRTUAL_ENV_DIR/bin/activate"
fi
# EARL_PORT/EARL_WORKERS are configurable so the same script drives both the
# api (EARL_ROLE=api) and compute (EARL_ROLE=compute) granian servers.
PYTHONPATH="$PWD" exec granian --interface asgi --http 1 app.main:app --host 0.0.0.0 --port "$EARL_PORT" --workers "$EARL_WORKERS" --backlog 4096 --workers-kill-timeout 10s
