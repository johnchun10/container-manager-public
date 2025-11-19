#!/usr/bin/env bash

SNAP_DIR="/root/tmux_resurrect"
OLD_ENV="$SNAP_DIR/old_env.sh"
NEW_ENV="$SNAP_DIR/new_env.sh"
TARGET_ENV="/root/target_env.sh"
APPLY_SCRIPT="$SNAP_DIR/scripts/apply_env_delta.sh"

# Capture current environment to new_env.sh
export -p > "$NEW_ENV"

# Apply delta: old_env.sh -> new_env.sh onto target_env.sh
"$APPLY_SCRIPT" "$OLD_ENV" "$NEW_ENV" "$TARGET_ENV"
