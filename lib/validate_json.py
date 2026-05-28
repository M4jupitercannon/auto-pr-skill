#!/usr/bin/env python3
"""Lightweight stdlib validators for auto-pr JSON artifacts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


class ValidationError(ValueError):
    pass


REVIEW_CATEGORIES = {
    "correctness",
    "test-coverage",
    "regression-risk",
    "cross-platform",
    "build",
    "style",
    "security",
    "other",
}

FINAL_REVIEW_DIMENSIONS = REVIEW_CATEGORIES | {"triage"}


def require_keys(obj: dict[str, Any], keys: set[str], label: str) -> None:
    missing = sorted(keys - obj.keys())
    if missing:
        raise ValidationError(f"{label} missing required keys: {', '.join(missing)}")


def reject_extra_keys(obj: dict[str, Any], allowed: set[str], label: str) -> None:
    extra = sorted(obj.keys() - allowed)
    if extra:
        raise ValidationError(f"{label} has unknown keys: {', '.join(extra)}")


def expect_type(value: Any, expected: type | tuple[type, ...], label: str) -> None:
    if not isinstance(value, expected):
        names = ", ".join(t.__name__ for t in expected) if isinstance(expected, tuple) else expected.__name__
        raise ValidationError(f"{label} must be {names}")


def validate_issue(item: Any, label: str, require_category: bool) -> None:
    expect_type(item, dict, label)
    allowed = {"category", "file", "line", "message"}
    required = {"message", "category"} if require_category else {"message"}
    require_keys(item, required, label)
    reject_extra_keys(item, allowed, label)
    expect_type(item["message"], str, f"{label}.message")
    if "category" in item:
        expect_type(item["category"], str, f"{label}.category")
    if item.get("file") is not None:
        expect_type(item["file"], str, f"{label}.file")
    if item.get("line") is not None:
        expect_type(item["line"], int, f"{label}.line")


def validate_review(data: Any) -> None:
    expect_type(data, dict, "review")
    allowed = {"round", "approved", "verdict", "needs_human", "blocking", "suggestions"}
    require_keys(data, allowed, "review")
    reject_extra_keys(data, allowed, "review")
    expect_type(data["round"], int, "review.round")
    expect_type(data["approved"], bool, "review.approved")
    expect_type(data["needs_human"], bool, "review.needs_human")
    if data["verdict"] not in {"approve", "request_changes", "block"}:
        raise ValidationError("review.verdict must be approve, request_changes, or block")
    expect_type(data["blocking"], list, "review.blocking")
    expect_type(data["suggestions"], list, "review.suggestions")
    for idx, item in enumerate(data["blocking"]):
        validate_issue(item, f"review.blocking[{idx}]", require_category=True)
        if item["category"] not in REVIEW_CATEGORIES:
            raise ValidationError(f"review.blocking[{idx}].category is unsupported")
    for idx, item in enumerate(data["suggestions"]):
        validate_issue(item, f"review.suggestions[{idx}]", require_category=False)
    if data["approved"] and (data["verdict"] != "approve" or data["blocking"]):
        raise ValidationError("approved reviews must have verdict=approve and empty blocking")


def validate_final_review_concern(item: Any, label: str) -> None:
    expect_type(item, dict, label)
    allowed = {"dimension", "file", "line", "message"}
    require_keys(item, {"dimension", "message"}, label)
    reject_extra_keys(item, allowed, label)
    expect_type(item["dimension"], str, f"{label}.dimension")
    if item["dimension"] not in FINAL_REVIEW_DIMENSIONS:
        raise ValidationError(f"{label}.dimension is unsupported")
    expect_type(item["message"], str, f"{label}.message")
    if item.get("file") is not None:
        expect_type(item["file"], str, f"{label}.file")
    if item.get("line") is not None:
        expect_type(item["line"], int, f"{label}.line")


def validate_final_review(data: Any) -> None:
    expect_type(data, dict, "final_review")
    allowed = {
        "verdict",
        "needs_human",
        "concerns",
        "agreed_with_reviewer",
        "agreed_with_triage",
    }
    require_keys(data, allowed, "final_review")
    reject_extra_keys(data, allowed, "final_review")
    if data["verdict"] not in {"approve", "request_changes", "block"}:
        raise ValidationError("final_review.verdict must be approve, request_changes, or block")
    expect_type(data["needs_human"], bool, "final_review.needs_human")
    expect_type(data["concerns"], list, "final_review.concerns")
    expect_type(data["agreed_with_reviewer"], bool, "final_review.agreed_with_reviewer")
    expect_type(data["agreed_with_triage"], bool, "final_review.agreed_with_triage")
    for idx, item in enumerate(data["concerns"]):
        validate_final_review_concern(item, f"final_review.concerns[{idx}]")
    if data["verdict"] == "approve" and data["concerns"]:
        raise ValidationError("approved final reviews must have empty concerns")
    if data["verdict"] == "block" and not data["needs_human"]:
        raise ValidationError("blocked final reviews must set needs_human=true")
    if data["verdict"] != "block" and data["needs_human"]:
        raise ValidationError("only blocked final reviews may set needs_human=true")


def validate_triage(data: Any) -> None:
    expect_type(data, dict, "triage")
    allowed = {"needs_human", "reasons", "diff_stats", "matched_paths"}
    require_keys(data, allowed, "triage")
    reject_extra_keys(data, allowed, "triage")
    expect_type(data["needs_human"], bool, "triage.needs_human")
    expect_type(data["reasons"], list, "triage.reasons")
    expect_type(data["matched_paths"], list, "triage.matched_paths")
    if any(not isinstance(item, str) for item in data["reasons"]):
        raise ValidationError("triage.reasons must contain strings")
    if any(not isinstance(item, str) for item in data["matched_paths"]):
        raise ValidationError("triage.matched_paths must contain strings")
    expect_type(data["diff_stats"], dict, "triage.diff_stats")
    require_keys(data["diff_stats"], {"files_changed", "insertions", "deletions"}, "triage.diff_stats")
    reject_extra_keys(data["diff_stats"], {"files_changed", "insertions", "deletions"}, "triage.diff_stats")
    for key in ("files_changed", "insertions", "deletions"):
        expect_type(data["diff_stats"][key], int, f"triage.diff_stats.{key}")
        if data["diff_stats"][key] < 0:
            raise ValidationError(f"triage.diff_stats.{key} must be >= 0")


def validate_tasks(data: Any) -> None:
    expect_type(data, list, "tasks")
    allowed = {
        "id", "fingerprint", "status", "exception", "n_failures", "tests",
        "rep_file", "rep_line", "files", "score", "summary",
    }
    required = allowed
    for idx, task in enumerate(data):
        label = f"tasks[{idx}]"
        expect_type(task, dict, label)
        require_keys(task, required, label)
        reject_extra_keys(task, allowed, label)
        for key in ("id", "fingerprint", "status", "summary"):
            expect_type(task[key], str, f"{label}.{key}")
        if task["status"] not in {"failed", "timeout"}:
            raise ValidationError(f"{label}.status must be failed or timeout")
        if task["exception"] is not None:
            expect_type(task["exception"], str, f"{label}.exception")
        expect_type(task["n_failures"], int, f"{label}.n_failures")
        expect_type(task["tests"], list, f"{label}.tests")
        expect_type(task["files"], list, f"{label}.files")
        expect_type(task["score"], (int, float), f"{label}.score")
        if task["rep_file"] is not None:
            expect_type(task["rep_file"], str, f"{label}.rep_file")
        if task["rep_line"] is not None:
            expect_type(task["rep_line"], int, f"{label}.rep_line")


VALIDATORS = {
    "tasks": validate_tasks,
    "tasks.schema.json": validate_tasks,
    "review": validate_review,
    "review.schema.json": validate_review,
    "final-review": validate_final_review,
    "final-review.schema.json": validate_final_review,
    "triage": validate_triage,
    "triage.schema.json": validate_triage,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schema", help="tasks, review, final-review, triage, or schema basename")
    parser.add_argument("json_file", type=Path)
    args = parser.parse_args()

    validator = VALIDATORS.get(Path(args.schema).name)
    if not validator:
        print(f"ERROR: unsupported schema: {args.schema}", file=sys.stderr)
        return 2

    try:
        data = json.loads(args.json_file.read_text())
        validator(data)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"[validate_json] {Path(args.schema).name} OK: {args.json_file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
