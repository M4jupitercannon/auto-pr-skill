#!/usr/bin/env python3
"""
classify_errors.py — group ctest failures into compact, prioritized tasks.

Reads JSONL from `parse_ctest.py` (one failure per line) and writes:
  * <out_dir>/tasks.json     — array of task summaries (small, orchestrator reads this)
  * <out_dir>/tasks/<id>/task.md — one compact brief per task (≤ ~30KB)

Grouping key
------------
Failures that share `fingerprint` (exception class @ tail-frame file:line) are
considered the same root cause and merged into one task. Timeouts that don't
produce a traceback are grouped by pure name prefix (e.g. all
`test_*_interp_op` timeouts).

Priority
--------
score = (#failures-in-group) * weight(status) * weight(file_path)
Higher score = higher priority. Tasks are emitted sorted desc.

Output schema is documented in templates/tasks.schema.json.

Usage:
    python3 classify_errors.py \\
        --in failures.jsonl \\
        --out-dir <run_dir> \\
        --max-tasks 10 \\
        --max-task-bytes 30000
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

STATUS_WEIGHT = {"failed": 1.0, "timeout": 0.5}
BUILD_FAILURE_ID = "task-01-build-failure"

GENERIC_FRAME_PATTERNS = (
    re.compile(r"(^|/)test/legacy_test/op_test\.py$"),
    re.compile(r"(^|/)python/paddle/base/(executor|framework)\.py$"),
    re.compile(r"(^|/)python/paddle/base/dygraph/(generated_tensor_methods_patch|tensor_patch_methods)\.py$"),
    re.compile(r"(^|/)python/paddle/pir/math_op_patch\.py$"),
)
NON_PROJECT_PATTERNS = (
    "/site-packages/",
    "/dist-packages/",
    "/.venv/",
    "/lib/python",
)
SOURCE_LOCATION_RE = re.compile(
    r"(?P<file>(?:[A-Za-z]:)?/?(?:[^:\s]*/)?(?:paddle|python|test|cmake)/[^:\s]+):(?P<line>\d+)"
)

# Boost tasks touching obviously fixable paths; demote infra-y ones
PATH_WEIGHT_RULES = [
    (re.compile(r"(^|/)test/"), 1.0),
    (re.compile(r"(^|/)python/paddle/"), 1.2),
    (re.compile(r"(^|/)paddle/phi/kernels/"), 1.4),
    (re.compile(r"(^|/)paddle/cinn/"), 0.6),  # cinn changes usually need humans
    (re.compile(r"(^|/)paddle/fluid/distributed/"), 0.5),
]


def path_weight(path: str) -> float:
    for rx, w in PATH_WEIGHT_RULES:
        if rx.search(path):
            return w
    return 1.0


def is_non_project_path(path: str) -> bool:
    return any(marker in path for marker in NON_PROJECT_PATTERNS)


def is_generic_frame(path: str) -> bool:
    return any(rx.search(path) for rx in GENERIC_FRAME_PATTERNS)


def normalize_path(path: str, repo_path: str = "") -> str:
    raw = path
    if repo_path:
        repo = repo_path.rstrip("/")
        if raw.startswith(f"{repo}/build/"):
            raw = f"{repo}/{raw[len(f'{repo}/build/'):]}"
        if raw.startswith(f"{repo}/"):
            return raw[len(repo) + 1 :]
    if "/build/" in raw:
        _, suffix = raw.split("/build/", 1)
        if suffix.startswith(("test/", "python/", "paddle/")):
            return suffix
    return raw


def frame_score(frame: dict) -> tuple[int, int]:
    path = frame.get("file", "")
    if is_non_project_path(path):
        return (-10, 0)
    if is_generic_frame(path):
        return (0, 0)
    if frame.get("func") == "source_hint":
        return (50, 0)
    if path.startswith("paddle/"):
        return (40, 0)
    if path.startswith("python/paddle/"):
        return (35, 0)
    if path.startswith("test/"):
        return (30, 0)
    return (20, 0)


def best_frame(frames: list[dict]) -> dict | None:
    if not frames:
        return None
    return max(
        enumerate(frames),
        key=lambda item: (frame_score(item[1])[0], item[0]),
    )[1]


def normalize_failure(failure: dict, repo_path: str = "") -> dict:
    frames = []
    for frame in failure.get("frames") or []:
        normalized = dict(frame)
        normalized["file"] = normalize_path(str(normalized.get("file", "")), repo_path)
        if not is_non_project_path(normalized["file"]):
            frames.append(normalized)

    useful = [fr for fr in frames if not is_generic_frame(fr["file"])]
    failure["frames"] = (useful or frames)[-8:]
    primary = best_frame(failure["frames"])
    failure["primary_frame"] = primary
    if primary:
        failure["fingerprint"] = f"{failure.get('exception') or 'Unknown'}@{primary['file']}:{primary['line']}"
    return failure


def load_failures(path: Path) -> list[dict]:
    out = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"[classify] skip malformed line: {e}", file=sys.stderr)
    return out


def build_failed(build_exit_path: Path | None) -> bool:
    if not build_exit_path or not build_exit_path.exists():
        return False
    try:
        return int(build_exit_path.read_text().strip() or "0") != 0
    except ValueError:
        return True


def build_failure_task(build_log_path: Path | None, out_dir: Path) -> list[dict]:
    excerpt = "Build failed before CTest failures could be parsed."
    files: list[str] = []
    if build_log_path and build_log_path.exists():
        lines = build_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        interesting = [
            line for line in lines
            if re.search(r"\b(error|failed|undefined reference|fatal):", line, re.IGNORECASE)
            or SOURCE_LOCATION_RE.search(line)
        ]
        excerpt_lines = (interesting or lines)[-80:]
        excerpt = "\n".join(excerpt_lines)
        seen: set[str] = set()
        for line in interesting:
            for match in SOURCE_LOCATION_RE.finditer(line):
                path = normalize_path(match.group("file"))
                if is_non_project_path(path) or path in seen:
                    continue
                seen.add(path)
                files.append(path)

    task = {
        "id": BUILD_FAILURE_ID,
        "fingerprint": "build-failure@build.log",
        "status": "failed",
        "exception": "BuildFailure",
        "n_failures": 1,
        "tests": ["build"],
        "rep_file": files[0] if files else "build.log",
        "rep_line": None,
        "files": files[:20],
        "score": 1.0,
        "summary": "Build command failed before actionable CTest failures were parsed.",
    }

    tasks_dir = out_dir / "tasks" / task["id"]
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (tasks_dir / "task.md").write_text(render_task_md(task, [{"id": "build", "raw_excerpt": excerpt}], 30_000))
    return [task]


def slugify(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    return s.strip("_.-") or "task"


def group_failures(failures: list[dict]) -> dict[str, list[dict]]:
    """Bucket failures by fingerprint, with timeout-only fallback grouping."""
    groups: dict[str, list[dict]] = defaultdict(list)

    for f in failures:
        fp = f.get("fingerprint") or "unknown"
        if not f.get("frames") and f.get("status") == "timeout":
            # Timeouts often share a kernel; group by name family.
            base = re.sub(r"_op$|_v\d+$|_\d+$", "", f["id"])
            fp = f"timeout-family:{base}"
        groups[fp].append(f)

    return groups


def score_group(items: list[dict]) -> float:
    n = len(items)
    status_w = max(STATUS_WEIGHT.get(it["status"], 0.5) for it in items)

    # Use the most representative actionable path, not a traceback helper tail.
    rep_path = ""
    for it in items:
        primary = it.get("primary_frame") or best_frame(it.get("frames") or [])
        if primary:
            rep_path = primary["file"]
            break
    if not rep_path:
        rep_path = items[0]["id"]

    return n * status_w * path_weight(rep_path)


def make_task(group_id: str, items: list[dict], task_idx: int) -> dict:
    rep = items[0]
    primary = rep.get("primary_frame") or best_frame(rep.get("frames") or [])
    rep_file = primary["file"] if primary else None
    rep_line = primary["line"] if primary else None

    # Collect unique source files mentioned across all failures in the group.
    files: dict[str, int] = {}
    for it in items:
        for fr in it.get("frames") or []:
            path = fr["file"]
            if is_non_project_path(path):
                continue
            score = frame_score(fr)[0]
            files[path] = max(files.get(path, -10), score)

    ordered_files = [
        path for path, _score in sorted(
            files.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]

    return {
        "id": f"task-{task_idx:02d}-{slugify(group_id)[:48]}",
        "fingerprint": group_id,
        "status": rep["status"],
        "exception": rep.get("exception"),
        "n_failures": len(items),
        "tests": [it["id"] for it in items[:50]],
        "rep_file": rep_file,
        "rep_line": rep_line,
        "files": ordered_files,
        "score": round(score_group(items), 3),
        "summary": _short_summary(rep),
    }


def _short_summary(rep: dict) -> str:
    excerpt = rep.get("raw_excerpt", "")
    msg = ""
    for line in excerpt.splitlines():
        em_match = re.match(r"^([A-Z][A-Za-z0-9_]*(?:Error|Exception)):\s*(.+)$", line)
        if em_match:
            msg = em_match.group(2).strip()
            break
    if not msg and rep.get("status") == "timeout":
        msg = f"timed out after {rep.get('duration_sec', 0):.1f}s"
    return msg[:200]


def render_task_md(task: dict, items: list[dict], max_bytes: int) -> str:
    """Render a compact markdown brief for a single task. Capped at max_bytes."""
    lines: list[str] = []
    lines.append(f"# Task `{task['id']}`")
    lines.append("")
    lines.append(f"- **Status**: {task['status']}")
    lines.append(f"- **Failures**: {task['n_failures']}")
    if task.get("exception"):
        lines.append(f"- **Exception**: `{task['exception']}`")
    if task.get("summary"):
        lines.append(f"- **Summary**: {task['summary']}")
    lines.append("")

    if task.get("rep_file"):
        loc = f"{task['rep_file']}:{task['rep_line']}" if task.get("rep_line") else task["rep_file"]
        lines.append(f"- **Primary file**: `{loc}`")
        lines.append("")

    if task["files"]:
        lines.append("## Touched files (best guesses)")
        for fp in task["files"][:20]:
            lines.append(f"- `{fp}`")
        lines.append("")

    lines.append("## Affected tests")
    for t in task["tests"]:
        lines.append(f"- `{t}`")
    lines.append("")

    lines.append("## Representative failure excerpt")
    rep_excerpt = items[0].get("raw_excerpt", "").rstrip()
    lines.append("```")
    lines.append(rep_excerpt)
    lines.append("```")
    lines.append("")

    # Add up to 2 more excerpts from other failures in the group, while respecting size.
    for extra in items[1:3]:
        ex = extra.get("raw_excerpt", "").rstrip()
        if not ex:
            continue
        lines.append(f"### Also failing: `{extra['id']}`")
        lines.append("```")
        lines.append(ex[:2000])
        lines.append("```")
        lines.append("")

    text = "\n".join(lines)
    if len(text.encode()) > max_bytes:
        # Trim from the middle of the representative excerpt
        head, sep, tail = text.partition("## Representative failure excerpt")
        budget = max_bytes - len(head.encode()) - 200
        if budget < 1000:
            text = text.encode()[:max_bytes].decode("utf-8", errors="ignore")
        else:
            tail_trimmed = tail[:budget] + "\n\n...[truncated]...\n```\n"
            text = head + sep + tail_trimmed
    return text


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--in", dest="input", type=Path, required=True,
                   help="path to failures.jsonl from parse_ctest.py")
    p.add_argument("--out-dir", type=Path, required=True,
                   help="run directory; tasks.json + tasks/ are written here")
    p.add_argument("--max-tasks", type=int, default=10,
                   help="cap on number of tasks emitted (default: 10)")
    p.add_argument("--max-task-bytes", type=int, default=30_000,
                   help="cap per task.md size (default: 30000)")
    p.add_argument("--repo-path", type=str, default="",
                   help="project root used to remap build/ paths")
    p.add_argument("--build-exit", type=Path, default=None,
                   help="optional build.exit; non-zero creates a build task if no ctest failures were parsed")
    p.add_argument("--build-log", type=Path, default=None,
                   help="optional build.log used for fallback build-failure task")
    args = p.parse_args()

    failures = [normalize_failure(f, args.repo_path) for f in load_failures(args.input)]
    if not failures:
        if build_failed(args.build_exit):
            tasks = build_failure_task(args.build_log, args.out_dir)
            (args.out_dir / "tasks.json").write_text(json.dumps(tasks, indent=2) + "\n")
            print("[classify] build failed with no parsed ctest failures; wrote build-failure task", file=sys.stderr)
        else:
            print("[classify] no failures parsed; writing empty tasks.json", file=sys.stderr)
            (args.out_dir / "tasks.json").write_text("[]\n")
        return 0

    groups = group_failures(failures)
    scored = sorted(
        ((gid, items) for gid, items in groups.items()),
        key=lambda gi: score_group(gi[1]),
        reverse=True,
    )

    tasks: list[dict] = []
    tasks_dir = args.out_dir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    for idx, (gid, items) in enumerate(scored[: args.max_tasks], start=1):
        task = make_task(gid, items, idx)
        tdir = tasks_dir / task["id"]
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "task.md").write_text(render_task_md(task, items, args.max_task_bytes))
        tasks.append(task)

    (args.out_dir / "tasks.json").write_text(json.dumps(tasks, indent=2) + "\n")
    print(
        f"[classify] {len(failures)} failures -> {len(tasks)} tasks "
        f"(of {len(groups)} groups). tasks.json written to {args.out_dir}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
