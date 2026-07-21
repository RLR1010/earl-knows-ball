#!/usr/bin/env bash
set -e

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

# 4. Wait for it to be ready
for i in $(seq 1 10); do
    if curl -sf http://localhost:8001/health >/dev/null 2>&1; then
        echo "API server is healthy (${i}s)"
        exit 0
    fi
    sleep 1
done

echo "ERROR: API server did not become healthy within 10 seconds" >&2
exit 1
