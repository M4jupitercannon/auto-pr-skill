---
name: auto-pr-skill
description: Run a multi-agent Claude Code auto-PR workflow that builds a project, classifies CI failures into compact fix tasks, loops coder and reviewer subagents, triages and final-reviews human-review risk, and submits safe PRs. Use when a user wants Claude Code to turn a project CI run into small, reviewable auto-fix PRs via /auto-pr <project>.
license: MIT
---

# auto-pr-skill (Claude Code)

Use this skill when a user wants Claude Code to turn a project CI run into
small, reviewable auto-fix PRs.

The installed slash command is:

```text
/auto-pr <project>
```

For the bundled Paddle profile, both `/auto-pr paddle` and `/auto-pr Paddle`
resolve `projects/paddle.yaml`.

## Architecture on Claude Code

* The `/auto-pr` **slash command** runs in the main session and acts as the
  orchestrator. It is a thin executor of `lib/orchestrate.py` (which decides
  every step); it owns all state and is the only actor that spawns subagents.
* Six **worker subagents** live in `.claude/agents/` (also installed to
  `~/.claude/agents/`): `auto-pr-error-analyzer`, `auto-pr-coder`,
  `auto-pr-reviewer`, `auto-pr-triage`, `auto-pr-final-reviewer`,
  `auto-pr-pr-submitter`. The orchestrator spawns each via the Task tool.
* Subagents start with a blank context and communicate only through small
  `md`/`json` artifacts under `<repo>/.auto-pr/run-<UTC>/`.

The pipeline is:

1. Initialize a run directory under `<repo>/.auto-pr/run-<UTC>/`.
2. Run the profiled build via `lib/run_build.sh`.
3. Parse and classify CTest/build failures into compact tasks.
4. For each task, run coder/reviewer rounds (the coder makes the revisions too),
   triage, final review, then submit or record a human-review-needed skip.

Use the deterministic helpers in `lib/` for build execution, state updates,
validation, artifact writes, and PR submission. The same `lib/`, `templates/`,
and `references/` are shared with the OpenCode variant; only the
agent/command/skill front-ends differ.
