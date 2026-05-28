# auto-pr-skill

A multi-agent [OpenCode](https://opencode.ai) skill that turns one command —
`/auto-pr <project>` — into a build → analyze → fix → review → triage → PR
pipeline. Designed for repos with large CI surfaces where a single failing
build produces many independent failures (Paddle on ROCm MI300X is the
reference profile).

## What it does

```
/auto-pr paddle
   |
   v
auto-pr-orchestrator  (one primary agent, runs the state machine)
   |
   |-- spawns auto-pr-error-analyzer    -> tasks.json (ranked, ≤30KB each)
   |
   |-- per task, in priority order:
   |     auto-pr-coder      (round 1)        -> attempt-1.diff
   |     auto-pr-reviewer   (round 1)        -> review-1.json
   |     [if not approved]  coder/reviewer loop, max N rounds
   |     auto-pr-triage                       -> triage.json
   |     auto-pr-final-reviewer              -> final-review.json
   |     auto-pr-pr-submitter (gh pr create)  -> pr.json
   |     [needs human review]                  -> human-review-needed.json
   |
   v
state.json + per-task md/json files under <repo>/.auto-pr/run-<UTC>/
```

Profile lookup is case-insensitive and also matches the profile `name:` field,
so `/auto-pr Paddle` resolves the bundled `projects/paddle.yaml`.

Each subagent runs in its own fresh OpenCode session, so each starts with a
near-empty context window (~1M usable). Communication between agents happens
through small files on disk; the orchestrator only ever reads tiny JSON
status fields, which keeps its own context comfortably under 50KB even on a
14-task run.

## Install

Prerequisites: `git`, `python3`, `jq`, `gh`, plus [opencode](https://opencode.ai/docs/install/).

```bash
git clone <this-repo> /workspace/projects/auto-pr-skill
cd /workspace/projects/auto-pr-skill
./install.sh                                       # global only
./install.sh --project /workspace/projects/Paddle  # global + project-scoped
```

`install.sh` symlinks the OpenCode skill, slash command, agents, and shared
`lib/` / `templates/` / `references/` into:

| Location | Used by |
| --- | --- |
| `~/.config/opencode/skills/auto-pr-skill/SKILL.md` | OpenCode skill discovery |
| `~/.config/opencode/commands/auto-pr.md` | OpenCode `/auto-pr` |
| `~/.config/opencode/agents/auto-pr-*.md` | OpenCode subagent registry |
| `~/.config/auto-pr/projects/*.yaml`      | profile resolver |
| `~/.config/auto-pr/{lib,templates}`      | helper scripts (read by agents) |
| `~/.config/auto-pr/references`           | shared review references, including AMD ROCm stack notes |
| `<repo>/.opencode/skills/auto-pr-skill/SKILL.md` | project-local skill discovery |
| `<repo>/.opencode/{commands,agents}/`    | per-project overrides (when `--project`) |
| `<repo>/.auto-pr/{profile.yaml,references}` | resolved profile and project-local references |

Symlinks (not copies), so editing the repo updates the live install.

To remove: `./install.sh --uninstall [--project <repo>]`.

## Use

From inside a repo whose profile is installed:

```
opencode
> /auto-pr paddle
```

`/auto-pr Paddle` works too.

Or from anywhere (global install):

```
> /auto-pr paddle
```

The orchestrator walks the pipeline, prints a phase-level status line at
each cut-point, and emits a final summary with PR URLs.

## Adding a new project

1. Copy `projects/paddle.yaml` to `projects/<your-project>.yaml`.
2. Edit:
   * `name` — must equal the slash-command argument (`/auto-pr <name>`)
   * `repo_path` — absolute path to the working tree
   * `build_cmd` + `build_args` — script that builds and runs CI; failures
     should land in either its stdout or in files under `log_glob`. The Paddle
     profile uses the bundled `projects/paddle/ci-rocm-mi300x.sh`.
   * `log_glob` — pattern (relative to `repo_path`) that finds the failure
     log; the analyzer picks the freshest match
   * `pr_skill_path` — path to your project's PR-creation SKILL.md (the
     submitter follows it for title/body/labels). If empty, the submitter
     falls back to `templates/pr_body.md.tmpl`.
   * `human_review_paths` — directory prefixes that always require human
     review even after auto-approval
   * `auto_submit_human_needed` — defaults to `false`; human-needed tasks are
     recorded and skipped unless this is set to `true`
   * `human_review_label` — label to add when human-needed PR submission is
     explicitly enabled; missing labels are recorded but do not fail the PR
   * `base_branch`, `branch_prefix`, `push_remote` — git/gh plumbing
3. Re-run `./install.sh [--project ...]`.

## Run artifacts

Every run lives under `<repo_path>/.auto-pr/run-<UTC>/`:

```
state.json                 # phase, counters, list of created PRs
profile.yaml               # snapshot of the profile in effect
build.log                  # captured build/test output
build.exit                 # build exit code
failures.jsonl             # one record per ctest failure (parser output)
tasks.json                 # ranked tasks, ≤max_tasks_per_run entries
tasks/<task-id>/
    task.md                # compact brief (≤~30KB)
    branch                 # task branch name
    attempt-1.diff
    review-1.json
    attempt-2.diff         # if reviewer requested changes
    review-2.json
    ...
    triage.json
    final-review.json     # final automated reviewer verdict before PR submission
    pr_title.txt
    pr_body.md
    pr.json                # final PR url + number
    human-review-needed.json # if triage requires human review and auto-submit is off
    stuck.json             # if max review rounds were exhausted
    abandon.json           # if coder judged scope too large
latest -> run-<UTC>/       # convenience symlink
```

This layout doubles as a debug trail. To replay, just `rm -rf .auto-pr/run-*`
and run `/auto-pr` again.

## Schemas

JSON Schemas (Draft 2020-12) live in `templates/`:

* [`tasks.schema.json`](templates/tasks.schema.json)
* [`review.schema.json`](templates/review.schema.json)
* [`final-review.schema.json`](templates/final-review.schema.json)
* [`triage.schema.json`](templates/triage.schema.json)

## Library scripts (deterministic, no LLM)

| Script | Purpose |
| --- | --- |
| `lib/parse_ctest.py` | Streams a CTest log, emits one JSON record per failed/timed-out test. |
| `lib/classify_errors.py` | Groups failures by fingerprint, ranks them, writes `tasks.json` + `task.md` files. |
| `lib/init_run.sh` | Creates the run directory and `state.json`. |
| `lib/run_build.sh` | Runs the profiled build command, tees `build.log`, and writes the real command exit code to `build.exit`. |
| `lib/state.sh` | jq-backed read/write helpers for `state.json`. |
| `lib/validate_json.py` | Stdlib validation for task, review, final-review, and triage JSON artifacts. |
| `lib/write_artifact.py` | Validated writer for review, final-review, triage, PR text, stuck, abandon, and human-review skip artifacts. |
| `lib/submit_pr.sh` | Fail-closed pre-push check → `git push` → `gh pr create`, then best-effort labels. |

Keeping deterministic work in scripts (not in agent prompts) keeps the run
reproducible and free of LLM-flake.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ERROR: no profile found for '<name>'` | profile not installed or name mismatch | `ls ~/.config/auto-pr/projects/` and re-run `install.sh` |
| Orchestrator loops forever on one task | reviewer never approves | check `review-N.json` blocking entries; consider raising `max_review_rounds` or marking the human path |
| Empty `tasks.json` despite failing CI | parser regex didn't catch the format | run `lib/parse_ctest.py --summary <log>` manually; tweak `SUMMARY_RE` if needed |
| `gh pr create` fails with auth | `gh auth login` not done in this environment | run `gh auth status` then `gh auth login` |
| Pre-push check (`prek`) keeps modifying files | normal for Paddle; submitter auto-commits the fixes | nothing — it's expected |

## Design notes

* **One primary agent + five+ subagents** — the orchestrator owns state,
  every other agent is one-shot. Subagents never call each other directly;
  the orchestrator is the only invoker.
* **Files-not-prompts** — every multi-KB payload (logs, diffs, reviews) lives
  on disk. Subagents read paths, not pasted contents.
* **Schema-pinned outputs** — `review-N.json`, `triage.json`, `tasks.json`
  are validated by stdlib helpers so the orchestrator's branch logic never
  depends on prose.
* **Branch per task** — each task gets `auto-pr/<task-id>` off the configured
  base branch; the submitter refuses to submit from the base branch and uses
  noninteractive `git`/`gh` plumbing only.
* **Coder subsumes code-fixer** — `auto-pr-coder` handles both first attempts
  and reviewer-requested fixes.
* **Final PR review gate** — after triage, `auto-pr-final-reviewer` performs a
  last read-only pass before submission. It can approve, block automatic
  submission for human review, or send the task back to the coder within the
  same `max_review_rounds` budget.
* **Cross-platform / AMD ROCm awareness** — reviewers and coders consult
  [`references/amd-rocm-stack.md`](references/amd-rocm-stack.md) for ROCm stack
  layers, HIP/CUDA parity checks, AMD GPU targets, Paddle ROCm paths, and links
  to upstream AMD documentation. ROCm fixes must preserve CUDA/NVIDIA, CPU,
  XPU, and generic GPU behavior unless the task explicitly proves otherwise.
* **Project PR conventions are preserved** — the submitter delegates the
  title/body to the project's existing PR skill (`paddle-pull-request` for
  Paddle), so any upstream change to that skill propagates automatically.

## License

MIT.
