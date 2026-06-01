#!/usr/bin/env bash
# verify_task.sh — optionally re-run a task's own failing tests to confirm the
# fix actually works before a PR is submitted. Deterministic; no LLM.
#
# Only runs when the profile sets `verify_cmd`. The command is templated with:
#     {tests}   space-joined ctest test names from tasks.json for this task
#     {repo}    repo_path
# Example profile value:
#     verify_cmd: "cd {repo}/build && ctest --output-on-failure -R '^({tests})$'"
#
# Writes <run_dir>/tasks/<task_id>/verify-<N>.json: {"passed": bool, "exit": N}
# where N is the current round (highest attempt-N.diff).
#
# Usage:
#     verify_task.sh <run_dir> <task_id>
set -euo pipefail

run_dir="${1:?usage: verify_task.sh <run_dir> <task_id>}"
task_id="${2:?usage: verify_task.sh <run_dir> <task_id>}"
profile="$run_dir/profile.yaml"
task_dir="$run_dir/tasks/$task_id"

[[ -f "$profile" ]] || { echo "ERROR: profile not found: $profile" >&2; exit 2; }
[[ -d "$task_dir" ]] || { echo "ERROR: task dir not found: $task_dir" >&2; exit 3; }

yaml_get() { awk -v key="$1" -F': *' '$1==key{sub(/^[^:]+: */,"");gsub(/^"|"$/,"");print;exit}' "$profile"; }
verify_cmd="$(yaml_get verify_cmd)"
repo_path="$(yaml_get repo_path)"

# Derive the current round N (highest attempt-N.diff) so the verify artifact is
# round-scoped like attempt-N.diff / review-N.json the driver reads.
n=0
for p in "$task_dir"/attempt-*.diff; do
    [[ -e "$p" ]] || continue
    cand="${p##*/attempt-}"; cand="${cand%.diff}"
    [[ "$cand" =~ ^[0-9]+$ ]] && (( cand > n )) && n="$cand"
done
verify_file="$task_dir/verify-$n.json"

# No verify command configured: treat as trivially passed.
if [[ -z "$verify_cmd" ]]; then
    printf '{"passed": true, "exit": 0, "skipped": true}\n' > "$verify_file"
    exit 0
fi

# Pull this task's test names from tasks.json (regex-joined with |).
tests="$(jq -r --arg id "$task_id" '
    (.[] | select(.id == $id) | .tests // []) | join("|")
' "$run_dir/tasks.json" 2>/dev/null || true)"

cmd="${verify_cmd//\{tests\}/$tests}"
cmd="${cmd//\{repo\}/$repo_path}"

echo "[verify_task] $cmd" >&2
set +e
( cd "$repo_path" && eval "$cmd" )
rc=$?
set -e

passed=false; [[ "$rc" -eq 0 ]] && passed=true
printf '{"passed": %s, "exit": %s}\n' "$passed" "$rc" > "$verify_file"
echo "[verify_task] passed=$passed exit=$rc" >&2
