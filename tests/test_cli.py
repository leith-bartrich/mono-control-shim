"""Tests for the shim.

Stdlib ``unittest``, deliberately: this repo's whole premise is a minimal trusted
surface on the host ("no third-party dependencies, ever"), and a test framework is a
dependency like any other. ``unittest`` costs nothing.

    python -m unittest discover -s tests -t .

The focus is the broker: its coordinates and per-run token reach the container
correctly, and no GitHub credential is resolved or forwarded anymore (host git owns
credentials now). The generic secret-plumbing is the one branching logic here with a
security consequence, so it must never be got wrong quietly.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mono_control_shim import cli

BROKER_TOKEN = "broker-per-run-token-example"


def _run_returning(stdout: str, returncode: int = 0):
    """Stub ``subprocess.run`` to return a fixed result (e.g. the image-inspect probe)."""

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        return subprocess.CompletedProcess(
            args=command, returncode=returncode, stdout=stdout, stderr=""
        )

    return fake_run


class SecretPlumbing(unittest.TestCase):
    """A secret (the broker token) must reach docker through the environment, never argv."""

    def test_secret_args_name_the_var_but_not_its_value(self) -> None:
        args = cli._secret_args({cli.BROKER_TOKEN_ENV: BROKER_TOKEN})

        self.assertEqual(args, ["-e", cli.BROKER_TOKEN_ENV])
        self.assertNotIn(BROKER_TOKEN, args)

    def test_no_secrets_means_no_flags(self) -> None:
        self.assertEqual(cli._secret_args({}), [])
        self.assertEqual(cli._secret_args(None), [])

    def test_secret_environ_carries_the_value_alongside_ours(self) -> None:
        with mock.patch.dict(os.environ, {"EXISTING": "kept"}, clear=True):
            env = cli._secret_environ({cli.BROKER_TOKEN_ENV: BROKER_TOKEN})

        assert env is not None
        self.assertEqual(env[cli.BROKER_TOKEN_ENV], BROKER_TOKEN)
        self.assertEqual(env["EXISTING"], "kept")  # inherited, not replaced

    def test_secret_environ_is_none_without_secrets(self) -> None:
        # None means "inherit ours", keeping the no-secret path exactly as it was.
        self.assertIsNone(cli._secret_environ({}))
        self.assertIsNone(cli._secret_environ(None))


class NoGitHubCredentialIsForwarded(unittest.TestCase):
    """The shim no longer resolves or forwards any GitHub credential.

    Git runs host-side as the developer, so host git owns credentials. The shim must
    build the ``HostContext`` with no token field, and never name a GitHub token to
    docker on either backend's command line or in the env handed to docker.
    """

    _GH_TOKEN_ENV = "MONO_CONTROL_GITHUB_TOKEN"

    def _capture(self, *, artifact: bool) -> tuple[list[str], dict[str, str] | None]:
        seen: dict = {}

        def fake_exec(cmd, *, env=None):  # noqa: ANN001
            seen["cmd"] = cmd
            seen["env"] = env
            return 0

        real_hostcontext = cli.HostContext

        def capturing_hostcontext(**kwargs):  # noqa: ANN003
            # The HostContext must not carry a github_token kwarg anymore.
            seen["host_kwargs"] = kwargs
            return real_hostcontext(**kwargs)

        workspace = mock.MagicMock(spec=cli.Path)
        workspace.__truediv__.return_value.is_dir.return_value = not artifact
        workspace.__truediv__.return_value.is_file.return_value = True

        # A clean environment: nothing about a GitHub token should appear anywhere,
        # neither injected into argv nor added to the env handed to docker. (docker
        # only forwards vars named with a `-e` flag, so a var never named is a var
        # never forwarded into the container.)
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(cli, "_exec", fake_exec):
                with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/docker"):
                    with mock.patch.object(cli, "_detect_host_platform", return_value="linux"):
                        with mock.patch.object(cli, "HostContext", capturing_hostcontext):
                            with mock.patch.object(cli.subprocess, "run", _run_returning("")):
                                cli._dispatch(
                                    workspace, ["mono-control", "repo", "list"],
                                    artifact=artifact,
                                )

        self.assertNotIn("github_token", seen["host_kwargs"])  # field is gone
        return seen["cmd"], seen["env"]

    def test_dev_mode_names_no_github_token_to_docker(self) -> None:
        cmd, env = self._capture(artifact=False)

        self.assertIn("compose", cmd)  # sanity: the dev branch
        self.assertNotIn(self._GH_TOKEN_ENV, cmd)  # not named as a -e flag
        assert env is not None
        self.assertNotIn(self._GH_TOKEN_ENV, env)  # ...nor added to docker's env

    def test_artifact_mode_names_no_github_token_to_docker(self) -> None:
        cmd, env = self._capture(artifact=True)

        self.assertIn("run", cmd)
        self.assertNotIn("compose", cmd)  # sanity: the artifact branch
        self.assertNotIn(self._GH_TOKEN_ENV, cmd)
        assert env is not None
        self.assertNotIn(self._GH_TOKEN_ENV, env)


class BrokerInjection(unittest.TestCase):
    """The broker's coordinates reach the container; its token does so as a secret."""

    def _capture(self, *, artifact: bool) -> tuple[list[str], dict[str, str] | None]:
        seen: dict = {}

        def fake_exec(cmd, *, env=None):  # noqa: ANN001
            seen["cmd"] = cmd
            seen["env"] = env
            return 0

        workspace = mock.MagicMock(spec=cli.Path)
        workspace.__truediv__.return_value.is_dir.return_value = not artifact
        workspace.__truediv__.return_value.is_file.return_value = True

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(cli, "_exec", fake_exec):
                with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/docker"):
                    with mock.patch.object(cli, "_detect_host_platform", return_value="linux"):
                        with mock.patch.object(cli, "_warn_if_workspace_incomplete", return_value=None):
                            with mock.patch.object(
                                cli.subprocess, "run", _run_returning("")
                            ):
                                cli._dispatch(workspace, ["mono-control"], artifact=artifact)

        return seen["cmd"], seen["env"]

    def test_host_and_port_ride_the_plain_env_path(self) -> None:
        cmd, _ = self._capture(artifact=False)
        joined = " ".join(cmd)

        self.assertIn(f"{cli.BROKER_HOST_ENV}={cli.BROKER_CONTAINER_HOST}", joined)
        # The port is ephemeral, so assert the shape rather than a value.
        port = next(a.split("=", 1)[1] for a in cmd if a.startswith(f"{cli.BROKER_PORT_ENV}="))
        self.assertTrue(port.isdigit() and int(port) > 0)

    def test_token_is_named_in_argv_but_valued_only_in_the_env(self) -> None:
        cmd, env = self._capture(artifact=False)

        self.assertIn(cli.BROKER_TOKEN_ENV, cmd)
        assert env is not None
        token = env[cli.BROKER_TOKEN_ENV]
        self.assertTrue(token)
        self.assertNotIn(token, " ".join(cmd))  # THE assertion, as for the GH token

    def test_artifact_mode_injects_the_same_way(self) -> None:
        cmd, env = self._capture(artifact=True)

        self.assertNotIn("compose", cmd)  # sanity: really the artifact branch
        self.assertIn(cli.BROKER_TOKEN_ENV, cmd)
        assert env is not None
        self.assertNotIn(env[cli.BROKER_TOKEN_ENV], " ".join(cmd))

    def test_artifact_mode_maps_host_docker_internal_for_linux_hosts(self) -> None:
        cmd, _ = self._capture(artifact=True)

        self.assertIn("--add-host", cmd)
        self.assertIn(f"{cli.BROKER_CONTAINER_HOST}:host-gateway", cmd)

    def test_a_broker_that_cannot_bind_only_warns(self) -> None:
        """Step 1 is additive: the command must still run without a broker."""
        seen: dict = {}

        def fake_exec(cmd, *, env=None):  # noqa: ANN001
            seen["cmd"] = cmd
            return 0

        def refuse_to_start(self) -> None:  # noqa: ANN001
            raise OSError("address already in use")

        workspace = mock.MagicMock(spec=cli.Path)
        workspace.__truediv__.return_value.is_dir.return_value = True
        workspace.__truediv__.return_value.is_file.return_value = True

        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(cli.BrokerServer, "start", refuse_to_start):
                with mock.patch.object(cli, "_exec", fake_exec):
                    with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/docker"):
                        with mock.patch.object(cli, "_detect_host_platform", return_value="linux"):
                            with mock.patch.object(cli, "_warn_if_workspace_incomplete", return_value=None):
                                with contextlib.redirect_stderr(stderr):
                                    rc = cli._dispatch(workspace, ["mono-control"])

        self.assertEqual(rc, 0)  # the command still ran
        self.assertIn("warning", stderr.getvalue().lower())
        self.assertNotIn(cli.BROKER_TOKEN_ENV, seen["cmd"])
        self.assertNotIn(cli.BROKER_PORT_ENV, " ".join(seen["cmd"]))


class WorkspaceDirsAreNotMounted(unittest.TestCase):
    """The managed workspace dirs are no longer bind-mounted into the container.

    The broker performs git/FS effects on those host paths directly, so the container
    no longer needs them mounted. What survives is the dev-mode live-source and uv-cache
    mounts, and the "run `mproj init`" hint when a required dir is missing.
    """

    def _dev_cmd(self, workspace: Path) -> tuple[list[str], str]:
        """Run a dev-mode dispatch against a real *workspace*, capturing cmd + stderr."""
        seen: dict = {}

        def fake_exec(cmd, *, env=None):  # noqa: ANN001
            seen["cmd"] = cmd
            return 0

        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(cli, "_exec", fake_exec):
                with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/docker"):
                    with mock.patch.object(cli, "_detect_host_platform", return_value="linux"):
                        with contextlib.redirect_stderr(stderr):
                            cli._dispatch(workspace, ["mono-control"])
        return seen["cmd"], stderr.getvalue()

    def _make_workspace(self, tmp: str, *, init_dirs: bool) -> Path:
        workspace = Path(tmp)
        compose = workspace / "mono-control" / ".devcontainer" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("services: {}\n", encoding="utf-8")
        if init_dirs:
            for name in cli.INIT_DIRS:
                (workspace / name).mkdir()
        return workspace

    def test_complete_workspace_mounts_only_source_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_workspace(tmp, init_dirs=True)
            cmd, stderr = self._dev_cmd(workspace)

        mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
        # The two dev-mode mounts survive...
        self.assertTrue(any(m.endswith(":/workspaces/mono-control") for m in mounts))
        self.assertTrue(any(m.endswith(":/home/codespace/.cache/uv") for m in mounts))
        # ...but none of the managed workspace dirs are mounted anymore.
        for name in cli.INIT_DIRS:
            self.assertFalse(
                any(m.endswith(f":/workspaces/{name}") for m in mounts),
                f"{name} should no longer be bind-mounted",
            )
        self.assertNotIn("warning", stderr.lower())  # nothing missing, nothing to warn

    def test_missing_workspace_dir_still_warns_but_does_not_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_workspace(tmp, init_dirs=True)
            # Drop one managed dir: the hint must fire, the mount must NOT reappear.
            (workspace / "mono-repos-offline").rmdir()
            cmd, stderr = self._dev_cmd(workspace)

        mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
        self.assertFalse(any(m.endswith(":/workspaces/mono-repos-offline") for m in mounts))
        self.assertIn("warning", stderr.lower())
        self.assertIn("mono-repos-offline", stderr)
        self.assertIn("mproj init", stderr)

    def test_warn_helper_is_silent_when_all_dirs_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            for name in cli.INIT_DIRS:
                (workspace / name).mkdir()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = cli._warn_if_workspace_incomplete(workspace)

        self.assertIsNone(result)  # pure existence check, contributes no -v flags
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
