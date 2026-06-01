#!/usr/bin/env python3
"""advance_task.py — atomic, idempotent terminal task transition for /auto-pr.

A task can end in three non-PR ways: ``stuck`` (review budget exhausted /
verification failed), ``human-review-needed`` (a gate wants a human), or
``abandoned`` (the coder judged the fix out of scope). Each of these used to be
two steps — write a skip artifact, then mutate ``state.json`` — which a crash
could split and double-count.

This script collapses both into a single atomic ``state.json`` write: it
writes/refreshes ``<kind>.json``, then bumps ``tasks_done`` / ``task_index`` /
the kind's counter, appends a ``skips`` entry, and clears ``current_task``. It is
idempotent the same way ``state.sh finish-pr`` is: if ``state.skips`` already
records this ``task_id`` the script is a no-op, so a re-issued action never
double-counts.

Usage:
    advance_task.py <run_dir> <task_id> <kind> [--detail STR]
    kind ∈ {stuck, human-review-needed, abandoned}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# kind -> the state.json counter it increments.
KIND_COUNTER = {
    "stuck": "tasks_stuck",
    "human-review-needed": "human_review_needed",
    "abandoned": "tasks_abandoned",
}

# kind -> the artifact basename. "abandoned" maps to the coder's existing
# `abandon.json` (the documented name the driver checks) so its specific reason
# is preserved rather than clobbered by a generic record.
KIND_ARTIFACT = {
    "stuck": "stuck",
    "human-review-needed": "human-review-needed",
    "abandoned": "abandon",
}


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def load_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("task_id")
    parser.add_argument("kind", choices=sorted(KIND_COUNTER))
    parser.add_argument("--detail", default="")
    args = parser.parse_args()

    state_file = args.run_dir / "state.json"
    state = load_json(state_file)
    if not isinstance(state, dict):
        print(f"ERROR: missing/invalid state.json: {state_file}", file=sys.stderr)
        return 2

    task_dir = args.run_dir / "tasks" / args.task_id
    if not task_dir.is_dir():
        print(f"ERROR: task dir not found: {task_dir}", file=sys.stderr)
        return 3

    skips = state.get("skips") or []
    # Idempotent: a prior advance for this task already counted it (mirrors the
    # dedupe-by-number guard in state.sh finish-pr). Do nothing on replay.
    if any(isinstance(s, dict) and s.get("task_id") == args.task_id for s in skips):
        print(f"[advance_task] {args.task_id} already advanced; no-op", file=sys.stderr)
        return 0

    # Write/refresh the artifact. Preserve an existing reason (e.g. the coder's
    # abandon.json) instead of clobbering it.
    artifact = task_dir / f"{KIND_ARTIFACT[args.kind]}.json"
    detail = load_json(artifact)
    if not (isinstance(detail, dict) and isinstance(detail.get("reason"), str)):
        detail = {"reason": args.kind, "details": args.detail, "task_id": args.task_id}
    atomic_write(artifact, json.dumps(detail, indent=2) + "\n")

    counter = KIND_COUNTER[args.kind]
    state["tasks_done"] = int(state.get("tasks_done", 0) or 0) + 1
    state["task_index"] = int(state.get("task_index", 0) or 0) + 1
    state[counter] = int(state.get(counter, 0) or 0) + 1
    state["skips"] = skips + [{"task_id": args.task_id, "kind": args.kind, "detail": detail}]
    state["current_task"] = None
    atomic_write(state_file, json.dumps(state, indent=2) + "\n")

    print(str(artifact))
    return 0


if __name__ == "__main__":
    sys.exit(main())
