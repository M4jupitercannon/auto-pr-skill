#!/usr/bin/env python3
"""Validated artifact writer for agents with read-only edit permissions."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from validate_json import validate_final_review, validate_review, validate_triage  # noqa: E402


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def read_stdin() -> str:
    data = sys.stdin.read()
    if not data:
        raise ValueError("stdin is empty")
    return data


def load_json_stdin() -> dict:
    data = json.loads(read_stdin())
    if not isinstance(data, dict):
        raise ValueError("stdin JSON must be an object")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=[
        "review", "final-review", "triage", "pr-title", "pr-body", "branch",
        "stuck", "abandon", "human-review-needed",
    ])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("task_id")
    parser.add_argument("--round", type=int, default=None)
    args = parser.parse_args()

    task_dir = args.run_dir / "tasks" / args.task_id
    if not task_dir.is_dir():
        print(f"ERROR: task dir not found: {task_dir}", file=sys.stderr)
        return 2

    try:
        if args.kind == "review":
            if args.round is None:
                raise ValueError("--round is required for review")
            data = load_json_stdin()
            validate_review(data)
            if data["round"] != args.round:
                raise ValueError(f"review.round {data['round']} != --round {args.round}")
            path = task_dir / f"review-{args.round}.json"
            atomic_write(path, json.dumps(data, indent=2) + "\n")
        elif args.kind == "final-review":
            data = load_json_stdin()
            validate_final_review(data)
            path = task_dir / "final-review.json"
            atomic_write(path, json.dumps(data, indent=2) + "\n")
        elif args.kind == "triage":
            data = load_json_stdin()
            validate_triage(data)
            path = task_dir / "triage.json"
            atomic_write(path, json.dumps(data, indent=2) + "\n")
        elif args.kind in {"stuck", "abandon", "human-review-needed"}:
            data = load_json_stdin()
            if "reason" not in data or not isinstance(data["reason"], str):
                raise ValueError(f"{args.kind} JSON must include string reason")
            path = task_dir / f"{args.kind}.json"
            atomic_write(path, json.dumps(data, indent=2) + "\n")
        else:
            text = read_stdin()
            if args.kind in {"pr-title", "branch"}:
                text = text.strip() + "\n"
            path = {
                "pr-title": task_dir / "pr_title.txt",
                "pr-body": task_dir / "pr_body.md",
                "branch": task_dir / "branch",
            }[args.kind]
            atomic_write(path, text)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
