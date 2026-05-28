#!/usr/bin/env bash
# submit_pr.sh — push a task's branch, optionally run pre-push checks, and
# create a PR via `gh`. The body is read from a file the submitter agent
# already prepared; this script is the deterministic git/gh plumbing.
#
# Usage:
#     lib/submit_pr.sh <run_dir> <task_id>
#
# Inputs (read from disk):
#     <run_dir>/profile.yaml
#     <run_dir>/tasks/<task_id>/branch              # plain text: branch name
#     <run_dir>/tasks/<task_id>/pr_title.txt
#     <run_dir>/tasks/<task_id>/pr_body.md
#     <run_dir>/tasks/<task_id>/triage.json
#
# Outputs:
#     <run_dir>/tasks/<task_id>/pr.json   # {url, number, branch, needs_human}
set -euo pipefail

run_dir="${1:?usage: submit_pr.sh <run_dir> <task_id>}"
task_id="${2:?usage: submit_pr.sh <run_dir> <task_id>}"

profile="$run_dir/profile.yaml"
task_dir="$run_dir/tasks/$task_id"

[[ -f "$profile" ]] || { echo "ERROR: profile not found: $profile" >&2; exit 2; }
[[ -d "$task_dir" ]] || { echo "ERROR: task dir not found: $task_dir" >&2; exit 3; }

yaml_get() {
    awk -v key="$1" -F': *' '
        $1 == key { sub(/^[^:]+: */, ""); gsub(/^"|"$/, ""); print; exit }
    ' "$profile"
}

git_status_without_artifacts() {
    git status --porcelain -- . ':(exclude).auto-pr' ':(exclude).opencode'
}

git_add_without_artifacts() {
    git add -A -- . ':(exclude).auto-pr' ':(exclude).opencode'
}

repo_path="$(yaml_get repo_path)"
push_remote="$(yaml_get push_remote)"
pre_push_check="$(yaml_get pre_push_check)"
base_branch="$(yaml_get base_branch)"
auto_submit_human_needed="$(yaml_get auto_submit_human_needed)"
human_review_label="$(yaml_get human_review_label)"

[[ -n "$auto_submit_human_needed" ]] || auto_submit_human_needed="false"
[[ -n "$human_review_label" ]] || human_review_label="human-review-needed"

[[ -d "$repo_path/.git" ]] || { echo "ERROR: not a git repo: $repo_path" >&2; exit 4; }

branch_file="$task_dir/branch"
title_file="$task_dir/pr_title.txt"
body_file="$task_dir/pr_body.md"
triage_file="$task_dir/triage.json"

for f in "$branch_file" "$title_file" "$body_file" "$triage_file"; do
    [[ -f "$f" ]] || { echo "ERROR: required input missing: $f" >&2; exit 5; }
done

branch="$(<"$branch_file")"
title="$(<"$title_file")"
needs_human="$(jq -r '.needs_human' "$triage_file")"

"$(dirname "$0")/validate_json.py" triage "$triage_file" >/dev/null

if [[ "$needs_human" == "true" && "$auto_submit_human_needed" != "true" ]]; then
    cat > "$task_dir/human-review-needed.json" <<JSON
{
  "reason": "triage-needs-human",
  "details": "triage.json set needs_human=true; auto_submit_human_needed is not enabled",
  "task_id": "$task_id"
}
JSON
    echo "[submit_pr] skipping PR because triage requires human review: $task_dir/human-review-needed.json" >&2
    exit 0
fi

cd "$repo_path"

# Ensure we're on the task branch (the coder agent should have left us here).
current="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$current" != "$branch" ]]; then
    echo "[submit_pr] checking out $branch" >&2
    git checkout "$branch"
fi

current="$(git rev-parse --abbrev-ref HEAD)"
if [[ -n "$base_branch" && ( "$branch" == "$base_branch" || "$current" == "$base_branch" ) ]]; then
    echo "ERROR: refusing to submit from protected/base branch: $base_branch" >&2
    exit 6
fi

# Optional pre-push hygiene check (e.g., prek for Paddle).
if [[ -n "$pre_push_check" ]]; then
    pre_push_passed=0
    for attempt in 1 2 3; do
        echo "[submit_pr] running pre-push check (attempt $attempt): $pre_push_check" >&2
        set +e
        eval "$pre_push_check"
        check_status=$?
        set -e

        dirty="$(git_status_without_artifacts)"
        if [[ -n "$dirty" ]]; then
            echo "[submit_pr] pre-push check modified files; committing fixes" >&2
            git_add_without_artifacts
            if git diff --cached --quiet; then
                echo "ERROR: pre-push check changed files but nothing safe to commit" >&2
                exit 7
            fi
            git commit -m "chore: apply $(echo "$pre_push_check" | awk '{print $1}') fixes"
            continue
        fi

        if [[ "$check_status" -eq 0 ]]; then
            pre_push_passed=1
            break
        fi

        echo "ERROR: pre-push check failed without auto-fixable changes" >&2
        exit 7
    done

    if [[ "$pre_push_passed" -ne 1 ]]; then
        echo "ERROR: pre-push check did not pass after auto-fix attempts" >&2
        exit 8
    fi
fi

# Push (idempotent).
echo "[submit_pr] pushing $branch -> $push_remote" >&2
git push -u "$push_remote" "$branch"

# Build gh args. Labels are added after creation so missing labels do not make
# an otherwise valid PR submission fail.
gh_args=(pr create --title "$title" --body-file "$body_file" --head "$branch")
[[ -n "$base_branch" ]] && gh_args+=(--base "$base_branch")

# Extra labels from profile (gh_extra_labels: [foo, bar])
extra_labels="$(awk '
    /^gh_extra_labels:/ { found=1; next }
    found && /^[ ]*-/ { sub(/^[ ]*-[ ]*/, ""); print }
    found && /^[^ ]/ { exit }
' "$profile")"

echo "[submit_pr] gh ${gh_args[*]}" >&2
pr_url="$(gh "${gh_args[@]}")"
pr_number="$(basename "$pr_url")"

label_failures=()
if [[ "$needs_human" == "true" ]]; then
    if ! gh pr edit "$pr_url" --add-label "$human_review_label"; then
        echo "[submit_pr] warning: could not add label '$human_review_label'" >&2
        label_failures+=("$human_review_label")
    fi
fi
while IFS= read -r lbl; do
    [[ -n "$lbl" ]] || continue
    if ! gh pr edit "$pr_url" --add-label "$lbl"; then
        echo "[submit_pr] warning: could not add label '$lbl'" >&2
        label_failures+=("$lbl")
    fi
done <<<"$extra_labels"

cat > "$task_dir/pr.json" <<JSON
{
  "url": "$pr_url",
  "number": $pr_number,
  "branch": "$branch",
  "needs_human": $needs_human,
  "label_failures": $(if (( ${#label_failures[@]} )); then printf '%s\n' "${label_failures[@]}" | jq -R . | jq -s .; else printf '[]'; fi),
  "task_id": "$task_id"
}
JSON

echo "[submit_pr] PR created: $pr_url" >&2
cat "$task_dir/pr.json"
