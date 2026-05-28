---
name: auto-pr-skill
description: Run a multi-agent OpenCode auto-PR workflow that builds a project, classifies CI failures into compact fix tasks, loops coder and reviewer agents, triages and final-reviews human-review risk, and submits safe PRs.
license: MIT
compatibility: opencode
metadata:
  command: /auto-pr
  reference-project: paddle
---

# auto-pr-skill

Use this skill when a user wants OpenCode to turn a project CI run into small,
reviewable auto-fix PRs.

The installed slash command is:

```text
/auto-pr <project>
```

For the bundled Paddle profile, both `/auto-pr paddle` and `/auto-pr Paddle`
resolve `projects/paddle.yaml`.

The pipeline is:

1. Initialize a run directory under `<repo>/.auto-pr/run-<UTC>/`.
2. Run the profiled build via `lib/run_build.sh`.
3. Parse and classify CTest/build failures into compact tasks.
4. For each task, run coder, reviewer, fixer loops, triage, final review, then
   submit or record a human-review-needed skip.

Keep multi-agent communication in the run directory using the documented
`md`/`json` artifacts. Use the deterministic helpers in `lib/` for build
execution, state updates, validation, artifact writes, and PR submission.
