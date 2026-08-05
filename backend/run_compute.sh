#!/usr/bin/env bash
# Dedicated COMPUTE worker: ingest/stats/writeups/admin + the task scheduler.
# For the machine that does article ingestion, stats, writeup generation,
# and predictions. Never serves the user-facing v1/mobile API.
cd "$(dirname "$0")"
export EARL_ROLE=compute
exec ./run_api.sh
