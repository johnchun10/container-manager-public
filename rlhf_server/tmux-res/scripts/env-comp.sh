#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 old_env.sh new_env.sh" >&2
  exit 1
fi

OLD_ENV=$1
NEW_ENV=$2

for f in "$OLD_ENV" "$NEW_ENV"; do
  if [[ ! -f "$f" ]]; then
    echo "File not found: $f" >&2
    exit 1
  fi
done

declare -A OLD NEW

# Load an env snapshot file into a clean environment and print key=value\0
load_env_file() {
  local file=$1
  env -i bash -c '
    set -a                     # auto-export variables defined in the file
    source "$1" >/dev/null 2>&1
    set +a
    env -0                     # dump environment as NUL-separated key=value
  ' _ "$file"
}

# Fill OLD[]
while IFS='=' read -r -d '' name value; do
  OLD["$name"]="$value"
done < <(load_env_file "$OLD_ENV")

# Fill NEW[]
while IFS='=' read -r -d '' name value; do
  NEW["$name"]="$value"
done < <(load_env_file "$NEW_ENV")

changed=0

echo "=== Changed variables (same name, different value) ==="
for name in "${!OLD[@]}"; do
  if [[ -v 'NEW[$name]' ]]; then
    if [[ "${OLD[$name]}" != "${NEW[$name]}" ]]; then
      printf '%s\n  old: %q\n  new: %q\n\n' \
        "$name" "${OLD[$name]}" "${NEW[$name]}"
      ((changed++))
    fi
  fi
done

echo "=== Removed variables (present in old, missing in new) ==="
for name in "${!OLD[@]}"; do
  if [[ ! -v 'NEW[$name]' ]]; then
    printf '%s\n  old: %q\n\n' "$name" "${OLD[$name]}"
    ((changed++))
  fi
done

echo "=== Added variables (present in new, missing in old) ==="
for name in "${!NEW[@]}"; do
  if [[ ! -v 'OLD[$name]' ]]; then
    printf '%s\n  new: %q\n\n' "$name" "${NEW[$name]}"
    ((changed++))
  fi
done

echo "Total differences: $changed"
