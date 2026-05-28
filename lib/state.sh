#!/usr/bin/env bash
# state.sh — read/write helpers for state.json (jq-backed).
#
# Sub-commands:
#     get   <run_dir> <jq-filter>            # print field
#     set   <run_dir> <jq-filter> <json>     # set field (atomic)
#     phase <run_dir> <name>                 # convenience: set .phase
#     append-pr <run_dir> <pr-json-file>     # push pr.json onto .prs[]
#     set-tasks-total <run_dir> [tasks-json] # set .tasks_total from tasks.json length
#     mark-task-done <run_dir>               # bump tasks_done, clear current_task
#     mark-task-stuck <run_dir> <task> <file>
#     mark-task-abandoned <run_dir> <task> <file>
#     mark-human-review-needed <run_dir> <task> <file>
#     show  <run_dir>                        # pretty-print state.json
set -euo pipefail

cmd="${1:-}"
[[ -n "$cmd" ]] || { echo "usage: state.sh {get|set|phase|append-pr|set-tasks-total|mark-task-done|mark-task-stuck|mark-task-abandoned|mark-human-review-needed|show} <run_dir> ..." >&2; exit 2; }
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
    append-pr)
        pr_file="${1:?missing pr.json file}"
        jq --slurpfile pr "$pr_file" '.prs += $pr' "$state_file" | atomic_write
        ;;
    set-tasks-total)
        tasks_file="${1:-$run_dir/tasks.json}"
        [[ -f "$tasks_file" ]] || { echo "ERROR: tasks file missing: $tasks_file" >&2; exit 6; }
        jq --argjson n "$(jq 'length' "$tasks_file")" '.tasks_total = $n' "$state_file" | atomic_write
        ;;
    mark-task-done)
        jq '.tasks_done = (.tasks_done + 1) | .current_task = null' "$state_file" | atomic_write
        ;;
    mark-task-stuck)
        task_id="${1:?missing task id}"
        detail_file="${2:?missing stuck detail file}"
        jq --arg task "$task_id" --slurpfile detail "$detail_file" '
            .tasks_done = (.tasks_done + 1)
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
