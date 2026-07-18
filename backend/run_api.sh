#!/usr/bin/env bash
cd "$(dirname "$0")"
PYTHONPATH="$PWD" exec granian --interface asgi --http 1 app.main:app --host 0.0.0.0 --port 8001