"""The shared git chokepoint: one subprocess seam, one credential posture.

This module holds the pieces that every git call in the shim must go through,
independent of *who* is calling. It was lifted out of ``verbs/git.py`` when the
CLI grew its own need to run git (``mproj init`` cloning or creating
``mono-config/``), because that call happens **before** any container exists:
there is no broker, no ``HostContext``, and no slug — so it cannot reach the verb
layer, but it must not therefore get a weaker posture.

What lives here is exactly what is common to both callers:

* ``run_git`` — the single subprocess seam. List-form args only, never a shell
  string, so a URL or ref can never be reinterpreted by a shell.
* the **non-interactive credential posture** (``GIT_TERMINAL_PROMPT=0`` plus
  ``credential.interactive=false``), so a network op with no usable credential
  fails fast on every platform instead of hanging on a TTY prompt or a GUI popup.
* the auth-failure classifier and its actionable "set up gh / a credential
  helper" hint, so both callers reword the same failure the same way.
* the identity fallback used when writing a commit on a host that has no git
  identity configured.

What deliberately does **not** live here is anything that knows about the managed
workspace — slugs, bare roots, worktrees, the ``mono-control.slug`` stamp. That is
the verb layer's business (``verbs/git.py``), because it is the layer that answers
to the container. ``mono-config/`` is not a managed repo: it is a plain working
checkout at the workspace root, so the CLI needs the seam without the model.

Credentials remain the host's. Git runs host-side *as the developer* and inherits
the host's own credential machinery (gh helper, Git Credential Manager, OS
keyring, ``~/.gitconfig``). Nothing here injects a token or a credential helper.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Optional


class GitError(Exception):
    """A git operation failed: a missing binary or a non-zero exit."""


def run_git(
    args: list[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
    config: Optional[list[str]] = None,
) -> str:
    """Run ``git [config...] <args>`` and return stripped stdout.

    List-form args only — never a shell string — so a URL or ref can never be
    reinterpreted by a shell. ``config`` holds ``-c key=value`` pairs that must
    precede the subcommand (e.g. the non-interactive posture). A non-zero exit
    raises ``GitError`` with stderr; a missing binary raises ``GitError`` too.
    """
    command = ["git", *(config or []), *args]
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except FileNotFoundError as e:  # pragma: no cover - git present in CI
        raise GitError("git executable not found on PATH") from e
    if result.returncode != 0:
        # No secret to redact: nothing here injects a token — host git supplies its
        # own credential — so the raw stderr is safe to surface (and is what the
        # auth classifier reads).
        raise GitError(f"`git {' '.join(args)}` failed: {result.stderr.strip()}")
    return result.stdout.strip()


# --------------------------------------------------------------------------- #
# The non-interactive credential posture
# --------------------------------------------------------------------------- #
# ``-c`` config that makes a git call strictly non-interactive on the credential
# axis: ``credential.interactive=false`` tells Git Credential Manager (and other
# helpers that honor it) to fail rather than pop a GUI prompt. Paired with the
# ``GIT_TERMINAL_PROMPT=0`` env below, a missing credential becomes a fast, clean
# error on every platform instead of a hang.
NONINTERACTIVE_CONFIG = ("-c", "credential.interactive=false")


def noninteractive_config() -> list[str]:
    """``-c`` flags enforcing the non-interactive credential posture."""
    return list(NONINTERACTIVE_CONFIG)


def noninteractive_env(*, https_only: bool = False) -> dict[str, str]:
    """The environment for a network git call: strictly non-interactive, no token.

    ``GIT_TERMINAL_PROMPT=0`` stops git itself from prompting on a TTY, turning a
    missing credential into a clean error. Host git supplies its own credential, so
    nothing is injected here. ``https_only`` adds ``GIT_ALLOW_PROTOCOL=https`` for
    callers whose URL is supplied by the *container* (a security allow-list,
    unrelated to credentials); a URL the developer types at the host CLI is not in
    that category and is not restricted.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if https_only:
        env["GIT_ALLOW_PROTOCOL"] = "https"
    return env


# --------------------------------------------------------------------------- #
# Auth-failure classification
# --------------------------------------------------------------------------- #
# stderr substrings (matched case-insensitively) that mark a git network failure as
# an auth / credential problem — as opposed to a missing repo, a DNS failure, or a
# refused connection. With the non-interactive posture above, a private remote with
# no usable credential fails with one of these rather than hanging on a prompt.
AUTH_MARKERS = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "invalid username or password",
    "support for password authentication was removed",
    "the requested url returned error: 403",
    "error: 403",
    "remote: permission to",
    "remote: repository not found",
)

# The actionable hint appended to an auth failure. Git runs on the host as the
# developer, so the fix is always "give host git a credential".
AUTH_HINT = (
    "git runs on the host now and found no usable GitHub credential. Set one up on "
    "the host — run `gh auth login` (easiest; makes gh git's credential helper), or "
    "configure a credential helper / fine-grained PAT for github.com."
)


def is_auth_failure(stderr: str) -> bool:
    """True when *stderr* (or a ``GitError`` message wrapping it) looks like an
    auth / credential failure rather than any other network error."""
    low = stderr.lower()
    return any(marker in low for marker in AUTH_MARKERS)


def auth_summary(target: str) -> str:
    """The actionable summary for an auth failure against *target* (a slug or URL)."""
    return f"authentication failed for {target}: {AUTH_HINT}"


# --------------------------------------------------------------------------- #
# Identity fallback for machine-written commits
# --------------------------------------------------------------------------- #
# Identity for a root commit written on the developer's behalf, used only for
# fields the host has not configured. The address is deliberately unroutable
# (RFC 2606 ``.invalid``): a machine-made commit should not look like it came from
# a real mailbox.
FALLBACK_IDENTITY = (
    ("user.name", "mono-control"),
    ("user.email", "mono-control@invalid"),
)


def identity_config(config_get: Callable[[str], str]) -> list[str]:
    """``-c user.*`` pairs for identity fields the host hasn't set.

    *config_get* reads one git config key and raises ``GitError`` when it is unset
    (``git config --get`` exits non-zero on a missing key). It is passed in rather
    than derived so each caller supplies its own reader — the verb layer has a repo
    handle, the CLI has only a path.

    The user's own identity is preferred, so a commit made on their behalf looks
    like the rest of their history. The fallback applies per field, and only when a
    field is missing, so writing a root commit still works on a fresh machine with
    no git identity configured rather than failing at the last step.
    """
    config: list[str] = []
    for key, fallback in FALLBACK_IDENTITY:
        try:
            if config_get(key):
                continue
        except GitError:
            pass  # unset: `config --get` exits non-zero on a missing key
        config += ["-c", f"{key}={fallback}"]
    return config


def config_reader(path: Path) -> Callable[[str], str]:
    """A ``config_get`` for ``identity_config`` that reads the repo at *path*."""

    def read(key: str) -> str:
        return run_git(["config", "--get", key], cwd=path)

    return read
