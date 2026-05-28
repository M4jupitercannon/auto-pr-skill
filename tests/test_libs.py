#!/usr/bin/env python3
from __future__ import annotations

import json
import os
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


if __name__ == "__main__":
    unittest.main()
