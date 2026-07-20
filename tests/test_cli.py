"""Tests for the shim.

Stdlib ``unittest``, deliberately: this repo's whole premise is a minimal trusted
surface on the host ("no third-party dependencies, ever"), and a test framework is a
dependency like any other. ``unittest`` costs nothing.

    python -m unittest discover -s tests -t .

The focus is the GitHub token: it is the only branching logic in the shim with a
security consequence, and the one thing here that must never be got wrong quietly.
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

TOKEN = "ghp_scoped_readonly_example"


def _no_env() -> mock.mock._patch_dict:
    """An environment with none of the token variables set (and nothing else, either).

    ``clear=True`` is safe because every test here also stubs ``shutil.which``, so
    nothing consults the real PATH.
    """
    return mock.patch.dict(os.environ, {}, clear=True)


def _resolve_capturing_stderr() -> tuple[str | None, str]:
    """Call ``_resolve_github_token``, returning (token, whatever it wrote to stderr)."""
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        token = cli._resolve_github_token()
    return token, stderr.getvalue()


def _gh_returning(stdout: str, returncode: int = 0):
    """Stub ``subprocess.run`` as a `gh auth token` that prints *stdout*."""

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        return subprocess.CompletedProcess(
            args=command, returncode=returncode, stdout=stdout, stderr=""
        )

    return fake_run


class ResolveGitHubToken(unittest.TestCase):
    """Precedence: our var, then the ecosystem's, then `gh`, then nothing."""

    def test_explicit_var_wins_over_everything(self) -> None:
        env = {cli.GITHUB_TOKEN_ENV: TOKEN, "GH_TOKEN": "wrong", "GITHUB_TOKEN": "wrong"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/gh"):
                with mock.patch.object(cli.subprocess, "run") as run:
                    token, stderr = _resolve_capturing_stderr()

        self.assertEqual(token, TOKEN)
        run.assert_not_called()  # a scoped token must not trigger the gh fallback
        self.assertEqual(stderr, "")  # ...nor warn

    def test_gh_token_used_when_ours_is_unset(self) -> None:
        with mock.patch.dict(os.environ, {"GH_TOKEN": TOKEN}, clear=True):
            with mock.patch.object(cli.shutil, "which", return_value=None):
                token, stderr = _resolve_capturing_stderr()

        self.assertEqual(token, TOKEN)
        self.assertEqual(stderr, "")

    def test_github_token_is_the_last_env_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": TOKEN}, clear=True):
            with mock.patch.object(cli.shutil, "which", return_value=None):
                token, _ = _resolve_capturing_stderr()

        self.assertEqual(token, TOKEN)

    def test_empty_env_var_is_skipped_not_returned(self) -> None:
        # An exported-but-empty var must not shadow a real token further down.
        env = {cli.GITHUB_TOKEN_ENV: "", "GH_TOKEN": TOKEN}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(cli.shutil, "which", return_value=None):
                token, _ = _resolve_capturing_stderr()

        self.assertEqual(token, TOKEN)

    def test_falls_back_to_gh_and_warns_loudly(self) -> None:
        with _no_env():
            with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/gh"):
                with mock.patch.object(cli.subprocess, "run", _gh_returning(TOKEN + "\n")):
                    token, stderr = _resolve_capturing_stderr()

        self.assertEqual(token, TOKEN)  # trailing newline stripped
        # The convenient path must never also be the silent one.
        self.assertIn("warning", stderr.lower())
        self.assertIn("WRITE", stderr)
        self.assertIn(cli.GITHUB_TOKEN_ENV, stderr)
        self.assertNotIn(TOKEN, stderr)  # and it must never print the token itself

    def test_no_token_when_gh_is_absent(self) -> None:
        with _no_env():
            with mock.patch.object(cli.shutil, "which", return_value=None):
                token, stderr = _resolve_capturing_stderr()

        self.assertIsNone(token)
        self.assertEqual(stderr, "")  # absence is normal: public remotes need nothing

    def test_no_token_when_gh_is_not_logged_in(self) -> None:
        with _no_env():
            with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/gh"):
                with mock.patch.object(cli.subprocess, "run", _gh_returning("", returncode=1)):
                    token, _ = _resolve_capturing_stderr()

        self.assertIsNone(token)

    def test_no_token_when_gh_returns_blank(self) -> None:
        with _no_env():
            with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/gh"):
                with mock.patch.object(cli.subprocess, "run", _gh_returning("  \n")):
                    token, stderr = _resolve_capturing_stderr()

        self.assertIsNone(token)
        self.assertEqual(stderr, "")  # nothing was used, so nothing to warn about

    def test_gh_failure_is_not_fatal(self) -> None:
        def boom(command, **kwargs):  # noqa: ANN001, ANN003
            raise OSError("gh exploded")

        with _no_env():
            with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/gh"):
                with mock.patch.object(cli.subprocess, "run", boom):
                    token, _ = _resolve_capturing_stderr()

        self.assertIsNone(token)


class SecretPlumbing(unittest.TestCase):
    """The token must reach docker through the environment, never through argv."""

    def test_secret_args_name_the_var_but_not_its_value(self) -> None:
        args = cli._secret_args({cli.GITHUB_TOKEN_ENV: TOKEN})

        self.assertEqual(args, ["-e", cli.GITHUB_TOKEN_ENV])
        self.assertNotIn(TOKEN, args)

    def test_no_secrets_means_no_flags(self) -> None:
        self.assertEqual(cli._secret_args({}), [])
        self.assertEqual(cli._secret_args(None), [])

    def test_secret_environ_carries_the_value_alongside_ours(self) -> None:
        with mock.patch.dict(os.environ, {"EXISTING": "kept"}, clear=True):
            env = cli._secret_environ({cli.GITHUB_TOKEN_ENV: TOKEN})

        assert env is not None
        self.assertEqual(env[cli.GITHUB_TOKEN_ENV], TOKEN)
        self.assertEqual(env["EXISTING"], "kept")  # inherited, not replaced

    def test_secret_environ_is_none_without_secrets(self) -> None:
        # None means "inherit ours", keeping the no-token path exactly as it was.
        self.assertIsNone(cli._secret_environ({}))
        self.assertIsNone(cli._secret_environ(None))


class GitHubTokenStaysOnTheHost(unittest.TestCase):
    """The GitHub token is no longer handed to the container at all.

    The broker now performs every git effect on the host and holds the token itself
    (in the ``HostContext``). So the shim must resolve the token — and still feed it to
    the ``HostContext`` — but must NOT name it to docker: no ``-e MONO_CONTROL_GITHUB_TOKEN``
    on either backend's command line, and (since it is not forwarded) nothing about it in
    the environment handed to docker either.
    """

    def _capture(
        self, *, artifact: bool
    ) -> tuple[list[str], dict[str, str] | None, str | None]:
        seen: dict = {}

        def fake_exec(cmd, *, env=None):  # noqa: ANN001
            seen["cmd"] = cmd
            seen["env"] = env
            return 0

        real_hostcontext = cli.HostContext

        def capturing_hostcontext(**kwargs):  # noqa: ANN003
            seen["host_token"] = kwargs.get("github_token")
            return real_hostcontext(**kwargs)

        workspace = mock.MagicMock(spec=cli.Path)
        # `workspace / "mono-control"` -> a path whose .is_dir() selects the backend.
        workspace.__truediv__.return_value.is_dir.return_value = not artifact
        workspace.__truediv__.return_value.is_file.return_value = True

        # Source the token from the `gh` fallback rather than an env var, so it never
        # sits in this process's environment — then the env handed to docker can be
        # asserted clean of it, not merely un-injected. (redirect the fallback warning.)
        stderr = io.StringIO()
        with _no_env():
            with mock.patch.object(cli, "_exec", fake_exec):
                with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/x"):
                    with mock.patch.object(cli, "_detect_host_platform", return_value="linux"):
                        with mock.patch.object(cli, "HostContext", capturing_hostcontext):
                            with mock.patch.object(
                                cli.subprocess, "run", _gh_returning(TOKEN + "\n")
                            ):
                                with contextlib.redirect_stderr(stderr):
                                    cli._dispatch(
                                        workspace, ["mono-control", "repo", "list"],
                                        artifact=artifact,
                                    )

        return seen["cmd"], seen["env"], seen.get("host_token")

    def test_dev_mode_does_not_hand_the_token_to_the_container(self) -> None:
        cmd, env, host_token = self._capture(artifact=False)

        self.assertIn("compose", cmd)  # sanity: we really took the dev branch
        self.assertNotIn(TOKEN, " ".join(cmd))  # never in argv
        self.assertNotIn(cli.GITHUB_TOKEN_ENV, cmd)  # ...and not named as a -e flag
        assert env is not None
        self.assertNotIn(cli.GITHUB_TOKEN_ENV, env)  # ...nor forwarded via docker's env
        self.assertEqual(host_token, TOKEN)  # but the broker's HostContext DID get it

    def test_artifact_mode_does_not_hand_the_token_to_the_container(self) -> None:
        cmd, env, host_token = self._capture(artifact=True)

        self.assertIn("run", cmd)
        self.assertNotIn("compose", cmd)  # sanity: we really took the artifact branch
        self.assertNotIn(TOKEN, " ".join(cmd))
        self.assertNotIn(cli.GITHUB_TOKEN_ENV, cmd)
        assert env is not None
        self.assertNotIn(cli.GITHUB_TOKEN_ENV, env)
        self.assertEqual(host_token, TOKEN)

    def test_no_token_is_a_normal_state_and_reaches_the_host_context_as_none(self) -> None:
        seen: dict = {}

        def fake_exec(cmd, *, env=None):  # noqa: ANN001
            seen["cmd"] = cmd
            seen["env"] = env
            return 0

        real_hostcontext = cli.HostContext

        def capturing_hostcontext(**kwargs):  # noqa: ANN003
            seen["host_token"] = kwargs.get("github_token")
            return real_hostcontext(**kwargs)

        workspace = mock.MagicMock(spec=cli.Path)
        workspace.__truediv__.return_value.is_dir.return_value = True
        workspace.__truediv__.return_value.is_file.return_value = True

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(cli, "_exec", fake_exec):
                with mock.patch.object(cli.shutil, "which", side_effect=lambda n: None if n == "gh" else "/usr/bin/docker"):
                    with mock.patch.object(cli, "_detect_host_platform", return_value="linux"):
                        with mock.patch.object(cli, "HostContext", capturing_hostcontext):
                            cli._dispatch(workspace, ["mono-control", "repo", "list"])

        self.assertNotIn(cli.GITHUB_TOKEN_ENV, seen["cmd"])
        assert seen["env"] is not None
        self.assertNotIn(cli.GITHUB_TOKEN_ENV, seen["env"])
        self.assertIsNone(seen.get("host_token"))  # no token is a normal state
        # The env is no longer None because the broker always contributes a secret of
        # its own (see BrokerInjection); what still holds is that nothing about a GitHub
        # token reaches docker.


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
                                cli.subprocess, "run", _gh_returning("")
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
