#!/usr/bin/env bash
# profile_get.sh — read one scalar key from a run's profile.yaml.
#
# Single source of truth for the flat `key: value` parsing every agent needs,
# so no agent (or model) hand-rolls grep/cut/awk on YAML.
#
# Usage:
#     profile_get.sh <run_dir> <key>
#     profile_get.sh <run_dir> branch_prefix
set -euo pipefail

run_dir="${1:?usage: profile_get.sh <run_dir> <key>}"
key="${2:?usage: profile_get.sh <run_dir> <key>}"
profile="$run_dir/profile.yaml"

[[ -f "$profile" ]] || { echo "ERROR: profile not found: $profile" >&2; exit 2; }

awk -v key="$key" -F': *' '
    $1 == key { sub(/^[^:]+: */, ""); sub(/[ \t]+#.*$/, ""); gsub(/^"|"$/, ""); print; exit }
' "$profile"
