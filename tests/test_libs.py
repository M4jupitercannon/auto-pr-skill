#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
sys.path.insert(0, str(LIB))

import parse_ctest  # noqa: E402
import classify_errors  # noqa: E402
import validate_json  # noqa: E402
import orchestrate  # noqa: E402


class ParseCTestTests(unittest.TestCase):
    def test_remaps_build_paths_and_prefers_source_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "ctest.log"
            log.write_text(
                """  1/1 Test #1: test_inverse ........................................................................***Failed    1.00 sec
Traceback (most recent call last):
  File "/repo/Paddle/build/test/legacy_test/test_zero_dim.py", line 10, in test_inverse
    paddle.linalg.cond(x)
  File "/repo/Paddle/build/test/legacy_test/op_test.py", line 621, in tearDownClass
    raise AssertionError("harness")
ValueError: (InvalidArgument) Input is not invertible. (at /repo/Paddle/paddle/phi/kernels/funcs/matrix_inverse.h:48)
""",
                encoding="utf-8",
            )

            failures = parse_ctest.parse(log, Path("/repo/Paddle"))

        self.assertEqual(len(failures), 1)
        data = failures[0].to_dict()
        self.assertEqual(
            data["fingerprint"],
            "ValueError@paddle/phi/kernels/funcs/matrix_inverse.h:48",
        )
        self.assertEqual(data["primary_frame"]["file"], "paddle/phi/kernels/funcs/matrix_inverse.h")
        self.assertNotIn("build/test/legacy_test/op_test.py", [frame["file"] for frame in data["frames"]])


class ClassifyTests(unittest.TestCase):
    def test_path_weights_apply_to_normalized_repo_relative_paths(self) -> None:
        self.assertEqual(classify_errors.path_weight("paddle/phi/kernels/add_kernel.cc"), 1.4)
        self.assertEqual(classify_errors.path_weight("python/paddle/tensor/math.py"), 1.2)
        self.assertEqual(classify_errors.path_weight("paddle/cinn/hlir/foo.cc"), 0.6)
        self.assertEqual(classify_errors.path_weight("paddle/fluid/distributed/foo.cc"), 0.5)
        self.assertEqual(classify_errors.path_weight("test/legacy_test/test_op.py"), 1.0)
        self.assertEqual(classify_errors.path_weight("/repo/Paddle/paddle/phi/kernels/add_kernel.cc"), 1.4)

    def test_nonzero_build_exit_creates_build_failure_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            failures = root / "failures.jsonl"
            failures.write_text("", encoding="utf-8")
            build_exit = root / "build.exit"
            build_exit.write_text("1\n", encoding="utf-8")
            build_log = root / "build.log"
            build_log.write_text("paddle/foo.cc:12: error: unknown type name\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(LIB / "classify_errors.py"),
                    "--in",
                    str(failures),
                    "--out-dir",
                    str(root),
                    "--build-exit",
                    str(build_exit),
                    "--build-log",
                    str(build_log),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            tasks = json.loads((root / "tasks.json").read_text())
            self.assertEqual(tasks[0]["id"], "task-01-build-failure")
            self.assertEqual(tasks[0]["rep_file"], "paddle/foo.cc")
            validate_json.validate_tasks(tasks)


class GitArtifactIgnoreTests(unittest.TestCase):
    def _git(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_artifact_pathspec_excludes_auto_pr_and_opencode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self.assertEqual(subprocess.run(["git", "init", str(repo)], capture_output=True).returncode, 0)

            (repo / ".auto-pr" / "run-test").mkdir(parents=True)
            (repo / ".auto-pr" / "run-test" / "state.json").write_text("{}\n", encoding="utf-8")
            (repo / ".opencode" / "agents").mkdir(parents=True)
            (repo / ".opencode" / "agents" / "auto-pr-coder.md").write_text("agent\n", encoding="utf-8")
            (repo / "real_change.txt").write_text("real\n", encoding="utf-8")

            unfiltered = self._git(repo, "status", "--porcelain")
            self.assertIn(".auto-pr/", unfiltered.stdout)
            self.assertIn(".opencode/", unfiltered.stdout)

            filtered = self._git(
                repo,
                "status",
                "--porcelain",
                "--",
                ".",
                ":(exclude).auto-pr",
                ":(exclude).opencode",
            )
            self.assertEqual(filtered.returncode, 0, filtered.stderr)
            self.assertIn("real_change.txt", filtered.stdout)
            self.assertNotIn(".auto-pr", filtered.stdout)
            self.assertNotIn(".opencode", filtered.stdout)

            add_result = self._git(
                repo,
                "add",
                "-A",
                "--",
                ".",
                ":(exclude).auto-pr",
                ":(exclude).opencode",
            )
            self.assertEqual(add_result.returncode, 0, add_result.stderr)
            staged = self._git(repo, "diff", "--cached", "--name-only")
            self.assertEqual(staged.returncode, 0, staged.stderr)
            self.assertEqual(staged.stdout.strip(), "real_change.txt")


class InitRunTests(unittest.TestCase):
    def _write_profile(self, path: Path, name: str, repo_path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"name: {name}\nrepo_path: {repo_path}\nbranch_prefix: auto-pr/\nbase_branch: develop\n",
            encoding="utf-8",
        )

    def _run_init(self, repo: Path, config: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(config)
        env["HOME"] = str(config / "home")
        return subprocess.run(
            [str(LIB / "init_run.sh"), *args],
            cwd=str(cwd or ROOT),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_repo_override_profile_must_match_requested_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            config = root / "config"
            repo.mkdir()
            self.assertEqual(subprocess.run(["git", "init", str(repo)], capture_output=True).returncode, 0)

            self._write_profile(repo / ".auto-pr" / "profile.yaml", "other", root / "missing")
            self._write_profile(config / "auto-pr" / "projects" / "paddle.yaml", "paddle", root / "ignored")

            result = self._run_init(repo, config, "paddle", "--repo", str(repo))

            self.assertEqual(result.returncode, 0, result.stderr)
            run_dir = Path(result.stdout.strip())
            self.assertEqual(run_dir.parent, repo / ".auto-pr")
            self.assertIn("name: paddle", (run_dir / "profile.yaml").read_text(encoding="utf-8"))
            exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertIn("/.auto-pr/", exclude)
            self.assertIn("/.opencode/", exclude)

    def test_cwd_profile_must_match_requested_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            config = root / "config"
            repo.mkdir()
            self.assertEqual(subprocess.run(["git", "init", str(repo)], capture_output=True).returncode, 0)

            self._write_profile(repo / ".auto-pr" / "profile.yaml", "other", root / "missing")
            self._write_profile(config / "auto-pr" / "projects" / "paddle.yaml", "paddle", repo)

            result = self._run_init(repo, config, "paddle", cwd=repo)

            self.assertEqual(result.returncode, 0, result.stderr)
            run_dir = Path(result.stdout.strip())
            self.assertEqual(run_dir.parent, repo / ".auto-pr")
            self.assertIn("name: paddle", (run_dir / "profile.yaml").read_text(encoding="utf-8"))


class RunBuildTests(unittest.TestCase):
    def test_run_build_records_real_exit_code_after_tee(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            run = root / "run"
            repo.mkdir()
            run.mkdir()
            build_script = root / "build.sh"
            build_script.write_text("#!/usr/bin/env bash\necho boom\nexit 42\n", encoding="utf-8")
            os.chmod(build_script, 0o755)
            (run / "profile.yaml").write_text(
                f"repo_path: {repo}\nbuild_cmd: {build_script}\nbuild_args: all\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [str(LIB / "run_build.sh"), str(run)],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 42)
            self.assertEqual((run / "build.exit").read_text().strip(), "42")
            self.assertIn("boom", (run / "build.log").read_text())


class ValidationTests(unittest.TestCase):
    def test_review_requires_agent_output_fields(self) -> None:
        with self.assertRaises(validate_json.ValidationError):
            validate_json.validate_review({
                "round": 1,
                "approved": True,
                "verdict": "approve",
            })

    def test_review_accepts_cross_platform_blocking_category(self) -> None:
        validate_json.validate_review({
            "round": 1,
            "approved": False,
            "verdict": "request_changes",
            "needs_human": True,
            "blocking": [
                {
                    "category": "cross-platform",
                    "file": "paddle/phi/kernels/gpu/example_kernel.cu",
                    "line": 42,
                    "message": "HIP-only branch removed the CUDA behavior.",
                }
            ],
            "suggestions": [],
        })

    def test_final_review_accepts_request_changes(self) -> None:
        validate_json.validate_final_review({
            "verdict": "request_changes",
            "needs_human": False,
            "concerns": [
                {
                    "dimension": "cross-platform",
                    "file": "paddle/phi/kernels/gpu/example_kernel.cu",
                    "line": 64,
                    "message": "Preserve the CUDA path when adding the HIP fix.",
                }
            ],
            "agreed_with_reviewer": False,
            "agreed_with_triage": True,
        })

    def test_final_review_rejects_missing_fields(self) -> None:
        with self.assertRaises(validate_json.ValidationError):
            validate_json.validate_final_review({
                "verdict": "approve",
                "needs_human": False,
                "concerns": [],
            })

    def test_final_review_rejects_approved_concerns(self) -> None:
        with self.assertRaises(validate_json.ValidationError):
            validate_json.validate_final_review({
                "verdict": "approve",
                "needs_human": False,
                "concerns": [
                    {
                        "dimension": "triage",
                        "message": "This concern must be resolved before approval.",
                    }
                ],
                "agreed_with_reviewer": True,
                "agreed_with_triage": False,
            })


class SchemaDriftTests(unittest.TestCase):
    """Guard against templates/*.schema.json drifting from lib/validate_json.py."""

    def _schema(self, name: str) -> dict:
        return json.loads((ROOT / "templates" / name).read_text())

    def test_review_categories_match_validator(self) -> None:
        schema = self._schema("review.schema.json")
        enum = set(schema["properties"]["blocking"]["items"]["properties"]["category"]["enum"])
        self.assertEqual(enum, validate_json.REVIEW_CATEGORIES)

    def test_final_review_dimensions_match_validator(self) -> None:
        schema = self._schema("final-review.schema.json")
        enum = set(schema["properties"]["concerns"]["items"]["properties"]["dimension"]["enum"])
        self.assertEqual(enum, validate_json.FINAL_REVIEW_DIMENSIONS)


class StateShTests(unittest.TestCase):
    def _state(self, run: Path) -> dict:
        return json.loads((run / "state.json").read_text())

    def _init_state(self, run: Path, **overrides: object) -> None:
        run.mkdir(parents=True, exist_ok=True)
        base = {
            "phase": "task-loop", "task_ids": ["a", "b"], "task_index": 0,
            "tasks_total": 2, "tasks_done": 0, "tasks_stuck": 0,
            "tasks_abandoned": 0, "human_review_needed": 0,
            "current_task": None, "prs": [], "skips": [],
        }
        base.update(overrides)
        (run / "state.json").write_text(json.dumps(base))

    def _run(self, run: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(LIB / "state.sh"), args[0], str(run), *args[1:]],
                              text=True, capture_output=True, check=False)

    def test_set_current_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            self._init_state(run)
            res = self._run(run, "set-current-task", "a")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(self._state(run)["current_task"], "a")

    def test_set_tasks_populates_ids_and_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            self._init_state(run, task_ids=[], tasks_total=0)
            (run / "tasks.json").write_text(json.dumps([
                {"id": "task-01"}, {"id": "task-02"}, {"id": "task-03"},
            ]))
            res = self._run(run, "set-tasks")
            self.assertEqual(res.returncode, 0, res.stderr)
            state = self._state(run)
            self.assertEqual(state["tasks_total"], 3)
            self.assertEqual(state["task_ids"], ["task-01", "task-02", "task-03"])

    def test_marks_advance_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            self._init_state(run)
            (run / "detail.json").write_text('{"reason":"x"}')
            self.assertEqual(self._run(run, "mark-task-stuck", "a", str(run / "detail.json")).returncode, 0)
            state = self._state(run)
            self.assertEqual(state["task_index"], 1)
            self.assertEqual(state["tasks_stuck"], 1)
            self.assertEqual(state["tasks_done"], 1)

    def test_finish_pr_is_idempotent_on_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            self._init_state(run, tasks_done=0)
            (run / "pr.json").write_text(json.dumps({"url": "u", "number": 7}))
            self.assertEqual(self._run(run, "finish-pr", str(run / "pr.json")).returncode, 0)
            first = self._state(run)
            self.assertEqual(len(first["prs"]), 1)
            self.assertEqual(first["task_index"], 1)
            self.assertEqual(first["tasks_done"], 1)
            # Replaying the same finish-pr must be a full no-op: not just the
            # prs[] array, but task_index/tasks_done too (a double-advance would
            # silently skip the next task).
            self.assertEqual(self._run(run, "finish-pr", str(run / "pr.json")).returncode, 0)
            replayed = self._state(run)
            self.assertEqual(len(replayed["prs"]), 1)
            self.assertEqual(replayed["task_index"], 1)
            self.assertEqual(replayed["tasks_done"], 1)


class OrchestrateTests(unittest.TestCase):
    def _make_run(self, tmp: str, *, state: dict | None = None, profile: dict | None = None) -> Path:
        run = Path(tmp) / "run"
        (run / "tasks").mkdir(parents=True)
        base_state = {
            "phase": "task-loop", "task_ids": ["t1"], "task_index": 0,
            "current_task": "t1", "prs": [],
        }
        base_state.update(state or {})
        (run / "state.json").write_text(json.dumps(base_state))
        prof = {"repo_path": "/tmp/repo", "base_branch": "develop",
                "max_review_rounds": "2", "auto_submit_human_needed": "false"}
        prof.update(profile or {})
        (run / "profile.yaml").write_text("".join(f"{k}: {v}\n" for k, v in prof.items()))
        # Default: past build/analyze/clean so tests focus on the task loop.
        (run / "build.exit").write_text("0\n")
        (run / "tasks.json").write_text(json.dumps([{"id": "t1"}]))
        (run / "clean-checked").write_text("")
        return run

    def _next(self, run: Path) -> dict:
        return orchestrate.compute_next(run)

    def _attempt(self, run: Path, n: int) -> None:
        (run / "tasks" / "t1" / f"attempt-{n}.diff").parent.mkdir(parents=True, exist_ok=True)
        (run / "tasks" / "t1" / f"attempt-{n}.diff").write_text("diff")

    def _write(self, run: Path, name: str, obj: object) -> None:
        d = run / "tasks" / "t1"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(obj))

    def test_build_then_analyze_then_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            (run / "tasks").mkdir(parents=True)
            (run / "state.json").write_text(json.dumps({"phase": "init"}))
            (run / "profile.yaml").write_text("repo_path: /tmp/repo\n")
            self.assertEqual(self._next(run)["label"], "build")
            (run / "build.exit").write_text("0\n")
            self.assertEqual(self._next(run)["agent"], "auto-pr-error-analyzer")
            (run / "tasks.json").write_text("[]")
            self.assertEqual(self._next(run)["label"], "load-tasks")

    def test_clean_check_runs_once_then_dirty_ends_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            (run / "clean-checked").unlink()
            self.assertEqual(self._next(run)["label"], "check-clean")
            (run / "dirty-tree.json").write_text('{"reason":"dirty-working-tree"}')
            self.assertEqual(self._next(run)["action"], "done")

    def test_coder_then_reviewer_then_triage_then_final_then_submit(self) -> None:
        # Regression #6: the happy path still reaches submitter then finish-pr,
        # using the round-scoped triage-<n>.json / final-review-<n>.json names.
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            self.assertEqual(self._next(run)["agent"], "auto-pr-coder")
            self._attempt(run, 1)
            self.assertEqual(self._next(run)["agent"], "auto-pr-reviewer")
            self._write(run, "review-1.json", {"approved": True, "verdict": "approve"})
            self.assertEqual(self._next(run)["agent"], "auto-pr-triage")
            self._write(run, "triage-1.json", {"needs_human": False})
            self.assertEqual(self._next(run)["agent"], "auto-pr-final-reviewer")
            self._write(run, "final-review-1.json", {"verdict": "approve"})
            self.assertEqual(self._next(run)["agent"], "auto-pr-pr-submitter")
            self._write(run, "pr.json", {"number": 1})
            self.assertEqual(self._next(run)["label"], "record PR t1")

    def test_reviewer_request_changes_spawns_next_coder_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            self._attempt(run, 1)
            self._write(run, "review-1.json", {"approved": False, "verdict": "request_changes"})
            action = self._next(run)
            self.assertEqual(action["agent"], "auto-pr-coder")
            self.assertIn("r2", action["label"])

    def test_reviewer_stuck_after_max_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp, profile={"max_review_rounds": "2"})
            self._attempt(run, 1)
            self._attempt(run, 2)
            self._write(run, "review-2.json", {"approved": False, "verdict": "request_changes"})
            action = self._next(run)
            self.assertEqual(action["action"], "run")
            self.assertIn("advance_task.py", action["cmd"])
            self.assertIn("stuck", action["cmd"])

    def test_final_review_request_changes_reenters_without_deletion(self) -> None:
        # Regression #2: final-review request_changes with no attempt-2 yet routes
        # to coder r2 (not triage), and never deletes the round-1 artifacts. A
        # crashed coder (still no attempt-2) must keep routing to coder r2.
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp, profile={"max_review_rounds": "3"})
            self._attempt(run, 1)
            self._write(run, "review-1.json", {"approved": True, "verdict": "approve"})
            self._write(run, "triage-1.json", {"needs_human": False})
            self._write(run, "final-review-1.json", {"verdict": "request_changes"})
            action = self._next(run)
            self.assertEqual(action["agent"], "auto-pr-coder")
            self.assertIn("r2", action["label"])
            # No deletion: round-1 triage/final-review survive for the coder + audit.
            self.assertTrue((run / "tasks" / "t1" / "triage-1.json").exists())
            self.assertTrue((run / "tasks" / "t1" / "final-review-1.json").exists())
            # Coder crashed without writing attempt-2 → still coder r2, not triage.
            replay = self._next(run)
            self.assertEqual(replay["agent"], "auto-pr-coder")
            self.assertIn("r2", replay["label"])

    def test_invalid_or_missing_final_verdict_routes_to_human_review(self) -> None:
        # Regression #1: a missing / typo'd / out-of-enum / unparseable final
        # verdict must route to human-review-needed, never to the submitter.
        for bad in ({"verdict": "approved_typo"}, {}, {"verdict": "block"}):
            with tempfile.TemporaryDirectory() as tmp:
                run = self._make_run(tmp)
                self._attempt(run, 1)
                self._write(run, "review-1.json", {"approved": True, "verdict": "approve"})
                self._write(run, "triage-1.json", {"needs_human": False})
                self._write(run, "final-review-1.json", bad)
                action = self._next(run)
                self.assertEqual(action.get("action"), "run", bad)
                self.assertIn("human-review-needed", action["cmd"], bad)
                self.assertNotIn("auto-pr-pr-submitter", json.dumps(action), bad)
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            self._attempt(run, 1)
            self._write(run, "review-1.json", {"approved": True, "verdict": "approve"})
            self._write(run, "triage-1.json", {"needs_human": False})
            (run / "tasks" / "t1" / "final-review-1.json").write_text("{ not json")
            action = self._next(run)
            self.assertEqual(action.get("action"), "run")
            self.assertIn("human-review-needed", action["cmd"])

    def test_inconsistent_approved_review_treated_as_not_approved(self) -> None:
        # Regression #5: approved:true but verdict:request_changes is NOT approved.
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp, profile={"max_review_rounds": "2"})
            self._attempt(run, 1)
            self._write(run, "review-1.json", {"approved": True, "verdict": "request_changes"})
            action = self._next(run)
            self.assertEqual(action["agent"], "auto-pr-coder")  # rounds remain → coder
            self.assertIn("r2", action["label"])
            self.assertFalse((run / "tasks" / "t1" / "triage-1.json").exists())
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp, profile={"max_review_rounds": "1"})
            self._attempt(run, 1)
            self._write(run, "review-1.json", {"approved": True, "verdict": "request_changes"})
            action = self._next(run)  # no rounds left → stuck, never triage/submit
            self.assertEqual(action["action"], "run")
            self.assertIn("stuck", action["cmd"])

    def test_final_review_block_records_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            self._attempt(run, 1)
            self._write(run, "review-1.json", {"approved": True, "verdict": "approve"})
            self._write(run, "triage-1.json", {"needs_human": False})
            self._write(run, "final-review-1.json", {"verdict": "block"})
            action = self._next(run)
            self.assertEqual(action["action"], "run")
            self.assertIn("advance_task.py", action["cmd"])
            self.assertIn("human-review-needed", action["cmd"])

    def test_triage_needs_human_blocks_submit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp, profile={"auto_submit_human_needed": "false"})
            self._attempt(run, 1)
            self._write(run, "review-1.json", {"approved": True, "verdict": "approve"})
            self._write(run, "triage-1.json", {"needs_human": True})
            self._write(run, "final-review-1.json", {"verdict": "approve"})
            action = self._next(run)
            self.assertIn("human-review-needed", action["cmd"])

    def test_pr_error_routes_to_human_review_not_resubmit(self) -> None:
        # Regression #3: a prior failed submit (pr-error.json, no pr.json) must
        # route to human-review, not re-spawn the submitter.
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            self._attempt(run, 1)
            self._write(run, "review-1.json", {"approved": True, "verdict": "approve"})
            self._write(run, "triage-1.json", {"needs_human": False})
            self._write(run, "final-review-1.json", {"verdict": "approve"})
            self._write(run, "pr-error.json", {"reason": "submit-failed", "exit": 1})
            action = self._next(run)
            self.assertEqual(action["action"], "run")
            self.assertIn("human-review-needed", action["cmd"])
            self.assertIn("submit failed", action["cmd"])

    def test_verify_gate_blocks_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp, profile={"verify_cmd": "false"})
            self._attempt(run, 1)
            self._write(run, "review-1.json", {"approved": True, "verdict": "approve"})
            self._write(run, "triage-1.json", {"needs_human": False})
            self._write(run, "final-review-1.json", {"verdict": "approve"})
            self.assertEqual(self._next(run)["label"], "verify t1")
            self._write(run, "verify-1.json", {"passed": False})
            action = self._next(run)
            self.assertIn("advance_task.py", action["cmd"])
            self.assertIn("stuck", action["cmd"])

    def test_abandon_advances_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            self._write(run, "abandon.json", {"reason": "scope-too-large"})
            action = self._next(run)
            self.assertIn("advance_task.py", action["cmd"])
            self.assertIn("abandoned", action["cmd"])

    def test_advance_task_stuck_is_idempotent_on_replay(self) -> None:
        # Regression #4: replaying advance_task.py stuck must bump the counters
        # exactly once (dedupe by task_id, like state.sh finish-pr by number).
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            (run / "tasks" / "t1").mkdir(parents=True, exist_ok=True)

            def advance() -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [str(LIB / "advance_task.py"), str(run), "t1", "stuck", "--detail", "boom"],
                    text=True, capture_output=True, check=False,
                )

            first = advance()
            self.assertEqual(first.returncode, 0, first.stderr)
            st = json.loads((run / "state.json").read_text())
            self.assertEqual((st["task_index"], st["tasks_stuck"], st["tasks_done"]), (1, 1, 1))
            self.assertEqual(len(st["skips"]), 1)

            replay = advance()
            self.assertEqual(replay.returncode, 0, replay.stderr)
            st2 = json.loads((run / "state.json").read_text())
            self.assertEqual((st2["task_index"], st2["tasks_stuck"], st2["tasks_done"]), (1, 1, 1))
            self.assertEqual(len(st2["skips"]), 1)

    def test_done_when_all_tasks_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp, state={"task_index": 1, "task_ids": ["t1"]})
            self.assertEqual(self._next(run)["action"], "done")


class SmokeTests(unittest.TestCase):
    """Mocked end-to-end driver runs. Fake every spawn by writing the artifact a
    real subagent would (using the round-scoped names), run every run/done cmd
    verbatim via bash, and loop until the driver says done. Asserts a PR is
    recorded and the task index advances — the full resumable pipeline on disk."""

    def _run_lib(self, script: str, *args: str, stdin: str | None = None,
                 env: dict | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(LIB / script), *args], input=stdin, text=True,
                              capture_output=True, check=False, env=env)

    def _setup(self, tmp: str, *, max_rounds: int = 3) -> tuple[Path, dict]:
        root = Path(tmp)
        repo = root / "repo"
        config = root / "config"
        repo.mkdir()
        self.assertEqual(subprocess.run(["git", "init", str(repo)], capture_output=True).returncode, 0)

        build = root / "fake_build.sh"
        build.write_text("#!/usr/bin/env bash\necho 'build ran' >&2\nexit 0\n", encoding="utf-8")
        os.chmod(build, 0o755)

        prof_dir = config / "auto-pr" / "projects"
        prof_dir.mkdir(parents=True)
        (prof_dir / "smoke.yaml").write_text(
            "name: smoke\n"
            f"repo_path: {repo}\n"
            "base_branch: main\n"
            "branch_prefix: auto-pr/\n"
            f"build_cmd: {build}\n"
            "build_args:\n"
            f"max_review_rounds: {max_rounds}\n"
            "auto_submit_human_needed: false\n"
            "push_remote: origin\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(config)
        env["HOME"] = str(config / "home")

        init = self._run_lib("init_run.sh", "smoke", env=env)
        self.assertEqual(init.returncode, 0, init.stderr)
        return Path(init.stdout.strip()), env

    @staticmethod
    def _round(prompt: str) -> int:
        m = re.search(r"round=(\d+)", prompt)
        return int(m.group(1)) if m else 1

    def _fake_spawn(self, run: Path, env: dict, action: dict, final_verdicts: dict[int, str]) -> None:
        agent = action["agent"]
        prompt = action["prompt"]
        tdir = run / "tasks" / "t1"
        if agent == "auto-pr-error-analyzer":
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / "task.md").write_text("# t1\nboom\n", encoding="utf-8")
            task = {
                "id": "t1", "fingerprint": "fp", "status": "failed", "exception": None,
                "n_failures": 1, "tests": ["test_a"], "rep_file": "src/foo.c", "rep_line": 10,
                "files": ["src/foo.c"], "score": 1.0, "summary": "boom",
            }
            (run / "tasks.json").write_text(json.dumps([task]), encoding="utf-8")
        elif agent == "auto-pr-coder":
            r = self._round(prompt)
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / f"attempt-{r}.diff").write_text(f"diff round {r}\n", encoding="utf-8")
            (tdir / "branch").write_text("auto-pr/t1\n", encoding="utf-8")
        elif agent == "auto-pr-reviewer":
            r = self._round(prompt)
            payload = json.dumps({"round": r, "approved": True, "verdict": "approve",
                                  "needs_human": False, "blocking": [], "suggestions": []})
            res = self._run_lib("write_artifact.py", "review", str(run), "t1",
                                "--round", str(r), stdin=payload, env=env)
            self.assertEqual(res.returncode, 0, res.stderr)
        elif agent == "auto-pr-triage":
            r = self._round(prompt)
            payload = json.dumps({"needs_human": False, "reasons": [],
                                  "diff_stats": {"files_changed": 1, "insertions": 1, "deletions": 0},
                                  "matched_paths": []})
            res = self._run_lib("write_artifact.py", "triage", str(run), "t1",
                                "--round", str(r), stdin=payload, env=env)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue((tdir / f"triage-{r}.json").exists())
        elif agent == "auto-pr-final-reviewer":
            r = self._round(prompt)
            if final_verdicts.get(r, "approve") == "approve":
                payload = json.dumps({"verdict": "approve", "needs_human": False, "concerns": [],
                                      "agreed_with_reviewer": True, "agreed_with_triage": True})
            else:
                payload = json.dumps({"verdict": "request_changes", "needs_human": False,
                                      "concerns": [{"dimension": "correctness", "message": "tighten the fix"}],
                                      "agreed_with_reviewer": False, "agreed_with_triage": True})
            res = self._run_lib("write_artifact.py", "final-review", str(run), "t1",
                                "--round", str(r), stdin=payload, env=env)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue((tdir / f"final-review-{r}.json").exists())
        elif agent == "auto-pr-pr-submitter":
            (tdir / "pr.json").write_text(json.dumps(
                {"url": "https://example.test/pr/1", "number": 1,
                 "branch": "auto-pr/t1", "needs_human": False}), encoding="utf-8")
        else:  # pragma: no cover - guard
            self.fail(f"unexpected agent spawn: {agent}")

    def _drive(self, run: Path, env: dict, *, final_verdicts: dict[int, str]) -> list[str]:
        transcript: list[str] = []
        for _ in range(60):
            nxt = subprocess.run([str(LIB / "orchestrate.py"), "next", str(run)],
                                 text=True, capture_output=True, check=False, env=env)
            self.assertEqual(nxt.returncode, 0, nxt.stderr)
            action = json.loads(nxt.stdout.strip())
            transcript.append(action["label"])
            kind = action["action"]
            if kind == "spawn":
                self._fake_spawn(run, env, action, final_verdicts)
            elif kind in ("run", "done"):
                res = subprocess.run(["bash", "-c", action["cmd"]], text=True,
                                     capture_output=True, check=False, env=env)
                self.assertEqual(res.returncode, 0, f"cmd failed: {action['cmd']}\n{res.stderr}")
                if kind == "done":
                    return transcript
            else:  # pragma: no cover - guard
                self.fail(f"unknown action kind: {kind}")
        self.fail("driver did not reach 'done' within the iteration budget")

    def test_happy_path_reaches_pr_and_advances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run, env = self._setup(tmp)
            transcript = self._drive(run, env, final_verdicts={1: "approve"})
            state = json.loads((run / "state.json").read_text())
            self.assertEqual(state["phase"], "done")
            self.assertEqual(len(state["prs"]), 1)
            self.assertEqual(state["prs"][0]["number"], 1)
            self.assertEqual(state["task_index"], 1)
            self.assertEqual(state["tasks_done"], 1)
            self.assertEqual(state["tasks_total"], 1)
            self.assertIn("submit t1", transcript)
            self.assertIn("record PR t1", transcript)
            self.assertTrue((run / "tasks" / "t1" / "triage-1.json").exists())
            self.assertTrue((run / "tasks" / "t1" / "final-review-1.json").exists())

    def test_final_review_request_changes_then_approve_submits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run, env = self._setup(tmp, max_rounds=3)
            transcript = self._drive(run, env, final_verdicts={1: "request_changes", 2: "approve"})
            state = json.loads((run / "state.json").read_text())
            self.assertEqual(state["phase"], "done")
            self.assertEqual(len(state["prs"]), 1)
            self.assertEqual(state["task_index"], 1)
            # The re-entry produced round-2 artifacts; round-1 ones were never deleted.
            t = run / "tasks" / "t1"
            self.assertTrue((t / "final-review-1.json").exists())
            self.assertTrue((t / "final-review-2.json").exists())
            self.assertTrue((t / "attempt-2.diff").exists())
            self.assertIn("submit t1", transcript)
            self.assertIn("record PR t1", transcript)


if __name__ == "__main__":
    unittest.main()
