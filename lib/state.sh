#!/usr/bin/env bash
# state.sh — read/write helpers for state.json (jq-backed).
#
# Sub-commands:
#     get   <run_dir> <jq-filter>            # print field
#     set   <run_dir> <jq-filter> <json>     # set field (atomic)
#     phase <run_dir> <name>                 # convenience: set .phase
#     set-current-task <run_dir> <task_id>   # set .current_task (no hand-built jq)
#     append-pr <run_dir> <pr-json-file>     # push pr.json onto .prs[]
#     finish-pr <run_dir> <pr-json-file>     # append-pr + mark-task-done, atomically
#     set-tasks <run_dir> [tasks-json]       # set .tasks_total + .task_ids from tasks.json
#     set-tasks-total <run_dir> [tasks-json] # alias of set-tasks (back-compat)
#     mark-task-done <run_dir>               # bump tasks_done, advance index, clear current_task
#     mark-task-stuck <run_dir> <task> <file>
#     mark-task-abandoned <run_dir> <task> <file>
#     mark-human-review-needed <run_dir> <task> <file>
#     show  <run_dir>                        # pretty-print state.json
set -euo pipefail

cmd="${1:-}"
[[ -n "$cmd" ]] || { echo "usage: state.sh {get|set|phase|set-current-task|append-pr|finish-pr|set-tasks|set-tasks-total|mark-task-done|mark-task-stuck|mark-task-abandoned|mark-human-review-needed|show} <run_dir> ..." >&2; exit 2; }
shift

run_dir="${1:-}"
[[ -d "$run_dir" ]] || { echo "ERROR: run_dir not a directory: $run_dir" >&2; exit 3; }
shift

state_file="$run_dir/state.json"
[[ -f "$state_file" ]] || { echo "ERROR: $state_file missing" >&2; exit 4; }

atomic_write() {
    local tmp
    tmp="$(mktemp "${state_file}.XXXXXX")"
    cat > "$tmp"
    mv "$tmp" "$state_file"
}

case "$cmd" in
    get)
        filter="${1:-.}"
        jq -r "$filter" "$state_file"
        ;;
    set)
        filter="${1:?missing filter}"
        value="${2:?missing value}"
        jq "$filter = $value" "$state_file" | atomic_write
        ;;
    phase)
        new_phase="${1:?missing phase name}"
        jq --arg p "$new_phase" '.phase = $p' "$state_file" | atomic_write
        ;;
    set-current-task)
        task_id="${1:?missing task id}"
        jq --arg t "$task_id" '.current_task = $t' "$state_file" | atomic_write
        ;;
    append-pr)
        pr_file="${1:?missing pr.json file}"
        jq --slurpfile pr "$pr_file" '.prs += $pr' "$state_file" | atomic_write
        ;;
    finish-pr)
        # Append the PR and complete the task in one atomic write. Fully
        # idempotent on the PR number: if this PR is already recorded, do
        # nothing — so a re-issued/replayed call never double-advances the task
        # index (which would silently skip the next task).
        pr_file="${1:?missing pr.json file}"
        jq --slurpfile pr "$pr_file" '
            if any(.prs[]?; .number == $pr[0].number)
            then .
            else .prs += $pr
                | .tasks_done = (.tasks_done + 1)
                | .task_index = ((.task_index // 0) + 1)
                | .current_task = null
            end
        ' "$state_file" | atomic_write
        ;;
    set-tasks|set-tasks-total)
        tasks_file="${1:-$run_dir/tasks.json}"
        [[ -f "$tasks_file" ]] || { echo "ERROR: tasks file missing: $tasks_file" >&2; exit 6; }
        jq --slurpfile t "$tasks_file" '
            .tasks_total = ($t[0] | length)
            | .task_ids = ($t[0] | map(.id))
        ' "$state_file" | atomic_write
        ;;
    mark-task-done)
        jq '
            .tasks_done = (.tasks_done + 1)
            | .task_index = ((.task_index // 0) + 1)
            | .current_task = null
        ' "$state_file" | atomic_write
        ;;
    mark-task-stuck)
        task_id="${1:?missing task id}"
        detail_file="${2:?missing stuck detail file}"
        jq --arg task "$task_id" --slurpfile detail "$detail_file" '
            .tasks_done = (.tasks_done + 1)
            | .task_index = ((.task_index // 0) + 1)
            | .tasks_stuck = ((.tasks_stuck // 0) + 1)
            | .skips = ((.skips // []) + [{"task_id": $task, "kind": "stuck", "detail": $detail[0]}])
            | .current_task = null
        ' "$state_file" | atomic_write
        ;;
    mark-task-abandoned)
        task_id="${1:?missing task id}"
        detail_file="${2:?missing abandon detail file}"
        jq --arg task "$task_id" --slurpfile detail "$detail_file" '
            .tasks_done = (.tasks_done + 1)
            | .task_index = ((.task_index // 0) + 1)
            | .tasks_abandoned = ((.tasks_abandoned // 0) + 1)
            | .skips = ((.skips // []) + [{"task_id": $task, "kind": "abandoned", "detail": $detail[0]}])
            | .current_task = null
        ' "$state_file" | atomic_write
        ;;
    mark-human-review-needed)
        task_id="${1:?missing task id}"
        detail_file="${2:?missing human-review detail file}"
        jq --arg task "$task_id" --slurpfile detail "$detail_file" '
            .tasks_done = (.tasks_done + 1)
            | .task_index = ((.task_index // 0) + 1)
            | .human_review_needed = ((.human_review_needed // 0) + 1)
            | .skips = ((.skips // []) + [{"task_id": $task, "kind": "human-review-needed", "detail": $detail[0]}])
            | .current_task = null
        ' "$state_file" | atomic_write
        ;;
    show)
        jq . "$state_file"
        ;;
    *)
        echo "ERROR: unknown command: $cmd" >&2
        exit 5
        ;;
esac
