#!/usr/bin/env bash
set -e

# If the backend runs under the systemd user service (earl-backend.service),
# delegate to systemd so we don't orphan a granian outside the service and
# race it for port 8001. Falls back to legacy behavior if the service is
# not active (e.g. root cron or a machine without the user manager).
if command -v systemctl >/dev/null 2>&1; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    if systemctl --user is-active earl-backend.service >/dev/null 2>&1; then
        echo "earl-backend.service is active — restarting via systemd"
        exec systemctl --user restart earl-backend.service
    fi
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="${LOG_FILE:-/tmp/earl-api.log}"

echo "=== Restarting Earl API Server ==="

# 1. Kill old granian processes gracefully
OLD_PIDS=$(pgrep -f 'granian.*app.main' 2>/dev/null || true)
if [ -n "$OLD_PIDS" ]; then
    echo "Killing old granian PIDs: $OLD_PIDS"
    kill $OLD_PIDS 2>/dev/null || true

    # Wait for real death (up to 10 seconds)
    for i in $(seq 1 10); do
        if ! pgrep -f 'granian.*app.main' >/dev/null 2>&1; then
            echo "Old granian exited after ${i}s"
            break
        fi
        sleep 1
    done

    # Force kill if still alive
    if pgrep -f 'granian.*app.main' >/dev/null 2>&1; then
        echo "Force killing remaining granian..."
        pkill -9 -f 'granian.*app.main' 2>/dev/null || true
        sleep 1
    fi
else
    echo "No running granian found"
fi

# 2. Rotate the log (rename preserves old file for reading)
if [ -f "$LOG_FILE" ]; then
    mv "$LOG_FILE" "${LOG_FILE}.old"
    echo "Rotated old log to ${LOG_FILE}.old"
fi

# 3. Start new server with appending (belt + suspenders)
echo "Starting new granian server..."
cd "$SCRIPT_DIR"

# Run with append redirect so even if old process briefly survives, no sparse null bytes
nohup "$SCRIPT_DIR/run_api.sh" >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "Started granian (PID $NEW_PID)"

# 4. Wait for it to be ready (more time for --workers 4)
for i in $(seq 1 30); do
    if curl -sf http://localhost:8001/health >/dev/null 2>&1; then
        echo "API server is healthy (${i}s)"
        exit 0
    fi
    sleep 1
done

echo "ERROR: API server did not become healthy within 30 seconds" >&2
exit 1
