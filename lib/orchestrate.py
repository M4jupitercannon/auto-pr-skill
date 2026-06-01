#!/usr/bin/env python3
"""
orchestrate.py — deterministic driver for the /auto-pr pipeline.

The LLM orchestrator is a *thin executor*: it calls `orchestrate.py next <run_dir>`,
performs the single action that is returned, then loops. All branching (build →
analyze → per-task code/review/triage/final-review/submit, round counting, the
final-review re-entry, and the human-review/stuck/abandon outcomes) lives here in
testable Python, not in a model prompt.

The next action is derived from `state.json` + the artifacts already on disk, so
the pipeline is **resumable**: re-running `next` after a crash continues cleanly.

Actions (one JSON object per `next` call):
  {"action":"run","cmd":"<shell>","label":"<short>"}        # run a deterministic command
  {"action":"spawn","agent":"auto-pr-...","prompt":"...","label":"..."}  # launch one subagent
  {"action":"done","cmd":"<shell>","label":"summary"}       # pipeline complete; run cmd then stop

Every terminal state transition is a single atomic, idempotent mutation as the
last step (`state.sh finish-pr` for a recorded PR; `advance_task.py` for the
stuck/human-review/abandoned outcomes), so a crash mid-action never double-counts.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Tiny, dependency-free readers
# ---------------------------------------------------------------------------
def load_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def parse_yaml_scalars(path: Path) -> dict[str, str]:
    """Flat `key: value` reader matching lib/*.sh yaml_get (no nested YAML)."""
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in line:
            continue
        if line[0] in (" ", "\t", "-"):  # nested / list item, ignore
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def latest_attempt(task_dir: Path) -> int:
    """Highest N among attempt-N.diff, or 0 if none."""
    best = 0
    for p in task_dir.glob("attempt-*.diff"):
        try:
            best = max(best, int(p.stem.split("-", 1)[1]))
        except (ValueError, IndexError):
            continue
    return best


def q(value: str) -> str:
    return '"' + str(value).replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Action emitters
# ---------------------------------------------------------------------------
def run(cmd: str, label: str) -> dict:
    return {"action": "run", "cmd": cmd, "label": label}


def spawn(agent: str, prompt: str, label: str) -> dict:
    return {"action": "spawn", "agent": agent, "prompt": prompt, "label": label}


def done(cmd: str) -> dict:
    return {"action": "done", "cmd": cmd, "label": "summary"}


def lib(script: str) -> str:
    return q(str(LIB / script))


# ---------------------------------------------------------------------------
# Subagent prompts (verbatim; placeholders already substituted)
# ---------------------------------------------------------------------------
def analyzer_prompt(run_dir: str) -> str:
    return (
        f"Run as auto-pr-error-analyzer. run_dir={run_dir}. "
        "Always ensure tasks.json exists (write [] if you cannot parse). "
        "Reply with ONLY the JSON status line."
    )


def coder_prompt(run_dir: str, tid: str, n: int) -> str:
    extra = "" if n == 1 else (
        f" Read review-{n - 1}.json and the latest final-review-{n - 1}.json "
        "(if present) and address every blocking item and concern."
    )
    return (
        f"Run as auto-pr-coder. run_dir={run_dir} task_id={tid} round={n}. "
        f"Read tasks/{tid}/task.md and the implicated source.{extra} "
        f"Make the smallest fix on the task branch, write attempt-{n}.diff. "
        "If the fix exceeds the size cap, write abandon.json instead. "
        "Reply with ONLY the JSON status line."
    )


def reviewer_prompt(run_dir: str, tid: str, n: int) -> str:
    return (
        f"Run as auto-pr-reviewer. run_dir={run_dir} task_id={tid} round={n}. "
        f"Review tasks/{tid}/attempt-{n}.diff against task.md, write review-{n}.json. "
        "Reply with ONLY the JSON status line."
    )


def triage_prompt(run_dir: str, tid: str, n: int) -> str:
    return (
        f"Run as auto-pr-triage. run_dir={run_dir} task_id={tid} round={n}. "
        f"Read review-{n}.json. Write triage-{n}.json via write_artifact.py --round {n}. "
        "Reply with ONLY the JSON status line."
    )


def final_prompt(run_dir: str, tid: str, n: int) -> str:
    return (
        f"Run as auto-pr-final-reviewer. run_dir={run_dir} task_id={tid} round={n}. "
        f"Read review-{n}.json and triage-{n}.json. "
        f"Write final-review-{n}.json via write_artifact.py --round {n}. "
        "Reply with ONLY the JSON status line."
    )


def submitter_prompt(run_dir: str, tid: str) -> str:
    return (
        f"Run as auto-pr-pr-submitter. run_dir={run_dir} task_id={tid}. "
        "Compose title/body, run submit_pr.sh, write pr.json. Reply with ONLY pr.json contents."
    )


# ---------------------------------------------------------------------------
# Terminal-transition commands (single atomic state mutation as last step)
# ---------------------------------------------------------------------------
def advance_cmd(run_dir: str, tid: str, kind: str, detail: str = "") -> str:
    """One idempotent advance_task.py call: writes <kind>.json and mutates state
    in a single atomic write (see lib/advance_task.py). kind ∈ {stuck,
    human-review-needed, abandoned}."""
    cmd = f"{lib('advance_task.py')} {q(run_dir)} {q(tid)} {q(kind)}"
    if detail:
        cmd += f" --detail {q(detail)}"
    return cmd


def load_tasks_cmd(run_dir: str) -> str:
    """Load tasks into state, but never loop: if tasks.json fails validation,
    reset it to [] (logged) so the run always reaches task-loop (0 tasks → done).

    Starts with a lib/ path so it matches the orchestrator's bash allowlist (the
    same reason every other emitted command leads with an absolute lib script)."""
    tasks = q(f"{run_dir}/tasks.json")
    reset = (
        "echo '[load-tasks] tasks.json failed validation; resetting to [] so the "
        f"run can finish' >&2; printf '[]' > {tasks}"
    )
    return (
        f"{lib('validate_json.py')} tasks {tasks} || {{ {reset}; }}; "
        f"{lib('state.sh')} set-tasks {q(run_dir)} && "
        f"{lib('state.sh')} phase {q(run_dir)} task-loop"
    )


# ---------------------------------------------------------------------------
# The decision function
# ---------------------------------------------------------------------------
def compute_next(run_dir: Path) -> dict:
    rd = str(run_dir)
    state = load_json(run_dir / "state.json") or {}
    profile = parse_yaml_scalars(run_dir / "profile.yaml")
    phase = state.get("phase", "init")

    try:
        max_rounds = int(profile.get("max_review_rounds", "3") or "3")
    except ValueError:
        max_rounds = 3
    auto_submit = profile.get("auto_submit_human_needed", "false") == "true"
    verify_cmd = profile.get("verify_cmd", "").strip()

    # 1. Build — run_build.sh always writes build.exit (even on early failure).
    if not (run_dir / "build.exit").exists():
        return run(
            f"{lib('state.sh')} phase {q(rd)} build; {lib('run_build.sh')} {q(rd)}; true",
            "build",
        )

    # 2. Analyze — analyzer always leaves a tasks.json (possibly []).
    if not (run_dir / "tasks.json").exists():
        return spawn("auto-pr-error-analyzer", analyzer_prompt(rd), "analyze")

    # 3. Load tasks into state exactly once (phase advances to task-loop).
    #    Resilient: invalid tasks.json is reset to [] so this never loops.
    if phase not in ("task-loop", "final-review", "done"):
        return run(load_tasks_cmd(rd), "load-tasks")

    # 4. One-time clean working-tree check before touching any task.
    if not (run_dir / "clean-checked").exists() and not (run_dir / "dirty-tree.json").exists():
        return run(f"{lib('check_clean.sh')} {q(rd)}; true", "check-clean")
    if (run_dir / "dirty-tree.json").exists():
        return done(f"{lib('state.sh')} phase {q(rd)} done")

    # 5. Per-task loop.
    ids = state.get("task_ids", []) or []
    idx = int(state.get("task_index", 0))
    if idx >= len(ids):
        return done(f"{lib('state.sh')} phase {q(rd)} done")

    tid = ids[idx]
    tdir = run_dir / "tasks" / tid
    tpath = f"{rd}/tasks/{tid}"

    if (tdir / "abandon.json").exists():
        return run(advance_cmd(rd, tid, "abandoned"), f"abandon {tid}")

    if state.get("current_task") != tid:
        return run(f"{lib('state.sh')} set-current-task {q(rd)} {q(tid)}", f"start {tid}")

    n = latest_attempt(tdir)
    if n == 0:
        return spawn("auto-pr-coder", coder_prompt(rd, tid, 1), f"code {tid} r1")

    if (tdir / "pr.json").exists():
        return run(f"{lib('state.sh')} finish-pr {q(rd)} {q(tpath + '/pr.json')}", f"record PR {tid}")

    # Review round N. Approved is a strict, validated verdict: a dict with
    # approved is True AND verdict == "approve". Anything else (missing/typo'd
    # verdict, inconsistent approved flag, garbage JSON) is treated as NOT
    # approved so we never branch into triage/submit on an unverified review.
    review = load_json(tdir / f"review-{n}.json")
    if review is None:
        return spawn("auto-pr-reviewer", reviewer_prompt(rd, tid, n), f"review {tid} r{n}")
    review_ok = (
        isinstance(review, dict)
        and review.get("approved") is True
        and review.get("verdict") == "approve"
    )
    if not review_ok:
        if n < max_rounds:
            return spawn("auto-pr-coder", coder_prompt(rd, tid, n + 1), f"code {tid} r{n + 1}")
        return run(advance_cmd(rd, tid, "stuck", "reviewer did not approve within max_review_rounds"),
                   f"stuck {tid}")

    # Approved → per-round triage → per-round final review.
    if not (tdir / f"triage-{n}.json").exists():
        return spawn("auto-pr-triage", triage_prompt(rd, tid, n), f"triage {tid}")
    if not (tdir / f"final-review-{n}.json").exists():
        return spawn("auto-pr-final-reviewer", final_prompt(rd, tid, n), f"final-review {tid}")

    # Validate the final verdict before branching: only "approve" may submit.
    final = load_json(tdir / f"final-review-{n}.json")
    verdict = final.get("verdict") if isinstance(final, dict) else None
    if verdict == "request_changes":
        if n < max_rounds:
            # NO deletion: the round n+1 coder reads final-review-{n}.json for the
            # concerns, and round n+1 produces fresh triage-/final-review- artifacts.
            return spawn("auto-pr-coder", coder_prompt(rd, tid, n + 1), f"code {tid} r{n + 1}")
        return run(advance_cmd(rd, tid, "stuck", "final reviewer requested changes after max_review_rounds"),
                   f"stuck {tid}")
    if verdict != "approve":
        # block, typo, missing, or non-dict/garbage JSON → safe human review.
        return run(advance_cmd(rd, tid, "human-review-needed", "final review blocked or invalid verdict"),
                   f"human-review {tid}")

    # verdict == approve → optional verification gate.
    if verify_cmd:
        if not (tdir / f"verify-{n}.json").exists():
            return run(f"{lib('verify_task.sh')} {q(rd)} {q(tid)}; true", f"verify {tid}")
        verify = load_json(tdir / f"verify-{n}.json")
        if not (isinstance(verify, dict) and verify.get("passed") is True):
            return run(advance_cmd(rd, tid, "stuck", "verification command failed for the fix"),
                       f"verify-failed {tid}")

    # Bounded submit failure: a prior submit left pr-error.json and no pr.json.
    if (tdir / "pr-error.json").exists():
        return run(advance_cmd(rd, tid, "human-review-needed", "submit failed; needs manual push"),
                   f"human-review {tid}")

    # Submit gate: triage may still require a human.
    triage = load_json(tdir / f"triage-{n}.json")
    if isinstance(triage, dict) and triage.get("needs_human") is True and not auto_submit:
        return run(advance_cmd(rd, tid, "human-review-needed", "auto_submit_human_needed is false"),
                   f"human-review {tid}")

    return spawn("auto-pr-pr-submitter", submitter_prompt(rd, tid), f"submit {tid}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["next"])
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()

    if not (args.run_dir / "state.json").exists():
        print(f"ERROR: not a run dir (no state.json): {args.run_dir}", file=sys.stderr)
        return 2

    print(json.dumps(compute_next(args.run_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
