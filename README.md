# auto-pr-skill

A multi-agent skill for [OpenCode](https://opencode.ai) **and**
[Claude Code](https://www.claude.com/product/claude-code) that turns one
command — `/auto-pr <project>` — into a build → analyze → fix → review →
triage → PR pipeline. Designed for repos with large CI surfaces where a single
failing build produces many independent failures (Paddle on ROCm MI300X is the
reference profile).

Both front-ends share the same deterministic `lib/`, `templates/`,
`references/`, and project profiles. Only the agent/command/skill wrappers
differ:

| Runtime | Front-end files | Installer |
| --- | --- | --- |
| OpenCode | `opencode/` + root `SKILL.md` | `./install.sh` |
| Claude Code | `claude/` | `./install-claude.sh` |

## What it does

```
/auto-pr paddle
   |
   v
orchestrator  (thin executor; control flow lives in lib/orchestrate.py)
   |  loop: action = orchestrate.py next <run>;  run it;  repeat
   |
   |-- spawns auto-pr-error-analyzer    -> tasks.json (ranked, ≤30KB each)
   |
   |-- per task, in priority order:
   |     auto-pr-coder      (round 1)        -> attempt-1.diff
   |     auto-pr-reviewer   (round 1)        -> review-1.json
   |     [if not approved]  coder/reviewer loop, max N rounds
   |     auto-pr-triage                       -> triage.json
   |     auto-pr-final-reviewer              -> final-review.json
   |     [optional verify_cmd gate]          -> verify-N.json
   |     auto-pr-pr-submitter (gh pr create)  -> pr.json
   |     [needs human review]                  -> human-review-needed.json
   |
   v
state.json + per-task md/json files under <repo>/.auto-pr/run-<UTC>/
```

The orchestrator never decides control flow itself. `lib/orchestrate.py next`
reads `state.json` + the artifacts on disk and returns the single next action
(run a deterministic command, or spawn one subagent). The model just executes it
and asks again, so round counting, the final-review re-entry, and the
stuck/abandon/human-review outcomes are testable Python — not model prose — and
the run is resumable after a crash.

Profile lookup is case-insensitive and also matches the profile `name:` field,
so `/auto-pr Paddle` resolves the bundled `projects/paddle.yaml`.

Each subagent runs in its own fresh OpenCode session, so each starts with a
near-empty context window (~1M usable). Communication between agents happens
through small files on disk; the orchestrator only ever reads tiny JSON
status fields, which keeps its own context comfortably under 50KB even on a
14-task run.

## Install

Common prerequisites: `git`, `python3`, `jq`, `gh`.

### OpenCode

Also requires [opencode](https://opencode.ai/docs/install/).

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

### Claude Code

Use `install-claude.sh`. It installs the Claude front-end and, by default,
performs the [DeepSeek → Claude Code migration](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code)
by writing a sourceable env file that points Claude Code at DeepSeek models.

```bash
cd /workspace/projects/auto-pr-skill
./install-claude.sh                                  # global + DeepSeek env file
./install-claude.sh --project /workspace/projects/Paddle
./install-claude.sh --install-cli --api-key sk-...   # also npm-install the CLI, set the key
./install-claude.sh --no-deepseek                    # skip the DeepSeek env file
./install-claude.sh --persist                        # source the env file from your shell rc
```

It symlinks into:

| Location | Used by |
| --- | --- |
| `~/.claude/skills/auto-pr-skill/SKILL.md` | Claude Code skill discovery |
| `~/.claude/commands/auto-pr.md` | Claude Code `/auto-pr` (the orchestrator) |
| `~/.claude/agents/auto-pr-*.md` | Claude Code subagent registry |
| `~/.claude/settings.json` | guardrails **merged** in (deny push/PR/network) |
| `~/.config/auto-pr/{projects,lib,templates,references}` | shared profiles + helpers (same as OpenCode) |
| `~/.config/auto-pr/claude-deepseek.env` | DeepSeek env (created unless `--no-deepseek`) |
| `<repo>/.claude/{skills,commands,agents,settings.json}` | per-project install (when `--project`) |
| `<repo>/.auto-pr/{profile.yaml,references}` | resolved profile and project-local references |

The DeepSeek env maps Claude model names to DeepSeek models — `opus`/`sonnet`
→ `deepseek-v4-pro[1m]` and `haiku` → `deepseek-v4-flash`. Each worker subagent
then **pins its own model** via the `model:` field in its frontmatter, so the
reasoning-heavy workers get the strong model and the mechanical ones stay cheap:

| Subagent | `model:` | DeepSeek model |
| --- | --- | --- |
| `auto-pr-coder`, `auto-pr-reviewer`, `auto-pr-final-reviewer` | `opus` | `deepseek-v4-pro[1m]` |
| `auto-pr-error-analyzer`, `auto-pr-triage`, `auto-pr-pr-submitter` | `haiku` | `deepseek-v4-flash` |

The orchestrator (main session) uses `deepseek-v4-pro[1m]`. `CLAUDE_CODE_SUBAGENT_MODEL`
remains the fallback for any subagent without an explicit `model:`. Provide your
key with `--api-key` or `$DEEPSEEK_API_KEY`; the env file is written `chmod 600`.

The installer also **merges** read-only guardrails into Claude Code's
`settings.json` (global and, with `--project`, repo-scoped) from
[`claude/settings.json`](claude/settings.json): a permission `deny` list that
stops the model — and the read-only worker subagents — from running `git push`,
`gh pr create/merge/edit`, or network fetches directly. Real pushes happen only
inside `lib/submit_pr.sh`, which the tool-level deny rules do not intercept. The
merge unions into any existing settings and never clobbers them.

To use it:

```bash
source ~/.config/auto-pr/claude-deepseek.env   # or rely on --persist
claude
> /auto-pr paddle
```

To remove: `./install-claude.sh --uninstall [--project <repo>]` (also removes
the DeepSeek env file unless `--no-deepseek`).

> **Architecture note.** On Claude Code the `/auto-pr` slash command runs in
> the **main session** and acts as the orchestrator (the OpenCode build uses a
> dedicated `auto-pr-orchestrator` primary agent). The main session spawns the
> six `auto-pr-*` worker subagents via the Task tool; subagents do not spawn
> other subagents. Read-only workers carry no `Edit`/`Write` tool and emit
> their JSON only through `lib/write_artifact.py`.

## Use

From inside a repo whose profile is installed:

```
opencode          # or: claude
> /auto-pr paddle
```

`/auto-pr Paddle` works too. It also works from anywhere with a global install.

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
   * `verify_cmd` *(optional)* — a command that re-runs a task's own tests
     before submission, templated with `{tests}` (regex-joined test names) and
     `{repo}`. Non-zero exit marks the task stuck instead of opening a PR. Leave
     unset to skip verification (default).
   * `base_branch`, `branch_prefix`, `push_remote` — git/gh plumbing
3. Re-run `./install.sh [--project ...]`.

## Run artifacts

Every run lives under `<repo_path>/.auto-pr/run-<UTC>/`:

```
state.json                 # phase, counters, task_ids/task_index, list of created PRs
profile.yaml               # snapshot of the profile in effect
build.log                  # captured build/test output
build.exit                 # build exit code (always written)
clean-checked              # sentinel: working tree was clean at task-loop start
dirty-tree.json            # written instead if the tree was dirty (ends the run)
failures.jsonl             # one record per ctest failure (parser output)
tasks.json                 # ranked tasks, ≤max_tasks_per_run entries
tasks/<task-id>/
    task.md                # compact brief (≤~30KB)
    branch                 # task branch name
    attempt-1.diff
    review-1.json
    triage-1.json          # per-round triage verdict (round N)
    final-review-1.json    # per-round final automated reviewer verdict (round N)
    verify-1.json          # per-round, if profile sets verify_cmd: {passed, exit}
    attempt-2.diff         # if reviewer/final-reviewer requested changes
    review-2.json
    triage-2.json
    final-review-2.json
    ...
    pr_title.txt
    pr_body.md
    pr.json                # final PR url + number
    pr-error.json          # if a submit attempt failed without producing pr.json
    human-review-needed.json # if a gate requires human review and auto-submit is off
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
| `lib/orchestrate.py` | The driver. `next <run>` returns the single next action (run/spawn/done); owns all branching, round counting, and outcomes. |
| `lib/parse_ctest.py` | Streams a CTest log, emits one JSON record per failed/timed-out test. |
| `lib/classify_errors.py` | Groups failures by fingerprint, ranks them, writes `tasks.json` + `task.md` files. |
| `lib/init_run.sh` | Creates the run directory and `state.json` (snapshots round/submit knobs). |
| `lib/run_build.sh` | Runs the profiled build command, tees `build.log`, and always writes the real exit code to `build.exit` (even on early failure). |
| `lib/profile_get.sh` | Single source of truth for reading one scalar key from `profile.yaml` (no agent hand-parses YAML). |
| `lib/check_clean.sh` | One-time working-tree cleanliness gate; writes `clean-checked` or `dirty-tree.json`. |
| `lib/verify_task.sh` | Optional, profile-gated re-run of a task's own tests before submit; writes `verify-N.json`. |
| `lib/state.sh` | jq-backed read/write helpers for `state.json` (incl. `set-tasks`, `finish-pr`, index-advancing marks). |
| `lib/advance_task.py` | Atomic, idempotent terminal transition (`stuck`/`human-review-needed`/`abandoned`): writes the skip artifact and bumps the counters/index in one `state.json` write; a re-issued call no-ops (dedupes by `task_id`). |
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

* **Deterministic driver** — control flow lives in `lib/orchestrate.py`, not in
  the model. The orchestrator is a thin executor (run the next action, repeat),
  which keeps a weaker model on-rails and makes the pipeline resumable and
  unit-testable.
* **One orchestrator + six subagents** — the orchestrator owns state, every
  other agent is one-shot. Subagents never call each other; the orchestrator is
  the only invoker.
* **Model routing** — reasoning-heavy workers (coder, reviewer, final reviewer)
  pin the strong model; mechanical workers (analyzer, triage, submitter) pin the
  cheap one (see the model table above).
* **Enforced read-only workers** — OpenCode encodes per-agent `permission`
  blocks; Claude Code gets a merged `settings.json` deny list. Discipline is
  enforced by config, not just prose.
* **Trusted profile boundary** — `verify_cmd` and `pre_push_check` are run as
  shell straight from the project profile, and `lib/submit_pr.sh` is the single
  intentional place that runs `git push` / `gh pr create`. Profiles are trusted
  input; the read-only guardrails deliberately do not intercept the submitter.
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
