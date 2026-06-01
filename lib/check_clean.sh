#!/usr/bin/env bash
# check_clean.sh — verify the target repo has no pre-existing tracked changes
# before the task loop touches it. Run once per /auto-pr run.
#
# A dirty tree is a whole-run problem (every task would otherwise be abandoned
# one by one), so we detect it here and let the driver stop the run cleanly.
#
# Writes exactly one sentinel:
#     <run_dir>/clean-checked   when the tree is clean   (exit 0)
#     <run_dir>/dirty-tree.json when it is dirty          (exit 1)
#
# Usage:
#     check_clean.sh <run_dir>
set -euo pipefail

run_dir="${1:?usage: check_clean.sh <run_dir>}"
profile="$run_dir/profile.yaml"
[[ -f "$profile" ]] || { echo "ERROR: profile not found: $profile" >&2; exit 2; }

repo_path="$(awk -F': *' '$1=="repo_path"{sub(/^[^:]+: */,"");gsub(/^"|"$/,"");print;exit}' "$profile")"
if [[ ! -d "$repo_path/.git" ]]; then
    # Stop the run with a recorded reason instead of exiting with no sentinel
    # (which would make the driver re-emit check-clean forever).
    cat > "$run_dir/dirty-tree.json" <<JSON
{
  "reason": "not-a-git-repo",
  "details": "repo_path is not a git repository; cannot operate",
  "repo_path": "$repo_path"
}
JSON
    echo "ERROR: not a git repo: $repo_path (wrote $run_dir/dirty-tree.json)" >&2
    exit 1
fi

cd "$repo_path"
dirty="$(git status --porcelain -- . ':(exclude).auto-pr' ':(exclude).opencode' ':(exclude).claude')"

if [[ -n "$dirty" ]]; then
    files="$(printf '%s\n' "$dirty" | sed 's/^...//' | jq -R . | jq -s .)"
    cat > "$run_dir/dirty-tree.json" <<JSON
{
  "reason": "dirty-working-tree",
  "details": "repo had tracked uncommitted changes before the run; refusing to fix anything",
  "repo_path": "$repo_path",
  "dirty_paths": $files
}
JSON
    echo "[check_clean] dirty tree at $repo_path; wrote $run_dir/dirty-tree.json" >&2
    exit 1
fi

: > "$run_dir/clean-checked"
echo "[check_clean] working tree clean at $repo_path" >&2
