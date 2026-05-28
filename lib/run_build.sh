#!/usr/bin/env bash
# run_build.sh — run the profiled build command and persist build.log/build.exit.
#
# Usage:
#     lib/run_build.sh <run_dir>
set -euo pipefail

run_dir="${1:?usage: run_build.sh <run_dir>}"
profile="$run_dir/profile.yaml"

[[ -f "$profile" ]] || { echo "ERROR: profile not found: $profile" >&2; exit 2; }

yaml_get() {
    awk -v key="$1" -F': *' '
        $1 == key { sub(/^[^:]+: */, ""); gsub(/^"|"$/, ""); print; exit }
    ' "$profile"
}

repo_path="$(yaml_get repo_path)"
build_cmd="$(yaml_get build_cmd)"
build_args="$(yaml_get build_args)"

[[ -d "$repo_path" ]] || { echo "ERROR: repo_path does not exist: $repo_path" >&2; exit 3; }
[[ -n "$build_cmd" ]] || { echo "ERROR: profile missing build_cmd" >&2; exit 4; }
[[ -x "$build_cmd" ]] || { echo "ERROR: build_cmd is not executable: $build_cmd" >&2; exit 5; }

args=()
if [[ -n "$build_args" ]]; then
    # Profiles intentionally use simple whitespace-separated args.
    read -r -a args <<<"$build_args"
fi

cd "$repo_path"
set +e
"$build_cmd" "${args[@]}" 2>&1 | tee "$run_dir/build.log"
exit_code="${PIPESTATUS[0]}"
set -e

printf '%s\n' "$exit_code" > "$run_dir/build.exit"
echo "[run_build] exit=$exit_code log=$run_dir/build.log" >&2
exit "$exit_code"
