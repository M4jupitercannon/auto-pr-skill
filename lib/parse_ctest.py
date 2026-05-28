#!/usr/bin/env python3
"""
parse_ctest.py — extract structured failure records from a CTest log.

Reads a CTest output-on-failure log (possibly produced with `-j N`, so
interleaved) and emits one JSON object per failed/timed-out test on stdout
(JSONL).

Each record:
  {
    "id":        "test_audio_functions",       # the ctest test name
    "test_num":  557,                          # ctest numeric id
    "status":    "failed" | "timeout",
    "duration_sec": 8.27,
    "summary_line_no": 117,                    # 1-based line in the log
    "frames": [
      {"file": "...", "line": 407, "func": "test_istft", "msg": "..."}
    ],
    "exception": "AssertionError",             # last raised exception class
    "raw_excerpt": "...",                      # up to ~120 lines around failure
    "fingerprint": "AssertionError@build/.../test_audio_functions.py:407"
  }

Designed for large logs without deps outside stdlib. It reads the log once into
memory so failure output can be associated with the summary line that precedes
it in CTest's output-on-failure format.

Usage:
    python3 parse_ctest.py path/to/ctest.log > failures.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------
SUMMARY_RE = re.compile(
    r"^\s*\d+/\d+\s+Test\s+#(?P<num>\d+):\s+(?P<name>\S+).*?\*{3}(?P<status>Failed|Timeout)\s+(?P<dur>[\d.]+)\s+sec",
)
# Marks the boundary where a test's captured output starts in --output-on-failure
TEST_OUTPUT_HEADER_RE = re.compile(
    r"^The following tests FAILED:|^Errors while running CTest|^\s*Start\s+\d+:",
)
TRACEBACK_FILE_RE = re.compile(
    r'^\s*File "(?P<file>[^"]+)",\s+line\s+(?P<line>\d+),\s+in\s+(?P<func>\S+)',
)
EXCEPTION_RE = re.compile(
    r"^(?P<cls>[A-Z][A-Za-z0-9_]*(?:Error|Exception|Failure)):\s*(?P<msg>.+)?$",
)
# C++/gtest style failure marker
GTEST_FAIL_RE = re.compile(r"^\[\s+FAILED\s+\]\s+(\S+)")
AT_SOURCE_RE = re.compile(r"\(at (?P<file>[^():]+):(?P<line>\d+)\)")

MAX_EXCERPT_LINES = 120
MAX_EXCERPT_BYTES = 8 * 1024

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


@dataclass
class Failure:
    name: str
    test_num: int
    status: str
    duration_sec: float
    summary_line_no: int
    frames: list[dict] = field(default_factory=list)
    exception: str | None = None
    raw_excerpt: str = ""

    def fingerprint(self) -> str:
        """A stable group key used by classify_errors.py."""
        primary = best_frame(self.frames)
        if primary:
            return f"{self.exception or 'Unknown'}@{primary['file']}:{primary['line']}"
        return f"{self.exception or self.status}@{self.name}"

    def to_dict(self) -> dict:
        return {
            "id": self.name,
            "test_num": self.test_num,
            "status": self.status,
            "duration_sec": self.duration_sec,
            "summary_line_no": self.summary_line_no,
            "exception": self.exception,
            "frames": self.frames,
            "primary_frame": best_frame(self.frames),
            "raw_excerpt": self.raw_excerpt,
            "fingerprint": self.fingerprint(),
        }


def is_non_project_path(path: str) -> bool:
    return any(marker in path for marker in NON_PROJECT_PATTERNS)


def is_generic_frame(path: str) -> bool:
    return any(rx.search(path) for rx in GENERIC_FRAME_PATTERNS)


def normalize_path(path: str, repo_path: Path | None) -> str:
    """Return a repo-relative, source-tree path when a CTest frame points at build/."""
    raw = path
    if repo_path:
        repo = str(repo_path)
        if raw.startswith(f"{repo}/build/"):
            raw = f"{repo}/{raw[len(f'{repo}/build/'):]}"
        if raw.startswith(f"{repo}/"):
            return raw[len(repo) + 1 :]

    # Best-effort fallback for historical failures.jsonl without --repo-path.
    build_marker = "/build/"
    if build_marker in raw:
        prefix, suffix = raw.split(build_marker, 1)
        if suffix.startswith(("test/", "python/", "paddle/")):
            return suffix
        raw = f"{prefix}/{suffix}"
    return raw


def frame_score(frame: dict) -> tuple[int, int]:
    path = frame["file"]
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
    scored = [(frame_score(fr), idx, fr) for idx, fr in enumerate(frames)]
    return max(scored, key=lambda item: (item[0][0], item[1]))[2]


def trim_excerpt(lines: list[str]) -> str:
    excerpt = "".join(lines[:MAX_EXCERPT_LINES])
    if len(excerpt.encode()) > MAX_EXCERPT_BYTES:
        excerpt = excerpt.encode()[:MAX_EXCERPT_BYTES].decode("utf-8", errors="ignore")
    return excerpt


def parse_failure_block(block: list[str], repo_path: Path | None) -> tuple[list[dict], str | None]:
    frames: list[dict] = []
    exception: str | None = None
    seen_hints: set[tuple[str, int]] = set()

    for raw in block:
        m = TRACEBACK_FILE_RE.match(raw)
        if m:
            file_path = normalize_path(m.group("file"), repo_path)
            if is_non_project_path(file_path):
                continue
            frames.append({
                "file": file_path,
                "line": int(m.group("line")),
                "func": m.group("func"),
            })
            continue

        hint = AT_SOURCE_RE.search(raw)
        if hint:
            file_path = normalize_path(hint.group("file"), repo_path)
            key = (file_path, int(hint.group("line")))
            if key not in seen_hints and not is_non_project_path(file_path):
                frames.append({
                    "file": file_path,
                    "line": int(hint.group("line")),
                    "func": "source_hint",
                })
                seen_hints.add(key)

        em = EXCEPTION_RE.match(raw.rstrip())
        if em:
            exception = em.group("cls")
            if frames:
                frames[-1]["msg"] = (em.group("msg") or "").strip()

    # Keep enough context for primary-frame selection while dropping pure helpers
    # only when a better project/source frame exists.
    useful = [fr for fr in frames if not is_generic_frame(fr["file"])]
    return (useful or frames)[-8:], exception


def block_end(lines: list[str], start: int) -> int:
    end = min(len(lines), start + MAX_EXCERPT_LINES)
    for idx in range(start + 1, end):
        line = lines[idx]
        if SUMMARY_RE.match(line):
            return idx
        if line.startswith("          Start ") and idx > start + 2:
            return idx
    return end


def parse(log_path: Path, repo_path: Path | None = None) -> list[Failure]:
    """Parse a ctest log and return one Failure per failed/timed-out test.

    Strategy:
      1. Stream the log keeping a rolling window of the last MAX_EXCERPT_LINES.
      2. When we hit a "***Failed" / "***Timeout" summary line, extract the
         most recent block of test output that mentions the test name.
      3. From that block, pull traceback frames + final exception class.
    """
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    failures: list[Failure] = []

    for idx, line in enumerate(lines):
        sm = SUMMARY_RE.match(line)
        if not sm:
            continue

        end = block_end(lines, idx)
        block = lines[idx:end]
        frames, exception = parse_failure_block(block, repo_path)
        failure = Failure(
            name=sm.group("name"),
            test_num=int(sm.group("num")),
            status=sm.group("status").lower(),
            duration_sec=float(sm.group("dur")),
            summary_line_no=idx + 1,
            frames=frames,
            exception=exception,
            raw_excerpt=trim_excerpt(block),
        )
        failures.append(failure)

    return failures


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("log", type=Path, help="path to ctest log")
    p.add_argument(
        "--out", type=Path, default=None,
        help="write JSONL to this file (default: stdout)",
    )
    p.add_argument(
        "--summary", action="store_true",
        help="also print a short text summary to stderr",
    )
    p.add_argument(
        "--repo-path", type=Path, default=None,
        help="project root used to remap build/ paths to repo-relative source paths",
    )
    args = p.parse_args()

    if not args.log.exists():
        print(f"ERROR: log not found: {args.log}", file=sys.stderr)
        return 2

    failures = parse(args.log, args.repo_path)

    out = args.out.open("w") if args.out else sys.stdout
    try:
        for f in failures:
            out.write(json.dumps(f.to_dict(), ensure_ascii=False))
            out.write("\n")
    finally:
        if args.out:
            out.close()

    if args.summary:
        n_failed = sum(1 for f in failures if f.status == "failed")
        n_timeout = sum(1 for f in failures if f.status == "timeout")
        print(
            f"[parse_ctest] {len(failures)} failures "
            f"({n_failed} failed, {n_timeout} timeout) from {args.log}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
