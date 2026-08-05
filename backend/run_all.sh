#!/usr/bin/env bash
# Explicit ALL-role launcher (dev box default): everything + scheduler.
# Equivalent to running run_api.sh with EARL_ROLE unset (defaults to all).
cd "$(dirname "$0")"
export EARL_ROLE=all
exec ./run_api.sh
