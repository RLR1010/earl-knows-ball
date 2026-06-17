#!/bin/bash
# Multi-sport pgvector embedding runner — NFL, NBA, MLB.
# Runs inside the existing API container (no separate container needed).
# The main PostgreSQL container already has pgvector extension built in.
#
# Usage:
#   ./embed_all.sh {start|start-batch|status|logs|stop}
#   ./embed_all.sh start-batch  # single pass then exit (great for one-shot)

set -e

API_CONTAINER="backend-api-1"
EMBED_SCRIPT="/app/run_embed_pgvector_all.py"

case "${1:-start}" in
  start)
    echo "Starting multi-sport embedder (NFL + NBA + MLB) inside API container..."
    docker exec -d "$API_CONTAINER" python3 "$EMBED_SCRIPT" --loop
    echo "Embedder started inside $API_CONTAINER."
    echo "Run './embed_all.sh logs' to see progress."
    ;;

  start-batch)
    echo "Running one-shot batch embed (single pass per sport)..."
    docker exec -d "$API_CONTAINER" python3 "$EMBED_SCRIPT" --one-shot
    echo "One-shot batch started."
    ;;

  status)
    # Check if the embed script is running inside the API container
    RUNNING=$(docker exec "$API_CONTAINER" sh -c 'pgrep -f run_embed_pgvector_all.py' 2>/dev/null || true)
    if [ -n "$RUNNING" ]; then
      echo "Running (PID(s): $RUNNING)"
      docker exec "$API_CONTAINER" sh -c 'ps aux | grep run_embed_pgvector' | grep -v grep
    else
      echo "Not running"
    fi
    ;;

  logs)
    docker exec "$API_CONTAINER" sh -c 'cat /var/log/embedder.log 2>/dev/null || echo "No log file found"'
    ;;

  stop)
    echo "Stopping embedder..."
    docker exec "$API_CONTAINER" sh -c 'pkill -f run_embed_pgvector_all.py' 2>/dev/null || true
    echo "Stopped."
    ;;

  *)
    echo "Usage: $0 {start|start-batch|status|logs|stop}"
    exit 1
    ;;
esac
