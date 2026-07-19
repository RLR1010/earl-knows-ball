#!/usr/bin/env bash
cd "$(dirname "$0")"
VIRTUAL_ENV_DIR="$(dirname "$0")/../venv"
if [ -f "$VIRTUAL_ENV_DIR/bin/activate" ]; then
    source "$VIRTUAL_ENV_DIR/bin/activate"
fi
PYTHONPATH="$PWD" exec granian --interface asgi --http 1 app.main:app --host 0.0.0.0 --port 8001