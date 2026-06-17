#!/bin/bash
# Clear stale compiled Python cache on startup
find /app -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
exec granian --interface asgi app.main:app --host 0.0.0.0 --port 8001
