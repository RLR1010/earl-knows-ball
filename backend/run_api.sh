#!/bin/bash
cd /home/rich/.openclaw/workspace/earl-knows-football/backend
PYTHONPATH="$PWD" exec granian --interface asgi app.main:app --host 0.0.0.0 --port 8001
