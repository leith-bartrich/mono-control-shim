"""Tests for the two-layer health check and the startup watchdog.

Stdlib ``unittest``, with docker itself mocked — unlike ``test_verbs_git.py``, the
thing under test here *is* the shim's reaction to what docker reports, so a fake
docker is the point rather than a compromise. The wedge these guard against is
reproducible only by a daemon that accepts a container and never starts it, which
no real local docker will do on demand.

    python -m unittest discover -s tests -t .

The three behaviors and why each matters:

* **Layer 1 claims only what it tested.** A ``docker info`` that answers proves the
  socket is alive and nothing else. The old wording said "available", so a wedged
  engine got a green light and the hunt for the cause started in the wrong repo.
* **Layer 2 (`doctor`) runs a real container**, bounded by a clock, because that is
  the only thing that distinguishes a healthy engine from a wedged one.
* **The watchdog hints and never kills.** A long `control` session or a slow
  `test-control` is legitimate; a diagnostic that interrupted one would be worse
  than the silence it replaces.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mono_control_shim import cli

DOCKER = "/usr/bin/docker"


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        return subprocess.CompletedProcess(
            args=command, returncode=returncode, stdout=stdout, stderr=stderr
        )

    return fake_run


def _capture(fn, *args, **kwargs) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*args, **kwargs)
    return code, out.getvalue(), err.getvalue()


class Layer1ClaimsOnlyWhatItTested(unittest.TestCase):
    """The wording bug that made a wedged engine look healthy."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        (self.workspace / "mono-control" / ".devcontainer").mkdir(parents=True)

    def test_a_reachable_daemon_is_never_called_available(self) -> None:
        with mock.patch.object(cli.shutil, "which", return_value=DOCKER):
            with mock.patch.object(cli.subprocess, "run", _completed(0)):
                code, out, _ = _capture(cli._run_status, self.workspace)

        self.assertEqual(code, 0)
        self.assertIn("reachable", out)
        self.assertNotIn("available", out)

    def test_it_points_at_doctor_for_the_deeper_answer(self) -> None:
        with mock.patch.object(cli.shutil, "which", return_value=DOCKER):
            with mock.patch.object(cli.subprocess, "run", _completed(0)):
                _, out, _ = _capture(cli._run_status, self.workspace)

        self.assertIn("mproj doctor", out)
        self.assertIn("actually start", out)

    def test_layer_1_never_starts_a_container(self) -> None:
        """The whole reason it is the cheap layer."""
        seen: list[list[str]] = []

        def spy(command, **kwargs):  # noqa: ANN001, ANN003
            seen.append(command)
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="")

        with mock.patch.object(cli.shutil, "which", return_value=DOCKER):
            with mock.patch.object(cli.subprocess, "run", spy):
                cli._docker_reachability()

        self.assertTrue(seen)
        for command in seen:
            self.assertNotIn("run", command, "layer 1 must not start containers")

    def test_an_unresponsive_daemon_is_reported_unreachable(self) -> None:
        def timeout(command, **kwargs):  # noqa: ANN001, ANN003
            raise subprocess.TimeoutExpired(cmd=command, timeout=10)

        with mock.patch.object(cli.shutil, "which", return_value=DOCKER):
            with mock.patch.object(cli.subprocess, "run", timeout):
                reachable, detail = cli._docker_reachability()

        self.assertFalse(reachable)
        self.assertIn("not responding", detail)

    def test_a_bounded_probe_is_used(self) -> None:
        """An unbounded probe would hang exactly where a bound is most needed."""
        seen: list[dict] = []

        def spy(command, **kwargs):  # noqa: ANN001, ANN003
            seen.append(kwargs)
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="")

        with mock.patch.object(cli.shutil, "which", return_value=DOCKER):
            with mock.patch.object(cli.subprocess, "run", spy):
                cli._docker_reachability()

        self.assertTrue(all(k.get("timeout") for k in seen))


class ReachabilityIsAboutTheHostAlone(unittest.TestCase):
    """The regression: a workspace with no checkout is not a broken docker.

    Reachability once folded in "is there a `mono-control/` checkout with a
    `.devcontainer`", so an artifact-only workspace — the ordinary way to *consume*
    the tool — printed `docker: unreachable` while `mproj control` ran fine in it.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)  # no mono-control/ at all

    def test_reachability_takes_no_workspace_at_all(self) -> None:
        """It cannot conflate the two if it cannot see the workspace."""
        import inspect

        params = inspect.signature(cli._docker_reachability).parameters
        self.assertEqual(list(params), [])

    def test_an_artifact_only_workspace_reports_docker_reachable(self) -> None:
        with mock.patch.object(cli.shutil, "which", return_value=DOCKER):
            with mock.patch.object(cli.subprocess, "run", _completed(0)):
                code, out, _ = _capture(cli._run_status, self.workspace)

        self.assertEqual(code, 0)
        self.assertIn("docker: reachable", out)
        self.assertNotIn("docker: unreachable", out)

    def test_a_missing_checkout_is_not_reported_as_a_docker_problem(self) -> None:
        with mock.patch.object(cli.shutil, "which", return_value=DOCKER):
            with mock.patch.object(cli.subprocess, "run", _completed(0)):
                _, out, _ = _capture(cli._run_status, self.workspace)

        docker_line = next(ln for ln in out.splitlines() if ln.startswith("docker:"))
        self.assertNotIn("devcontainer", docker_line)
        self.assertNotIn("mono-control", docker_line)


class ModeIsReportedAsInformation(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)

    def _make_checkout(self, *, compose: bool = True) -> None:
        devcontainer = self.workspace / "mono-control" / ".devcontainer"
        devcontainer.mkdir(parents=True)
        if compose:
            (devcontainer / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    def test_a_checkout_selects_dev_mode(self) -> None:
        self._make_checkout()
        self.assertEqual(cli._selected_mode(self.workspace), cli.MODE_DEV)

    def test_no_checkout_selects_artifact_mode(self) -> None:
        self.assertEqual(cli._selected_mode(self.workspace), cli.MODE_ARTIFACT)

    def test_a_local_image_never_beats_a_checkout(self) -> None:
        """`_dispatch` never consults the image; the report must not either."""
        self._make_checkout()
        seen: list[list[str]] = []

        def spy(command, **kwargs):  # noqa: ANN001, ANN003
            seen.append(command)
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="")

        with mock.patch.object(cli.subprocess, "run", spy):
            mode, ready, _ = cli._mode_status(self.workspace, DOCKER)

        self.assertEqual(mode, cli.MODE_DEV)
        self.assertTrue(ready)
        self.assertEqual(seen, [], "dev mode must not probe the artifact image")

    def test_dev_mode_is_reported_in_status(self) -> None:
        self._make_checkout()
        with mock.patch.object(cli.shutil, "which", return_value=DOCKER):
            with mock.patch.object(cli.subprocess, "run", _completed(0)):
                _, out, _ = _capture(cli._run_status, self.workspace)

        self.assertIn(f"mode: {cli.MODE_DEV}", out)

    def test_artifact_mode_is_reported_in_status(self) -> None:
        with mock.patch.object(cli.shutil, "which", return_value=DOCKER):
            with mock.patch.object(cli.subprocess, "run", _completed(0)):
                _, out, _ = _capture(cli._run_status, self.workspace)

        self.assertIn(f"mode: {cli.MODE_ARTIFACT}", out)

    def test_neither_mode_is_a_failure(self) -> None:
        """Having no checkout is the ordinary way to consume the tool."""
        for make in (lambda: None, self._make_checkout):
            with self.subTest(make=make):
                make()
                with mock.patch.object(cli.subprocess, "run", _completed(0)):
                    _, ready, _ = cli._mode_status(self.workspace, DOCKER)
                self.assertTrue(ready)

    def test_a_checkout_without_a_compose_file_is_not_ready(self) -> None:
        """Dev mode would error here, so the report must not call it fine."""
        self._make_checkout(compose=False)

        mode, ready, detail = cli._mode_status(self.workspace, DOCKER)

        self.assertEqual(mode, cli.MODE_DEV)
        self.assertFalse(ready)
        self.assertIn("docker-compose.yml", detail)

    def test_artifact_mode_without_an_image_is_not_ready(self) -> None:
        def no_image(command, **kwargs):  # noqa: ANN001, ANN003
            rc = 1 if "image" in command else 0
            return subprocess.CompletedProcess(args=command, returncode=rc, stdout="")

        with mock.patch.object(cli.subprocess, "run", no_image):
            mode, ready, detail = cli._mode_status(self.workspace, DOCKER)

        self.assertEqual(mode, cli.MODE_ARTIFACT)
        self.assertFalse(ready)
        self.assertIn("build-control", detail)

    def test_an_unreachable_daemon_does_not_claim_the_image_is_there(self) -> None:
        mode, ready, detail = cli._mode_status(self.workspace, None)

        self.assertEqual(mode, cli.MODE_ARTIFACT)
        self.assertTrue(ready)
        self.assertIn("would run", detail)

    def test_status_is_a_report_and_always_exits_zero(self) -> None:
        """`doctor` owns the verdict; this line stays cheap and descriptive."""
        with mock.patch.object(cli.shutil, "which", return_value=None):
            code, _, _ = _capture(cli._run_status, self.workspace)

        self.assertEqual(code, 0)


class DoctorRunsALiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        for name in cli.INIT_DIRS:
            (self.workspace / name).mkdir()
        (self.workspace / cli.WORKSPACE_MARKER / ".git").mkdir()

    def _run(self, dispatch) -> tuple[int, str, str]:
        with mock.patch.object(cli.shutil, "which", return_value=DOCKER):
            with mock.patch.object(cli.subprocess, "run", dispatch):
                return _capture(cli._run_doctor, self.workspace)

    def test_a_healthy_engine_passes_every_check(self) -> None:
        code, out, _ = self._run(_completed(0))

        self.assertEqual(code, 0, out)
        self.assertNotIn("FAIL", out)
        self.assertIn("live container test", out)

    def test_the_live_test_actually_starts_a_container(self) -> None:
        seen: list[list[str]] = []

        def spy(command, **kwargs):  # noqa: ANN001, ANN003
            seen.append(command)
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="")

        self._run(spy)

        runs = [c for c in seen if "run" in c]
        self.assertTrue(runs, "doctor must actually start a container")
        self.assertTrue(all("--rm" in c for c in runs), "the test container must clean up")

    def test_a_wedged_engine_is_caught_and_named(self) -> None:
        """`info` answers, the container never starts — the exact failure seen."""

        def wedged(command, **kwargs):  # noqa: ANN001, ANN003
            if "run" in command:
                raise subprocess.TimeoutExpired(cmd=command, timeout=30)
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="")

        code, out, _ = self._run(wedged)

        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn("did not start", out)
        self.assertIn("wsl --shutdown", out, "the fix should be named, not just the fault")

    def test_the_live_test_is_bounded(self) -> None:
        seen: list[dict] = []

        def spy(command, **kwargs):  # noqa: ANN001, ANN003
            if "run" in command:
                seen.append(kwargs)
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="")

        self._run(spy)

        self.assertTrue(seen)
        self.assertTrue(all(k.get("timeout") for k in seen))

    def test_a_missing_image_is_a_finding_not_a_pull(self) -> None:
        seen: list[list[str]] = []

        def no_image(command, **kwargs):  # noqa: ANN001, ANN003
            seen.append(command)
            rc = 1 if "image" in command else 0
            return subprocess.CompletedProcess(args=command, returncode=rc, stdout="")

        code, out, _ = self._run(no_image)

        self.assertEqual(code, 1)
        self.assertIn("build-control", out)
        for command in seen:
            self.assertNotIn("pull", command, "doctor must stay offline")

    def test_it_reports_which_mode_the_workspace_will_use(self) -> None:
        _, out, _ = self._run(_completed(0))

        self.assertIn("mode:", out)
        self.assertIn(cli.MODE_ARTIFACT, out)

    def test_a_dev_workspace_is_reported_as_dev(self) -> None:
        devcontainer = self.workspace / "mono-control" / ".devcontainer"
        devcontainer.mkdir(parents=True)
        (devcontainer / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

        code, out, _ = self._run(_completed(0))

        self.assertEqual(code, 0, out)
        self.assertIn(cli.MODE_DEV, out)

    def test_dev_mode_is_not_failed_over_a_missing_artifact_image(self) -> None:
        """Dev mode never runs that image; failing over it would be a false alarm."""
        devcontainer = self.workspace / "mono-control" / ".devcontainer"
        devcontainer.mkdir(parents=True)
        (devcontainer / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

        def no_image(command, **kwargs):  # noqa: ANN001, ANN003
            rc = 1 if "image" in command else 0
            return subprocess.CompletedProcess(args=command, returncode=rc, stdout="")

        code, out, _ = self._run(no_image)

        self.assertEqual(code, 0, out)
        self.assertIn("skipped", out)
        self.assertIn("does not use it", out)

    def test_artifact_mode_still_fails_over_a_missing_image(self) -> None:
        """Same missing image, opposite verdict — artifact mode cannot run at all."""

        def no_image(command, **kwargs):  # noqa: ANN001, ANN003
            rc = 1 if "image" in command else 0
            return subprocess.CompletedProcess(args=command, returncode=rc, stdout="")

        code, out, _ = self._run(no_image)

        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)

    def test_a_missing_docker_cli_stops_before_the_daemon_check(self) -> None:
        with mock.patch.object(cli.shutil, "which", return_value=None):
            code, out, _ = _capture(cli._run_doctor, self.workspace)

        self.assertEqual(code, 1)
        self.assertIn("not found on PATH", out)

    def test_an_unfindable_workspace_is_a_finding_not_a_crash(self) -> None:
        with mock.patch.object(cli.shutil, "which", return_value=DOCKER):
            with mock.patch.object(cli.subprocess, "run", _completed(0)):
                code, out, _ = _capture(cli._run_doctor, None)

        self.assertEqual(code, 1)
        self.assertIn("mproj init", out)
        self.assertIn("docker:", out, "docker is still diagnosed without a workspace")

    def test_workspace_gaps_are_reported(self) -> None:
        (self.workspace / "mono-work").rmdir()

        code, out, _ = self._run(_completed(0))

        self.assertEqual(code, 1)
        self.assertIn("mono-work", out)
        self.assertIn("FAIL", out)

    def test_an_empty_config_dir_is_reported(self) -> None:
        (self.workspace / cli.WORKSPACE_MARKER / ".git").rmdir()

        code, out, _ = self._run(_completed(0))

        self.assertEqual(code, 1)
        self.assertIn("--config-url", out)


class StartupWatchdogHintsButNeverKills(unittest.TestCase):
    def test_it_hints_when_a_container_is_stuck_created(self) -> None:
        with mock.patch.object(cli.subprocess, "run", _completed(0, stdout="abc123\n")):
            self.assertTrue(cli._containers_stuck_created(DOCKER))

    def test_it_is_silent_when_nothing_is_stuck(self) -> None:
        with mock.patch.object(cli.subprocess, "run", _completed(0, stdout="")):
            self.assertFalse(cli._containers_stuck_created(DOCKER))

    def test_a_docker_query_that_hangs_counts_as_the_symptom(self) -> None:
        """A daemon too wedged to answer `ps` is exactly what is worth reporting."""

        def timeout(command, **kwargs):  # noqa: ANN001, ANN003
            raise subprocess.TimeoutExpired(cmd=command, timeout=10)

        with mock.patch.object(cli.subprocess, "run", timeout):
            self.assertTrue(cli._containers_stuck_created(DOCKER))

    def test_a_run_that_finishes_in_time_says_nothing(self) -> None:
        done = cli.threading.Event()
        done.set()  # the run already completed

        def explode(*_args, **_kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("a finished run must not be probed")

        with mock.patch.object(cli, "_containers_stuck_created", explode):
            _, _, err = _capture(cli._watch_startup, DOCKER, done)

        self.assertEqual(err, "")

    def test_the_hint_names_doctor_and_says_nothing_was_cancelled(self) -> None:
        done = cli.threading.Event()

        with mock.patch.object(cli, "_STARTUP_HINT_AFTER", 0.0):
            with mock.patch.object(cli, "_containers_stuck_created", lambda _d: True):
                _, _, err = _capture(cli._watch_startup, DOCKER, done)

        self.assertIn("mproj doctor", err)
        self.assertIn("nothing has been cancelled", err)

    def test_a_running_container_produces_no_hint(self) -> None:
        done = cli.threading.Event()

        with mock.patch.object(cli, "_STARTUP_HINT_AFTER", 0.0):
            with mock.patch.object(cli, "_containers_stuck_created", lambda _d: False):
                _, _, err = _capture(cli._watch_startup, DOCKER, done)

        self.assertEqual(err, "")

    def test_the_watchdog_never_touches_the_child_exit_code(self) -> None:
        """Its entire power is one line on stderr."""
        with mock.patch.object(cli.subprocess, "run", _completed(42)):
            code = cli._exec([DOCKER, "run", "x"], watch_startup=True)

        self.assertEqual(code, 42)

    def test_exec_without_the_watchdog_starts_no_thread(self) -> None:
        def explode(*_args, **_kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("the watchdog must be opt-in")

        with mock.patch.object(cli.threading, "Thread", explode):
            with mock.patch.object(cli.subprocess, "run", _completed(0)):
                code = cli._exec([DOCKER, "build", "."])

        self.assertEqual(code, 0)

    def test_container_runs_arm_the_watchdog(self) -> None:
        armed: list[bool] = []
        real_exec = cli._exec

        def spy(cmd, **kwargs):  # noqa: ANN001, ANN003
            armed.append(bool(kwargs.get("watch_startup")))
            return real_exec(cmd, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            compose = workspace / "mono-control" / ".devcontainer"
            compose.mkdir(parents=True)
            (compose / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            with mock.patch.object(cli, "_exec", spy):
                with mock.patch.object(cli.subprocess, "run", _completed(0)):
                    with contextlib.redirect_stderr(io.StringIO()):
                        cli._dev_run(DOCKER, workspace, ["mono-control"])

        self.assertEqual(armed, [True])


class OutputStaysAscii(unittest.TestCase):
    """Windows consoles and redirected output encode with the locale codepage.

    A non-ASCII character in a *printed* string turns into mojibake (or an encode
    error) the moment someone pipes `mproj` to a file, so runtime strings stay
    ASCII. Docstrings and comments are exempt — they are never written to a stream.
    """

    def test_no_runtime_string_carries_non_ascii(self) -> None:
        import ast

        source = Path(cli.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        docstring_lines = set()
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                if ast.get_docstring(node, clean=False) is not None and node.body:
                    docstring_lines.add(node.body[0].value.lineno)

        offenders = [
            (node.lineno, node.value[:60])
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.lineno not in docstring_lines
            and any(not ch.isascii() for ch in node.value)
        ]

        self.assertEqual(offenders, [], f"non-ASCII in runtime strings: {offenders}")


if __name__ == "__main__":
    unittest.main()
