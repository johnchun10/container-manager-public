#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "Usage: $0 old_env.sh new_env.sh target_env.sh" >&2
  exit 1
fi

OLD_ENV=$1
NEW_ENV=$2
TARGET_ENV=$3

# Require OLD_ENV and NEW_ENV to exist
for f in "$OLD_ENV" "$NEW_ENV"; do
  if [[ ! -f "$f" ]]; then
    echo "File not found: $f" >&2
    exit 1
  fi
done

# If TARGET_ENV does not exist, create an empty file (and its directory)
if [[ ! -f "$TARGET_ENV" ]]; then
  mkdir -p "$(dirname "$TARGET_ENV")"
  : > "$TARGET_ENV"
fi

declare -A OLD NEW CHANGED ADDED REMOVED HANDLED

# Load an env snapshot file into a clean environment and print key=value\0
# Assumes the file was created with:  export -p > env.sh
load_env_file() {
  local file=$1
  env -i bash -c '
    # file contains "declare -x VAR=..." lines from `export -p`
    source "$1" >/dev/null 2>&1 || true
    env -0
  ' _ "$file"
}
is_valid_varname() {
  [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]
}

# Fill OLD[]
while IFS='=' read -r -d '' name value; do
  # Skip invalid names and SHLVL
  if [[ "$name" == "SHLVL" ]] || ! is_valid_varname "$name"; then
    continue
  fi
  OLD["$name"]="$value"
done < <(load_env_file "$OLD_ENV")

# Fill NEW[]
while IFS='=' read -r -d '' name value; do
  # Skip invalid names and SHLVL
  if [[ "$name" == "SHLVL" ]] || ! is_valid_varname "$name"; then
    continue
  fi
  NEW["$name"]="$value"
done < <(load_env_file "$NEW_ENV")


# Compute CHANGED and REMOVED
for name in "${!OLD[@]}"; do
  if [[ -v NEW["$name"] ]]; then
    if [[ "${OLD[$name]}" != "${NEW[$name]}" ]]; then
      CHANGED["$name"]=1
    fi
  else
    REMOVED["$name"]=1
  fi
done

# Compute ADDED
for name in "${!NEW[@]}"; do
  if [[ ! -v OLD["$name"] ]]; then
    ADDED["$name"]=1
  fi
done

# Helper: generate a line in `export -p` style: declare -x VAR="value"
gen_export_line() {
  local var=$1 val=$2 out

  # Skip invalid identifiers defensively
  if ! is_valid_varname "$var"; then
    return 0
  fi

  printf -v out 'declare -x %s=%q' "$var" "$val"
  printf '%s\n' "$out"
}


TMP_FILE=$(mktemp)

# Parse var name from a line, supporting:
#   declare -x VAR=...
#   export VAR=...
#   VAR=...
get_var_name() {
  local line_no_comment=$1
  # strip inline comments
  line_no_comment=${line_no_comment%%#*}

  if [[ $line_no_comment =~ ^[[:space:]]*declare[[:space:]]+-x[[:space:]]+([A-Za-z_][A-Za-z0-9_]*)= ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  elif [[ $line_no_comment =~ ^[[:space:]]*export[[:space:]]+([A-Za-z_][A-Za-z0-9_]*)= ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  elif [[ $line_no_comment =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)= ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  else
    printf '\n'
  fi
}

# Rewrite TARGET_ENV into TMP_FILE with changes applied
while IFS= read -r line || [[ -n "$line" ]]; do
  var_name=$(get_var_name "$line")

  if [[ -z "$var_name" ]]; then
    # Not a simple VAR line: keep as is (comments, functions, etc.)
    printf '%s\n' "$line" >> "$TMP_FILE"
    continue
  fi

  # Special case: never keep SHLVL in target_env
  if [[ "$var_name" == "SHLVL" ]]; then
    # Do NOT write this line, and don't track it
    continue
  fi

  # Mark this var as encountered in target
  HANDLED["$var_name"]=1

  if [[ -v REMOVED["$var_name"] ]]; then
    # Variable was removed in new env → drop this line (do not write)
    continue
  fi

  if [[ -v CHANGED["$var_name"] || -v ADDED["$var_name"] ]]; then
    # Variable changed or should be added/updated → write new value
    gen_export_line "$var_name" "${NEW[$var_name]}" >> "$TMP_FILE"
  else
    # No change for this variable → keep line as is
    printf '%s\n' "$line" >> "$TMP_FILE"
  fi
done < "$TARGET_ENV"

# Append new or changed vars that did not exist in target_env at all
for name in "${!ADDED[@]}"; do
  [[ "$name" == "SHLVL" ]] && continue
  if [[ ! -v HANDLED["$name"] ]]; then
    gen_export_line "$name" "${NEW[$name]}" >> "$TMP_FILE"
  fi
done

for name in "${!CHANGED[@]}"; do
  [[ "$name" == "SHLVL" ]] && continue
  if [[ ! -v HANDLED["$name"] ]]; then
    gen_export_line "$name" "${NEW[$name]}" >> "$TMP_FILE"
  fi
done

# Only update target if there are actual changes
if ! cmp -s "$TMP_FILE" "$TARGET_ENV"; then
  mv "$TMP_FILE" "$TARGET_ENV"
else
  rm "$TMP_FILE"
fi
