#!/usr/bin/env bash
# Runner script for MLB stats ingestion
# Usage: ./mlb_stats_loader.sh {start|status|logs|stop|batting-only|games-only}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

CONTAINER_NAME="earl-mlb-stats-loader"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

# Build a minimal image with just what we need
build_loader_image() {
    local dockerfile_content='FROM python:3.12-slim
WORKDIR /app

# Only install essential packages (no torch/xgboost/etc)
COPY backend/requirements.txt .
RUN grep -v "sentence-transformers\|xgboost\|scikit-learn\|torch\|pyarrow\|feedparser" requirements.txt > /tmp/requirements-minimal.txt \
    && echo "httpx" >> /tmp/requirements-minimal.txt \
    && pip install --no-cache-dir -r /tmp/requirements-minimal.txt

COPY backend/ .
ENV PYTHONPATH=/app'

    echo "$dockerfile_content" | docker build -t earl-mlb-stats-loader -f- .
}

case "${1:-start}" in
    start)
        echo "Building MLB stats loader image (minimal)..."
        build_loader_image

        echo "Starting MLB stats loader..."
        docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
        docker run -d \
            --name "$CONTAINER_NAME" \
            --network host \
            -v "$LOG_DIR:/app/logs" \
            --restart no \
            earl-mlb-stats-loader \
            python -m app.ingestion.mlb_stats 2>&1

        echo "Started container: $CONTAINER_NAME"
        echo "Monitor with: $0 logs"
        echo "Check status: $0 status"
        ;;

    status)
        docker ps --filter "name=$CONTAINER_NAME" --format "{{.Status}}" 2>/dev/null || echo "Not running"
        docker logs --tail 20 "$CONTAINER_NAME" 2>/dev/null || echo "Container not found"
        ;;

    logs)
        shift
        docker logs "$CONTAINER_NAME" ${@:---tail 100 -f} 2>&1
        ;;

    stop)
        echo "Stopping $CONTAINER_NAME..."
        docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
        ;;

    batting-only)
        echo "Building MLB stats loader image (minimal)..."
        build_loader_image

        docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
        docker run -d \
            --name "$CONTAINER_NAME" \
            --network host \
            -v "$LOG_DIR:/app/logs" \
            --restart no \
            earl-mlb-stats-loader \
            python -c "import asyncio; from app.ingestion.mlb_stats import load_batting_only; asyncio.run(load_batting_only())" 2>&1
        echo "Started batting-only loader. Check: $0 logs"
        ;;

    games-only)
        echo "Building MLB stats loader image (minimal)..."
        build_loader_image

        docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
        docker run -d \
            --name "$CONTAINER_NAME" \
            --network host \
            -v "$LOG_DIR:/app/logs" \
            --restart no \
            earl-mlb-stats-loader \
            python -c "import asyncio; from app.ingestion.mlb_stats import load_games_only; asyncio.run(load_games_only())" 2>&1
        echo "Started games-only loader. Check: $0 logs"
        ;;

    *)
        echo "Usage: $0 {start|status|logs|stop|batting-only|games-only}"
        exit 1
        ;;
esac
