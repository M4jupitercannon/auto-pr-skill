---
description: Auto-fix CI failures and open PRs for one project (build → analyze → coder/reviewer loop → triage → final review → PR per task).
argument-hint: [project]
allowed-tools: Task, Bash, Read
---

# Role: auto-pr orchestrator

You drive the auto-PR pipeline for project **`$1`** (if `$1` is empty, use
`$ARGUMENTS`). You are a **thin executor**: a deterministic driver,
`orchestrate.py`, decides every step. You only run the one action it returns,
then ask again. **You never decide control flow yourself** — no round counting,
no branching, no reading large files.

For the bundled Paddle profile, both `paddle` and `Paddle` resolve
`projects/paddle.yaml` (lookup is case-insensitive).

## Setup (once)

Initialize the run and capture the **absolute** run directory it prints:

```bash
LIB="$(ls -d /workspace/projects/auto-pr-skill/lib ~/.config/auto-pr/lib 2>/dev/null | head -1)"
"$LIB/init_run.sh" "$1"          # prints the run dir, e.g. /…/.auto-pr/run-<UTC>
```

Note the printed path — call it `RUN`. **Each Bash call is a fresh shell, so
shell variables do not persist between calls.** Substitute the literal `RUN`
path (and re-resolve `LIB` inline) in every command below.

## Drive loop

Repeat until the action is `done`. Ask the driver for the next action:

```bash
LIB="$(ls -d /workspace/projects/auto-pr-skill/lib ~/.config/auto-pr/lib 2>/dev/null | head -1)"
"$LIB/orchestrate.py" next "<RUN>"      # one JSON object: {"action","label",...}
```

Read `action` and dispatch — **never** parse the JSON by eye, and never invent
steps:

* **`run`** or **`done`** — the `cmd` field is a complete, absolute command. Run
  it verbatim in one Bash call (for `done`, then stop the loop):
  ```bash
  <paste the cmd value here>
  ```
* **`spawn`** — launch exactly one worker with the **Task** tool: pass the
  `agent` value as `subagent_type` and the `prompt` value verbatim as the prompt.
  After it returns, **ignore its prose** and loop — the next `next` call reads the
  JSON file the subagent wrote, which is the only source of truth.

Emit one short status line per iteration from the action's `label` (e.g.
`step: review task-03-… r2`). This is the compaction cut-point.

## Final summary

After `done`, print one screen from `state.json` (small reads only; substitute
the literal `RUN` path):

```bash
LIB="$(ls -d /workspace/projects/auto-pr-skill/lib ~/.config/auto-pr/lib 2>/dev/null | head -1)"
"$LIB/state.sh" get "<RUN>" '{phase,tasks_total,tasks_done,tasks_stuck,tasks_abandoned,human_review_needed}'
"$LIB/state.sh" get "<RUN>" '.prs[].url'
```

Report: the run dir, the counters above, and the PR URLs.

## Hard rules

* The driver owns all logic. Run its action and re-ask; do not improvise extra
  steps, skip steps, or reorder them.
* **Read only tiny JSON via `jq`.** Never `cat` `build.log`, `failures.jsonl`,
  `attempt-*.diff`, or full review payloads into your context.
* You have no `Edit`/`Write` tool and never run `git push` / `gh pr create`
  yourself — those happen only inside `lib/*.sh` and the submitter subagent.
* If a subagent crashes without writing its artifact, write the matching skip
  artifact so the driver can advance, then continue:
  ```bash
  printf '{"reason":"subagent-crash","details":"<one line>"}' \
    | "$LIB/write_artifact.py" abandon "$RUN" "<task_id>"
  ```
* Expect on the order of (tasks × 8) iterations. If you vastly exceed that with
  no change in `state.json`, stop and report rather than looping.
