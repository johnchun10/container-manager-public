#!/usr/bin/env bash

TARGET_ENV="/root/target_env.sh"
SNAP_DIR="/root/tmux_resurrect"
SNAP_FILE="$SNAP_DIR/old_env.sh"

# Only source if it exists
if [[ -f "$TARGET_ENV" ]]; then
  # shellcheck source=/root/target_env.sh
  source "$TARGET_ENV"

  # If PWD is set in target_env and the directory exists, cd into it
  if [[ -n "${PWD:-}" && -d "$PWD" ]]; then
    cd "$PWD"
  fi
fi

# No output


# Dump current environment in export -p format
export -p > "$SNAP_FILE"
