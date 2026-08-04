"""Tests for ``mproj init``'s config source: clone / fresh / skip.

Stdlib ``unittest`` + the real ``git`` binary, matching ``test_verbs_git.py``: the
point of this feature is that it really produces a usable config repo, so faking
git would prove nothing. Hermetic and offline — the "URL" a clone test passes is a
*local path* to a repo made in a temp dir, so no test reaches the network.

    python -m unittest discover -s tests -t .

The three things worth getting right, and why:

* **Idempotency.** An existing config repo short-circuits before any question, so
  re-running ``init`` on a working workspace never prompts.
* **The non-TTY refusal.** With no flag and no terminal there is nobody to ask, so
  ``init`` must fail fast naming the flags rather than block on ``input()`` — the
  same reflex as the ``GIT_TERMINAL_PROMPT=0`` posture it clones under.
* **Never clobbering.** A ``mono-config/`` with contents but no ``.git`` is refused,
  not overwritten.
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
from mono_control_shim.git_run import GitError


def _git(args: list[str], cwd: Path) -> str:
    """Run git in a test fixture with a pinned identity (no global config needed)."""
    ident = [
        "-c", "user.email=tester@example.com",
        "-c", "user.name=Tester",
        "-c", "commit.gpgsign=false",
    ]
    result = subprocess.run(
        ["git", *ident, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class InitConfigBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config_dir = self.workspace / cli.WORKSPACE_MARKER

    def _make_source_repo(self, name: str = "source-config") -> Path:
        """A real repo with one commit, used as a local clone 'URL'."""
        source = self.root / name
        source.mkdir()
        _git(["init", "--initial-branch", "main"], source)
        (source / "system.json").write_text("{}\n", encoding="utf-8")
        _git(["add", "."], source)
        _git(["commit", "-m", "seed"], source)
        return source

    def _init(self, **kwargs) -> tuple[int, str, str]:
        """Run ``_run_init`` capturing output. Defaults to a non-TTY."""
        kwargs.setdefault("isatty", False)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli._run_init(self.workspace, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def _assert_dirs_exist(self) -> None:
        for name in cli.INIT_DIRS:
            self.assertTrue((self.workspace / name).is_dir(), name)


class CloneBranch(InitConfigBase):
    def test_config_url_clones_into_mono_config(self) -> None:
        source = self._make_source_repo()

        code, out, _ = self._init(config_url=str(source))

        self.assertEqual(code, 0, out)
        self._assert_dirs_exist()
        self.assertTrue((self.config_dir / ".git").exists())
        self.assertTrue((self.config_dir / "system.json").is_file())
        self.assertEqual(cli._config_origin(self.config_dir), str(source))

    def test_clone_carries_the_noninteractive_posture(self) -> None:
        """A private config repo with no credential must fail fast, not hang."""
        source = self._make_source_repo()
        seen: list[tuple[dict, list]] = []
        real_run_git = cli.run_git

        def spy(args, **kwargs):  # noqa: ANN001, ANN003
            if args and args[0] == "clone":
                seen.append((kwargs.get("env") or {}, kwargs.get("config") or []))
            return real_run_git(args, **kwargs)

        with mock.patch.object(cli, "run_git", spy):
            code, _, _ = self._init(config_url=str(source))

        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 1)
        env, config = seen[0]
        self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0")
        self.assertIn("credential.interactive=false", " ".join(config))

    def test_clone_auth_failure_is_reworded_actionably(self) -> None:
        def boom(args, **kwargs):  # noqa: ANN001, ANN003
            raise GitError(
                "`git clone -- https://github.com/o/r.git ...` failed: "
                "fatal: Authentication failed for 'https://github.com/o/r.git'"
            )

        with mock.patch.object(cli, "run_git", boom):
            code, _, err = self._init(config_url="https://github.com/o/r.git")

        self.assertEqual(code, 1)
        self.assertIn("gh auth login", err)

    def test_non_auth_clone_failure_keeps_the_raw_error(self) -> None:
        code, _, err = self._init(config_url=str(self.root / "does-not-exist"))

        self.assertEqual(code, 1)
        self.assertNotIn("gh auth login", err)

    def test_a_failed_clone_still_leaves_a_reusable_workspace(self) -> None:
        """The plain dirs are made first, so `init` can simply be re-run."""
        code, _, _ = self._init(config_url=str(self.root / "does-not-exist"))
        self.assertEqual(code, 1)
        self._assert_dirs_exist()

        code, _, _ = self._init(config_url=str(self._make_source_repo()))
        self.assertEqual(code, 0)
        self.assertTrue((self.config_dir / ".git").exists())


class FreshBranch(InitConfigBase):
    def test_config_fresh_creates_a_repo_with_a_real_head(self) -> None:
        """``git init`` alone leaves HEAD unborn; the root commit makes it real."""
        code, out, _ = self._init(config_fresh=True)

        self.assertEqual(code, 0, out)
        self.assertTrue((self.config_dir / ".git").exists())
        self.assertEqual(
            _git(["symbolic-ref", "--short", "HEAD"], self.config_dir), "main"
        )
        self.assertTrue(_git(["rev-parse", "--verify", "HEAD^{commit}"], self.config_dir))
        self.assertEqual(_git(["status", "--porcelain"], self.config_dir), "")

    def test_fresh_repo_gets_no_remote(self) -> None:
        """Pointing a fresh config repo at a remote is a separate, later act."""
        self._init(config_fresh=True)
        self.assertIsNone(cli._config_origin(self.config_dir))

    def test_root_commit_falls_back_to_tool_identity(self) -> None:
        """A host with no git identity configured must still get its root commit."""

        def unset(_path):  # noqa: ANN001
            def read(key: str) -> str:
                raise GitError(f"`git config --get {key}` failed: ")

            return read

        with mock.patch.object(cli, "config_reader", unset):
            code, _, _ = self._init(config_fresh=True)

        self.assertEqual(code, 0)
        author = _git(["log", "-1", "--format=%an <%ae>"], self.config_dir)
        self.assertEqual(author, "mono-control <mono-control@invalid>")


class SkipBranch(InitConfigBase):
    def test_no_config_leaves_an_empty_dir(self) -> None:
        """The behavior of `init` before it learned to populate mono-config/."""
        code, out, _ = self._init(no_config=True)

        self.assertEqual(code, 0, out)
        self._assert_dirs_exist()
        self.assertFalse((self.config_dir / ".git").exists())
        self.assertEqual(list(self.config_dir.iterdir()), [])


class NonInteractiveRefusal(InitConfigBase):
    def test_no_flag_and_no_tty_refuses_instead_of_asking(self) -> None:
        code, _, err = self._init(isatty=False)

        self.assertEqual(code, 1)
        self.assertIn("--config-url", err)
        self.assertIn("--config-fresh", err)
        self.assertIn("--no-config", err)

    def test_the_refusal_never_calls_input(self) -> None:
        """The whole point: a non-TTY must fail fast, not block on stdin."""

        def explode(*_args, **_kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("input() must not be called without a TTY")

        with mock.patch("builtins.input", explode):
            code, _, _ = self._init(isatty=False)

        self.assertEqual(code, 1)

    def test_the_dirs_are_still_created_before_the_refusal(self) -> None:
        self._init(isatty=False)
        self._assert_dirs_exist()


class InteractivePrompt(InitConfigBase):
    def test_choosing_clone_asks_for_a_url_and_clones(self) -> None:
        source = self._make_source_repo()

        with mock.patch("builtins.input", side_effect=["1", str(source)]):
            code, _, _ = self._init(isatty=True)

        self.assertEqual(code, 0)
        self.assertTrue((self.config_dir / "system.json").is_file())

    def test_empty_choice_defaults_to_clone(self) -> None:
        source = self._make_source_repo()

        with mock.patch("builtins.input", side_effect=["", str(source)]):
            code, _, _ = self._init(isatty=True)

        self.assertEqual(code, 0)
        self.assertTrue((self.config_dir / ".git").exists())

    def test_choosing_fresh_creates_a_repo(self) -> None:
        with mock.patch("builtins.input", side_effect=["2"]):
            code, _, _ = self._init(isatty=True)

        self.assertEqual(code, 0)
        self.assertTrue((self.config_dir / ".git").exists())

    def test_choosing_skip_leaves_an_empty_dir(self) -> None:
        with mock.patch("builtins.input", side_effect=["3"]):
            code, _, _ = self._init(isatty=True)

        self.assertEqual(code, 0)
        self.assertEqual(list(self.config_dir.iterdir()), [])

    def test_an_empty_url_is_refused(self) -> None:
        with mock.patch("builtins.input", side_effect=["1", "  "]):
            code, _, err = self._init(isatty=True)

        self.assertEqual(code, 1)
        self.assertIn("no URL", err)

    def test_an_unknown_choice_is_refused(self) -> None:
        with mock.patch("builtins.input", side_effect=["9"]):
            code, _, err = self._init(isatty=True)

        self.assertEqual(code, 1)
        self.assertIn("not a choice", err)

    def test_ctrl_d_aborts_without_a_traceback(self) -> None:
        with mock.patch("builtins.input", side_effect=EOFError):
            code, _, err = self._init(isatty=True)

        self.assertEqual(code, 1)
        self.assertIn("aborted", err)


class ExistingConfigRepo(InitConfigBase):
    def _clone_once(self) -> Path:
        source = self._make_source_repo()
        code, _, _ = self._init(config_url=str(source))
        self.assertEqual(code, 0)
        return source

    def test_rerunning_on_a_working_workspace_never_prompts(self) -> None:
        """Idempotency: an existing repo short-circuits before the question."""
        self._clone_once()

        def explode(*_args, **_kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("input() must not be called for an existing repo")

        with mock.patch("builtins.input", explode):
            code, out, _ = self._init(isatty=True)  # no flags, TTY: would prompt

        self.assertEqual(code, 0)
        self.assertIn("already a repo", out)

    def test_matching_config_url_is_a_no_op(self) -> None:
        source = self._clone_once()

        code, out, _ = self._init(config_url=str(source))

        self.assertEqual(code, 0)
        self.assertIn("already a repo", out)

    def test_a_different_config_url_is_refused_not_repointed(self) -> None:
        source = self._clone_once()
        other = self._make_source_repo("other-config")

        code, _, err = self._init(config_url=str(other))

        self.assertEqual(code, 1)
        self.assertIn("different origin", err)
        # The existing repo is untouched.
        self.assertEqual(cli._config_origin(self.config_dir), str(source))

    def test_a_fresh_repo_with_no_origin_is_reported_not_refused(self) -> None:
        self._init(config_fresh=True)

        code, out, _ = self._init(config_url="https://example.invalid/cfg.git")

        self.assertEqual(code, 0, "no origin means nothing to conflict with")
        self.assertIn("no origin", out)


class OccupiedConfigDir(InitConfigBase):
    def test_contents_without_a_git_dir_are_never_clobbered(self) -> None:
        self.config_dir.mkdir()
        (self.config_dir / "system.json").write_text("{}\n", encoding="utf-8")

        code, _, err = self._init(config_url=str(self._make_source_repo()))

        self.assertEqual(code, 1)
        self.assertIn("not a git repo", err)
        self.assertEqual(
            (self.config_dir / "system.json").read_text(encoding="utf-8"), "{}\n"
        )

    def test_an_occupied_dir_is_refused_before_any_prompt(self) -> None:
        self.config_dir.mkdir()
        (self.config_dir / "stray").write_text("x", encoding="utf-8")

        def explode(*_args, **_kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("input() must not be called for an occupied dir")

        with mock.patch("builtins.input", explode):
            code, _, _ = self._init(isatty=True)

        self.assertEqual(code, 1)


class ConfigStateClassification(InitConfigBase):
    def test_absent_and_empty_both_read_as_empty(self) -> None:
        self.assertEqual(cli._config_state(self.config_dir), cli.CONFIG_EMPTY)
        self.config_dir.mkdir()
        self.assertEqual(cli._config_state(self.config_dir), cli.CONFIG_EMPTY)

    def test_a_git_file_counts_as_a_repo(self) -> None:
        """A worktree / submodule checkout carries `.git` as a file, not a dir."""
        self.config_dir.mkdir()
        (self.config_dir / ".git").write_text("gitdir: ../elsewhere\n", encoding="utf-8")
        self.assertEqual(cli._config_state(self.config_dir), cli.CONFIG_REPO)


class FlagParsing(unittest.TestCase):
    def test_config_source_flags_are_mutually_exclusive(self) -> None:
        for pair in (
            ["--config-url", "https://example.invalid/c.git", "--config-fresh"],
            ["--config-fresh", "--no-config"],
            ["--config-url", "https://example.invalid/c.git", "--no-config"],
        ):
            with self.subTest(pair=pair):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as cm:
                        cli.main(["init", *pair])
                self.assertEqual(cm.exception.code, 2)

    def test_init_does_not_require_an_existing_workspace_marker(self) -> None:
        """`init` creates the marker, so it cannot demand it up front."""
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["init", "--workspace", tmp, "--no-config"])
            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / cli.WORKSPACE_MARKER).is_dir())


if __name__ == "__main__":
    unittest.main()
