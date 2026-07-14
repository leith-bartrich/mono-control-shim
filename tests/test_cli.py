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
import unittest
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


class TokenNeverEntersArgv(unittest.TestCase):
    """The end-to-end property, asserted on the real command lines both backends build.

    argv is readable by any local process, so a token there would be a worse leak than
    the prompt this whole change exists to remove.
    """

    def _capture(self, *, artifact: bool) -> tuple[list[str], dict[str, str] | None]:
        seen: dict = {}

        def fake_exec(cmd, *, env=None):  # noqa: ANN001
            seen["cmd"] = cmd
            seen["env"] = env
            return 0

        workspace = mock.MagicMock(spec=cli.Path)
        # `workspace / "mono-control"` -> a path whose .is_dir() selects the backend.
        workspace.__truediv__.return_value.is_dir.return_value = not artifact
        workspace.__truediv__.return_value.is_file.return_value = True

        env = {cli.GITHUB_TOKEN_ENV: TOKEN}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(cli, "_exec", fake_exec):
                with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/docker"):
                    with mock.patch.object(cli, "_detect_host_platform", return_value="linux"):
                        with mock.patch.object(cli, "_volume_args", return_value=[]):
                            with mock.patch.object(
                                cli.subprocess,
                                "run",
                                _gh_returning(""),  # image-inspect probe: exit 0 = present
                            ):
                                cli._dispatch(workspace, ["mono-control", "repo", "list"],
                                              artifact=artifact)

        return seen["cmd"], seen["env"]

    def test_dev_mode_passes_token_by_name_only(self) -> None:
        cmd, env = self._capture(artifact=False)

        self.assertIn("compose", cmd)  # sanity: we really took the dev branch
        self.assertNotIn(TOKEN, " ".join(cmd))  # THE assertion
        self.assertEqual(
            [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e" and cmd[i + 1] == cli.GITHUB_TOKEN_ENV],
            [cli.GITHUB_TOKEN_ENV],
        )
        assert env is not None
        self.assertEqual(env[cli.GITHUB_TOKEN_ENV], TOKEN)  # ...it rides in the env

    def test_artifact_mode_passes_token_by_name_only(self) -> None:
        cmd, env = self._capture(artifact=True)

        self.assertIn("run", cmd)
        self.assertNotIn("compose", cmd)  # sanity: we really took the artifact branch
        self.assertNotIn(TOKEN, " ".join(cmd))  # THE assertion
        self.assertIn(cli.GITHUB_TOKEN_ENV, cmd)
        assert env is not None
        self.assertEqual(env[cli.GITHUB_TOKEN_ENV], TOKEN)

    def test_no_token_leaves_the_command_line_untouched(self) -> None:
        seen: dict = {}

        def fake_exec(cmd, *, env=None):  # noqa: ANN001
            seen["cmd"] = cmd
            seen["env"] = env
            return 0

        workspace = mock.MagicMock(spec=cli.Path)
        workspace.__truediv__.return_value.is_dir.return_value = True
        workspace.__truediv__.return_value.is_file.return_value = True

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(cli, "_exec", fake_exec):
                with mock.patch.object(cli.shutil, "which", side_effect=lambda n: None if n == "gh" else "/usr/bin/docker"):
                    with mock.patch.object(cli, "_detect_host_platform", return_value="linux"):
                        with mock.patch.object(cli, "_volume_args", return_value=[]):
                            cli._dispatch(workspace, ["mono-control", "repo", "list"])

        self.assertNotIn(cli.GITHUB_TOKEN_ENV, seen["cmd"])
        self.assertIsNone(seen["env"])  # inherit, exactly as before secrets existed


if __name__ == "__main__":
    unittest.main()
