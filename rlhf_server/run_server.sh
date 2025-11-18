#!/usr/bin/env bash
set -e

# Change to the directory of this script (so relative paths work)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- Environment configuration ----
# Override any of these when calling the script, e.g.:
#   TASKS_ROOT=/custom/tasks ./run_server.sh

export TASKS_ROOT="${TASKS_ROOT:-../tasks}"
export CONTAINER_RESURRECT_PATH="${CONTAINER_RESURRECT_PATH:-/root/tmux_resurrect}"
export LOCAL_RESURRECT_PATH="${LOCAL_RESURRECT_PATH:-$SCRIPT_DIR/tmux-res}"
export FLASK_PORT="${FLASK_PORT:-9901}"

# ---- Run the server ----
uv run server.py
