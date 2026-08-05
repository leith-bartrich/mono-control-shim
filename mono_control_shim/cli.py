"""Host-installed shim CLI for mono-control.

This module is deliberately stdlib-only. It is installed on the *host* and its
only job is to locate the mono workspace and hand off to the real mono-control
tooling (which runs inside a dev container). Keeping the host surface minimal
and dependency-free is a security goal, not an accident.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from mono_control_shim.broker import BrokerServer, HostContext
from mono_control_shim.git_run import (
    GitError,
    auth_summary,
    config_reader,
    identity_config,
    is_auth_failure,
    noninteractive_config,
    noninteractive_env,
    run_git,
)

# A directory is recognized as a mono workspace when it contains the manifest
# directory `mono-config/`. A `mono-control/` checkout may or may not sit beside
# it; its presence selects dev vs. artifact execution (see `control`), not whether
# this is a workspace.
WORKSPACE_MARKER = "mono-config"

# Env var consulted when --workspace is not passed.
WORKSPACE_ENV_VAR = "MONO_WORKSPACE"

# Canonical local image ref for artifact mode (no mono-control/ checkout, or
# --artifact forced). Distribution via ghcr.io is planned; for now built locally.
MONO_CONTROL_IMAGE = "mono-control:latest"

# Host-platform declaration carried into the container (consumed by mono-control's
# host-platform gate; see mono-control/docs/design/host-platform.md). The shim is
# the host-side authority that supplies it — it always knows the host — so it sets
# this on every container run via `-e`, overriding the image's baked `generic`.
HOST_PLATFORM_ENV = "MONO_CONTROL_HOST_PLATFORM"

# platform.system() -> the token mono-control expects.
_HOST_PLATFORM_BY_SYSTEM = {
    "Windows": "windows",
    "Darwin": "darwin",
    "Linux": "linux",
}

# Host-callback broker coordinates carried into the container (see broker.py). Same
# shape as the host platform above — host-side knowledge the container cannot derive —
# split by sensitivity: the host and port are not secret and ride the `-e KEY=VALUE`
# path, while the per-run token is a SECRET and takes a VALUELESS `-e` route (named in
# argv, valued only in the environment handed to docker), so it never appears in argv.
#
# `host.docker.internal` is the name Docker Desktop gives the host from inside a
# container. The shim names it rather than an address because the address is not
# stable across Docker networks, and the container has no way to learn either.
BROKER_HOST_ENV = "MONO_BROKER_HOST"
BROKER_PORT_ENV = "MONO_BROKER_PORT"
BROKER_TOKEN_ENV = "MONO_BROKER_TOKEN"

BROKER_CONTAINER_HOST = "host.docker.internal"


def _detect_host_platform() -> str:
    """Return the mono-control host-platform token for this host.

    An explicit ``MONO_CONTROL_HOST_PLATFORM`` in the environment is respected as
    an override (handy for forcing a target's stamping behavior — e.g. exercising
    Windows semantics from a Linux box); the container validates whatever it is
    given. Otherwise the OS is detected. An unmappable host raises ``ValueError``:
    the shim's whole job here is to supply a concrete platform, so it refuses
    rather than guessing or silently falling back to ``generic``.
    """
    override = os.environ.get(HOST_PLATFORM_ENV)
    if override:
        return override
    system = platform.system()
    token = _HOST_PLATFORM_BY_SYSTEM.get(system)
    if token is None:
        raise ValueError(
            f"unsupported host platform {system!r}; cannot determine "
            f"{HOST_PLATFORM_ENV}. Set it explicitly to one of "
            f"{sorted(_HOST_PLATFORM_BY_SYSTEM.values())}."
        )
    return token


def _is_workspace(path: Path) -> bool:
    """True if *path* looks like a mono workspace root (has `mono-config/`)."""
    return (path / WORKSPACE_MARKER).is_dir()


def _walk_up_for_workspace(start: Path) -> Path | None:
    """Walk up from *start* looking for a workspace root. Returns None if none."""
    start = start.resolve()
    for candidate in (start, *start.parents):
        if _is_workspace(candidate):
            return candidate
    return None


def resolve_workspace(explicit: str | None) -> Path | None:
    """Resolve the workspace using the precedence:

    1. The explicit --workspace value, if given.
    2. The MONO_WORKSPACE environment variable, if set.
    3. Walking up from the current working directory.

    Returns the resolved Path, or None if nothing could be found.
    """
    if explicit:
        path = Path(explicit).expanduser().resolve()
        return path if _is_workspace(path) else None

    env_value = os.environ.get(WORKSPACE_ENV_VAR)
    if env_value:
        path = Path(env_value).expanduser().resolve()
        return path if _is_workspace(path) else None

    return _walk_up_for_workspace(Path.cwd())


def _resolve_init_target(explicit: str | None) -> Path:
    """Resolve where `init` should create the workspace directories.

    `init` *creates* the `mono-config/` marker, so it cannot require it to
    already exist (unlike ``resolve_workspace``). Bootstrap precedence, with no
    marker check and no walk-up: explicit --workspace, then MONO_WORKSPACE, then
    the current directory.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_value = os.environ.get(WORKSPACE_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser().resolve()
    return Path.cwd()


# How long the cheap probes wait on the docker CLI. A responsive daemon answers
# `info` / `ps` in well under a second; a wedged one answers never, so a bound is
# what turns "hangs forever" into "reports a problem".
_PROBE_TIMEOUT = 10.0


def _docker_reachability() -> tuple[bool, str]:
    """Layer 1: is the docker CLI there and does the daemon answer?

    Returns (reachable, human_readable_detail). Stdlib only; never raises.

    **Purely a question about the host.** It deliberately knows nothing about the
    workspace. An earlier version folded in "does a `mono-control/` checkout with a
    `.devcontainer` exist" and returned False when it did not — which reported
    *docker* as unreachable on a workspace with no checkout. That is the normal
    end-user setup, the one artifact mode exists to serve, and docker was fine:
    `mproj control` ran and answered correctly in exactly the workspace this line
    called broken. Which mode a workspace will run in is now ``_mode_status``, and
    it is information rather than a verdict.

    This is also the **cheap** layer, and it claims only what it tested. A daemon
    can answer ``docker info`` perfectly while being unable to start a single
    container, so proving that costs a container run and lives in ``mproj doctor``.
    """
    docker = shutil.which("docker")
    if docker is None:
        return False, "docker not found on PATH"

    try:
        result = subprocess.run(
            [docker, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
        daemon_up = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        daemon_up = False

    if not daemon_up:
        return False, f"docker found ({docker}) but the daemon is not responding"

    return True, "daemon answered"


# --------------------------------------------------------------------------- #
# Which backend this workspace will use — information, not a verdict
# --------------------------------------------------------------------------- #
MODE_DEV = "dev"
MODE_ARTIFACT = "artifact"


def _selected_mode(workspace: Path) -> str:
    """The backend ``_dispatch`` will choose for this workspace.

    Mirrors ``_dispatch``'s own test exactly — the presence of a ``mono-control/``
    *directory* — because a mode report that disagrees with the dispatcher is worse
    than no report. Note the local ``mono-control:latest`` image is not consulted:
    a checkout always wins, however fresh the image is.
    """
    return MODE_DEV if (workspace / "mono-control").is_dir() else MODE_ARTIFACT


def _mode_status(workspace: Path, docker: str | None) -> tuple[str, bool, str]:
    """(mode, ready, detail) for the backend this workspace will use.

    Neither mode is a failure — having no checkout is the ordinary way to consume
    the tool. ``ready`` is False only when the *selected* mode cannot actually run:
    a checkout whose compose file is missing, or artifact mode with no image built.

    *docker* is the CLI path when the daemon is reachable, else None; without it the
    artifact image cannot be probed, so the report says what it will do rather than
    claiming the image is there.
    """
    mode = _selected_mode(workspace)

    if mode == MODE_DEV:
        compose = workspace / "mono-control" / ".devcontainer" / "docker-compose.yml"
        if not compose.is_file():
            return mode, False, f"mono-control/ present but {compose} is missing"
        return mode, True, "runs live source via Compose (mono-control/ checkout)"

    if docker is None:
        return mode, True, f"would run {MONO_CONTROL_IMAGE} (no mono-control/ checkout)"

    try:
        image = _local_image(docker)
    except (OSError, subprocess.SubprocessError):
        image = None
    if image is None:
        return mode, False, (
            f"no {MONO_CONTROL_IMAGE} image and no mono-control/ checkout - run "
            "`mproj build-control` from a workspace that has the source"
        )
    return mode, True, f"runs the prebuilt {image} (no mono-control/ checkout)"


# Directories that `mproj init` ensures exist in the workspace root. The broker acts
# on these host-side dirs directly (they feed the HostContext), so they are no longer
# bind-mounted into the container, but they MUST still exist on the host: mono-repos-bare
# holds the bare repositories (one per slug — the durable store; a repo never moves out of
# it), mono-work holds the worktrees a repo is materialized into, and mono-config the
# manifest dir. Committed work is safe in the bare repo, so a discarded worktree loses
# nothing that was committed.
INIT_DIRS = ("mono-repos-bare", "mono-work", "mono-config")


def _run_status(workspace: Path) -> int:
    """Default command: report the workspace and docker *reachability*.

    A **report**, not a verdict — it always exits 0, and ``doctor`` is where a
    problem becomes an exit code. That division is deliberate: this runs on the hot
    path, so it stays cheap and descriptive.

    Says "reachable", never "available". The word matters: this ran a single
    ``docker info``, so the only honest claim is that the daemon answered. Pointing
    at ``doctor`` in the same breath is what stops a green line here from being read
    as "everything works" when containers cannot start.
    """
    print(f"workspace: {workspace}")

    reachable, detail = _docker_reachability()
    print(f"docker: {'reachable' if reachable else 'unreachable'} ({detail})")

    docker = shutil.which("docker") if reachable else None
    mode, ready, mode_detail = _mode_status(workspace, docker)
    print(f"mode: {mode} - {mode_detail}")
    if not ready:
        print(f"  the {mode} backend cannot run as configured; `mproj doctor` has the detail")

    if reachable:
        print(
            "  reachability only - run `mproj doctor` to test that a container can "
            "actually start"
        )

    return 0


# --------------------------------------------------------------------------- #
# `init`'s config source: clone / fresh / skip
# --------------------------------------------------------------------------- #
# What `init` should do about `mono-config/`. `SKIP` is the historical behavior —
# create the empty directory and nothing more — kept as an explicit choice so
# anything scripted against the old `mproj init` keeps working with one flag rather
# than walking into a prompt.
CONFIG_CLONE = "clone"
CONFIG_FRESH = "fresh"
CONFIG_SKIP = "skip"

# The state `mono-config/` is in, which decides whether there is a question to ask.
CONFIG_REPO = "repo"  # already a git repo: nothing to do, and nothing to ask
CONFIG_EMPTY = "empty"  # absent, or present and empty: clonable
CONFIG_OCCUPIED = "occupied"  # has content but is not a repo: refuse, never clobber

# The initial branch for a `--config-fresh` repo. Named explicitly rather than left
# to the host's `init.defaultBranch` so a fresh config repo is the same everywhere,
# and via `--initial-branch` (not `-b`) to match `verbs/git.py`'s bare init.
_FRESH_CONFIG_BRANCH = "main"


def _config_state(config_dir: Path) -> str:
    """Classify ``mono-config/`` into one of the three states above.

    ``.git`` is tested with ``exists()`` rather than ``is_dir()`` because a worktree
    or a submodule checkout carries it as a *file*; either way the directory is
    already a repo and we must not touch it.
    """
    if (config_dir / ".git").exists():
        return CONFIG_REPO
    if not config_dir.exists():
        return CONFIG_EMPTY
    if any(config_dir.iterdir()):
        return CONFIG_OCCUPIED
    return CONFIG_EMPTY


def _config_origin(config_dir: Path) -> str | None:
    """The existing repo's ``origin`` URL, or None if it has no origin."""
    try:
        return run_git(["config", "--get", "remote.origin.url"], cwd=config_dir) or None
    except GitError:
        return None


def _prompt_config_source(config_dir: Path) -> tuple[str, str | None] | None:
    """Ask what to do about ``mono-config/``. Returns (choice, url) or None to abort.

    Only ever reached on a TTY with no flag given — see ``_resolve_config_source``.
    """
    print(f"\nNo config repo at {config_dir}.")
    print("mono-control reads the workspace manifest from there, so it needs one.\n")
    print("  [1] Clone an existing config repo")
    print("  [2] Create a fresh, empty config repo here")
    print("  [3] Skip for now - just create the empty directory\n")

    try:
        choice = input("Choice [1]: ").strip() or "1"
        if choice == "1":
            url = input("Config repo URL: ").strip()
            if not url:
                print("error: no URL given", file=sys.stderr)
                return None
            return CONFIG_CLONE, url
        if choice == "2":
            return CONFIG_FRESH, None
        if choice == "3":
            return CONFIG_SKIP, None
    except (EOFError, KeyboardInterrupt):
        print("\naborted", file=sys.stderr)
        return None

    print(f"error: not a choice: {choice!r}", file=sys.stderr)
    return None


def _resolve_config_source(
    config_dir: Path,
    *,
    config_url: str | None,
    config_fresh: bool,
    no_config: bool,
    isatty: bool,
) -> tuple[str, str | None] | None:
    """Decide the config source from the flags, or ask. None means "stop".

    An explicit flag always wins and never prompts, so `init` stays scriptable. With
    no flag the answer has to come from somewhere: a TTY gets the question, and a
    non-TTY gets a *refusal* rather than a hang. That refusal is the same reflex as
    the ``GIT_TERMINAL_PROMPT=0`` posture in ``git_run`` — when input is impossible,
    fail fast and name the way out instead of blocking on something nobody can answer.
    """
    if config_url is not None:
        return CONFIG_CLONE, config_url
    if config_fresh:
        return CONFIG_FRESH, None
    if no_config:
        return CONFIG_SKIP, None

    if not isatty:
        print(
            "error: no config source given and stdin is not a terminal, so there is "
            "nothing to ask.\n"
            "  Pass one of --config-url URL, --config-fresh, or --no-config.",
            file=sys.stderr,
        )
        return None

    return _prompt_config_source(config_dir)


def _clone_config(config_dir: Path, url: str) -> int:
    """Clone *url* into ``mono-config/``.

    Runs under the shared non-interactive posture, so a private config repo with no
    usable host credential fails fast with the actionable hint instead of hanging on
    a credential prompt. ``--`` separates the URL from the options so a value
    starting with ``-`` can never be read as a flag.

    The URL is deliberately *not* scheme-restricted. ``verbs/git.py`` allow-lists
    https because there the URL arrives from the container; here the developer typed
    it at their own shell, which is the same trust as running ``git clone`` by hand —
    and ssh remotes and local paths are both legitimate.
    """
    print(f"config:  cloning {url}")
    try:
        run_git(
            ["clone", "--", url, str(config_dir)],
            env=noninteractive_env(),
            config=noninteractive_config(),
        )
    except GitError as e:
        message = str(e)
        if is_auth_failure(message):
            print(f"error: {auth_summary(url)}", file=sys.stderr)
        else:
            print(f"error: {message}", file=sys.stderr)
        return 1
    print(f"config:  {config_dir} cloned")
    return 0


def _init_fresh_config(config_dir: Path) -> int:
    """Create a brand-new, empty config repo at ``mono-config/``.

    Given an empty root commit for the same reason ``verbs/git.py`` gives one to a
    newly init'd bare repo (see ``GitRepo.write_root_commit``): ``git init`` leaves
    HEAD on an *unborn* branch, so the branch does not exist until something is
    committed. One empty commit means the repo has a real HEAD, a real branch, and a
    diffable history from the moment it exists. No remote is added — pointing a fresh
    config repo at one is a separate, later act.
    """
    try:
        run_git(["init", "--initial-branch", _FRESH_CONFIG_BRANCH, str(config_dir)])
        tree = run_git(["hash-object", "-w", "-t", "tree", os.devnull], cwd=config_dir)
        commit = run_git(
            ["commit-tree", tree, "-m", "initial commit"],
            cwd=config_dir,
            config=identity_config(config_reader(config_dir)),
        )
        run_git(
            ["update-ref", f"refs/heads/{_FRESH_CONFIG_BRANCH}", commit], cwd=config_dir
        )
    except GitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"config:  {config_dir} initialized fresh on {_FRESH_CONFIG_BRANCH} (no remote)")
    return 0


def _ensure_config(
    workspace: Path,
    *,
    config_url: str | None,
    config_fresh: bool,
    no_config: bool,
    isatty: bool,
) -> int:
    """Bring ``mono-config/`` into being, by whichever route was chosen.

    An existing config repo short-circuits before any question is asked, which is
    what keeps `init` idempotent: re-running it on a working workspace never
    prompts, and the interactive path is reserved for a genuine bootstrap.
    """
    config_dir = workspace / WORKSPACE_MARKER
    state = _config_state(config_dir)

    if state == CONFIG_REPO:
        origin = _config_origin(config_dir)
        detail = f" (origin: {origin})" if origin else " (no origin)"
        if config_url is not None and origin is not None and origin != config_url:
            print(
                f"error: {config_dir} is already a config repo with a different origin.\n"
                f"  existing: {origin}\n"
                f"  requested: {config_url}\n"
                "  Repointing an existing config repo is not init's job - move or "
                "remove it first, or change its remote with git.",
                file=sys.stderr,
            )
            return 1
        print(f"config:  {config_dir} is already a repo{detail}")
        return 0

    if state == CONFIG_OCCUPIED:
        print(
            f"error: {config_dir} has contents but is not a git repo.\n"
            "  Refusing to clone or init over it. Move or remove it first.",
            file=sys.stderr,
        )
        return 1

    resolved = _resolve_config_source(
        config_dir,
        config_url=config_url,
        config_fresh=config_fresh,
        no_config=no_config,
        isatty=isatty,
    )
    if resolved is None:
        return 1

    choice, url = resolved
    if choice == CONFIG_CLONE:
        assert url is not None
        # `git clone` is happy to write into an existing *empty* directory, which is
        # exactly what the dir pass just made — so clone and init compose in either
        # order without special-casing.
        return _clone_config(config_dir, url)
    if choice == CONFIG_FRESH:
        return _init_fresh_config(config_dir)

    print(f"config:  {config_dir} left empty (no config source)")
    return 0


def _run_init(
    workspace: Path,
    *,
    config_url: str | None = None,
    config_fresh: bool = False,
    no_config: bool = False,
    isatty: bool | None = None,
) -> int:
    """Ensure the workspace has the managed directories the broker acts on.

    Creates ``mono-repos-bare/``, ``mono-work/`` and ``mono-config/`` in the
    workspace root if they are missing, then settles what ``mono-config/`` should
    *contain* — a clone, a fresh repo, or nothing. Idempotent: already-present
    directories are left untouched and an existing config repo is never touched.

    The plain directories are made first so a failed clone still leaves a workspace
    that ``mproj init`` can simply be re-run against.
    """
    print(f"workspace: {workspace}")

    created = []
    for name in INIT_DIRS:
        target = workspace / name
        if target.is_dir():
            print(f"exists:  {target}")
        else:
            target.mkdir(parents=True, exist_ok=True)
            created.append(target)
            print(f"created: {target}")

    if not created:
        print("all workspace directories already exist")

    if isatty is None:
        isatty = sys.stdin.isatty()

    return _ensure_config(
        workspace,
        config_url=config_url,
        config_fresh=config_fresh,
        no_config=no_config,
        isatty=isatty,
    )


def _warn_if_workspace_incomplete(workspace: Path) -> None:
    """Warn (and suggest `mproj init`) for any missing managed workspace dir.

    The broker now performs git/FS effects on these host paths directly (they feed
    the ``HostContext``), so they are no longer bind-mounted into the container.
    But they must still EXIST on the host for the broker to work, so the
    missing-dir hint that used to ride along with the (now removed) bind mounts is
    preserved here as a pure existence check that mounts nothing.
    """
    for name in INIT_DIRS:  # mono-repos-bare, mono-work, mono-config
        source = workspace / name
        if not source.is_dir():
            print(f"warning: {source} does not exist; run `mproj init`.", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Layer 2: `doctor` — the live "can I actually run a container?" test
# --------------------------------------------------------------------------- #
# How long the live container test waits. Starting a container from an image that
# is already local takes a second or two; a wedged engine accepts the *create* and
# then never starts it, so the only thing that distinguishes the two is a clock.
_LIVE_TEST_TIMEOUT = 30.0

# What the live test runs. `true` exits 0 immediately and exists in every image this
# shim deals with, so the test measures the engine's ability to start a container
# rather than anything about the workload.
_LIVE_TEST_ENTRYPOINT = "true"

_WEDGED_ENGINE_HINT = (
    "the docker engine accepted the container but never started it. This is the "
    "engine wedging, not a mono-control problem - restart Docker Desktop, and if "
    "that does not take, `wsl --shutdown` first (Windows) before starting it again."
)


def _check(ok: bool, label: str, detail: str) -> bool:
    """Print one doctor line and pass the verdict back through."""
    print(f"  [{'ok' if ok else 'FAIL'}] {label}: {detail}")
    return ok


def _local_image(docker: str) -> str | None:
    """The mono-control image present locally, or None.

    Deliberately never pulls. A doctor that reaches the network to test the local
    engine would both fail offline and prove less: the point is whether *this*
    machine can start a container from an image it already has.
    """
    probe = subprocess.run(
        [docker, "image", "inspect", MONO_CONTROL_IMAGE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=_PROBE_TIMEOUT,
        check=False,
    )
    return MONO_CONTROL_IMAGE if probe.returncode == 0 else None


def _live_container_test(docker: str, image: str) -> tuple[bool, str]:
    """Start a throwaway container and wait, bounded, for it to finish.

    This is the check that ``docker info`` cannot make. A timeout here is the
    signature of a wedged engine: create succeeds, start never happens, and every
    `mproj control` hangs forever with no explanation.
    """
    try:
        result = subprocess.run(
            [docker, "run", "--rm", "--entrypoint", _LIVE_TEST_ENTRYPOINT, image],
            capture_output=True,
            text=True,
            timeout=_LIVE_TEST_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"a container from {image} did not start within "
            f"{int(_LIVE_TEST_TIMEOUT)}s - {_WEDGED_ENGINE_HINT}"
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"could not run the live container test: {e}"

    if result.returncode != 0:
        return False, f"container exited {result.returncode}: {result.stderr.strip()}"
    return True, f"started and exited cleanly ({image})"


def _run_doctor(workspace: Path | None) -> int:
    """`mproj doctor` — the deep check, run only when asked.

    Reports the workspace, then escalates through docker: on PATH, daemon
    answering, and finally a real container start. Everything cheap runs first so a
    plainly broken environment is named without waiting on the expensive test.

    *workspace* may be None — a workspace that cannot be located is a finding to
    report, not a reason to refuse to diagnose the rest.
    """
    ok = True

    print("workspace:")
    if workspace is None:
        ok = _check(
            False,
            "location",
            f"no directory containing {WORKSPACE_MARKER}/ found - run `mproj init`",
        ) and ok
    else:
        _check(True, "location", str(workspace))
        for name in INIT_DIRS:
            target = workspace / name
            ok = _check(
                target.is_dir(), name, "present" if target.is_dir() else "missing - run `mproj init`"
            ) and ok
        state = _config_state(workspace / WORKSPACE_MARKER)
        ok = _check(
            state == CONFIG_REPO,
            "config repo",
            {
                CONFIG_REPO: "a git repo",
                CONFIG_EMPTY: "empty - run `mproj init --config-url URL`",
                CONFIG_OCCUPIED: "has contents but is not a git repo",
            }[state],
        ) and ok

    print("docker:")
    docker = shutil.which("docker")
    if docker is None:
        _check(False, "cli", "not found on PATH")
        return 1
    _check(True, "cli", docker)

    try:
        info = subprocess.run(
            [docker, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
        reachable = info.returncode == 0
        detail = "daemon answered" if reachable else "daemon returned an error"
    except (OSError, subprocess.SubprocessError):
        reachable, detail = False, "daemon did not answer in time"
    if not _check(reachable, "daemon", detail):
        return 1

    # The live test needs a local image. Its absence is a finding of its own, not a
    # reason to pull — and not a reason to call the engine healthy either.
    # Which backend this workspace will use. Reported for its own sake — the rest of
    # this cannot be read correctly without it — and because it decides whether a
    # missing artifact image is a failure or an irrelevance.
    mode = MODE_ARTIFACT
    if workspace is not None:
        print("mode:")
        mode, mode_ready, mode_detail = _mode_status(workspace, docker)
        ok = _check(mode_ready, mode, mode_detail) and ok

    try:
        image = _local_image(docker)
    except (OSError, subprocess.SubprocessError):
        image = None
    if image is None:
        # Absence means different things per mode: artifact mode cannot run at all
        # without this image, while dev mode never uses it — so failing a dev
        # workspace over it would report it broken for an image it does not need.
        detail = (
            f"no local {MONO_CONTROL_IMAGE} to test the engine with - run "
            "`mproj build-control` first"
        )
        if mode == MODE_ARTIFACT:
            _check(False, "live container test", detail)
            return 1
        _check(True, "live container test", f"skipped: {detail} (dev mode does not use it)")
        return 0 if ok else 1

    live_ok, live_detail = _live_container_test(docker, image)
    ok = _check(live_ok, "live container test", live_detail) and ok

    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# The startup watchdog: a hint, never a kill
# --------------------------------------------------------------------------- #
# How long a container run may go without the watchdog looking into it. Normal
# starts are a second or two; a first-ever run builds the image first, which is
# slow but creates no stuck container, so a build will not trip this.
_STARTUP_HINT_AFTER = 20.0

_STARTUP_HINT = (
    "note: the container has not started yet. If this does not move, the docker "
    "engine may be wedged - run `mproj doctor` in another terminal. Still waiting; "
    "nothing has been cancelled."
)


def _containers_stuck_created(docker: str) -> bool:
    """True if docker shows a container stuck in ``created``, or cannot answer.

    ``created`` without ``running`` is the fingerprint of the wedge: the engine took
    the container and never started it. A query that times out is treated as the
    same symptom, because a daemon too wedged to answer ``ps`` is exactly the
    condition worth reporting.
    """
    try:
        result = subprocess.run(
            [docker, "ps", "--filter", "status=created", "--quiet"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return result.returncode == 0 and bool(result.stdout.strip())


def _watch_startup(docker: str, done: threading.Event) -> None:
    """Wait out the grace period, then hint if the container never started.

    Never touches the child process. A long `control` session or a slow
    `test-control` is legitimate and must not be interrupted by a diagnostic — so
    the watchdog's entire power is one line on stderr, printed once.
    """
    if done.wait(_STARTUP_HINT_AFTER):
        return  # finished inside the grace period: nothing to say
    if _containers_stuck_created(docker):
        print(_STARTUP_HINT, file=sys.stderr)


# Persistent uv cache volume so repeated `--rm` runs (e.g. test-control) don't
# re-download dependencies every time.
_UV_CACHE_VOLUME = "mono-control-uv-cache"
# Container-side venv path for `uv run`, so it never tries to reuse the host's
# (possibly Windows) .venv that the live-source mount exposes.
_TEST_VENV = "/home/codespace/.mono-control-test-venv"


def _dispatch(
    workspace: Path,
    inner_argv: list[str],
    *,
    build: bool = False,
    dev_only: bool = False,
    artifact: bool = False,
    env: dict[str, str] | None = None,
    stdout_path: Path | None = None,
) -> int:
    """Run *inner_argv* inside the mono-control container.

    Two backends: **dev mode** runs Docker Compose against a live `mono-control/`
    checkout; **artifact mode** runs the prebuilt image (mono-control:latest)
    directly. The backend is chosen by checkout presence, but ``artifact=True``
    forces artifact mode even when a checkout is present. ``dev_only`` operations
    (e.g. tests) refuse to run in artifact mode — there is no source to act on.
    """
    docker = shutil.which("docker")
    if docker is None:
        print("error: docker not found on PATH", file=sys.stderr)
        return 1
    # The shim is the host-side authority for the host platform: detect it and
    # inject it on every container run (last, so it is authoritative over any
    # caller-supplied env). Refuse on an unmappable host rather than guess.
    try:
        host_platform = _detect_host_platform()
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    env = {**(env or {}), HOST_PLATFORM_ENV: host_platform}

    # No GitHub credential is resolved or carried anymore. The broker performs every
    # git effect on the host, as the developer, so host git resolves credentials from
    # the host's own machinery (gh helper, Git Credential Manager, OS keyring,
    # ~/.gitconfig). The container is handed nothing.
    secrets: dict[str, str] = {}

    # Stand up the host-callback broker for the lifetime of the container run, so the
    # container can ask the host to do the few things only the host can (see broker.py).
    # It is BEST-EFFORT: a failure to bind must not turn a working command into a
    # broken one. Warn and run without it.
    #
    # Importing the verb packs is what registers their handlers (@verb runs at import);
    # the host context carries the host paths those handlers act on — knowledge the
    # container cannot have and must not be handed. The paths are the managed workspace
    # dirs (INIT_DIRS): mono-repos-bare holds the bare repositories, mono-work holds the
    # worktrees they are materialized into, mono-config the manifest dir.
    from mono_control_shim import verbs  # noqa: F401  (import = register packs)

    host_context = HostContext(
        work_root=workspace / "mono-work",
        bare_root=workspace / "mono-repos-bare",
        config_dir=workspace / "mono-config",
    )

    broker: BrokerServer | None = None
    try:
        broker = BrokerServer(host_context)
        broker.start()
    except OSError as e:
        print(
            f"warning: host-callback broker could not start ({e}); "
            "running without it.",
            file=sys.stderr,
        )
        broker = None

    if broker is not None:
        # Coordinates are not secret — they are useless without the token — so they
        # take the ordinary `-e KEY=VALUE` path...
        env[BROKER_HOST_ENV] = BROKER_CONTAINER_HOST
        env[BROKER_PORT_ENV] = str(broker.port)
        # ...and the token takes the GitHub token's route: named in argv, valued only
        # in the environment handed to docker.
        secrets[BROKER_TOKEN_ENV] = broker.token

    try:
        if (workspace / "mono-control").is_dir() and not artifact:
            return _dev_run(
                docker, workspace, inner_argv, build=build, env=env, secrets=secrets,
                stdout_path=stdout_path,
            )
        if dev_only:
            print(
                "error: this operation requires a mono-control/ checkout (dev mode).",
                file=sys.stderr,
            )
            return 1
        if build:
            print(
                "warning: --build has no effect in artifact mode (no mono-control source).",
                file=sys.stderr,
            )
        return _artifact_run(
            docker, workspace, inner_argv, env=env, secrets=secrets,
            stdout_path=stdout_path,
        )
    finally:
        # The broker's authority is scoped to exactly one container run. Ending it here
        # means the token dies with the command that issued it.
        if broker is not None:
            broker.stop()


def _secret_args(secrets: dict[str, str] | None) -> list[str]:
    """Render *secrets* as valueless ``-e NAME`` flags.

    Both `docker run` and `docker compose run` read a valueless ``-e NAME`` from the
    environment of the process invoking them — which is exactly what we want: the name
    goes in argv, the value does not.
    """
    args: list[str] = []
    for key in secrets or {}:
        args += ["-e", key]
    return args


def _secret_environ(secrets: dict[str, str] | None) -> dict[str, str] | None:
    """Our environment plus *secrets*, to hand to docker; ``None`` when there are none.

    ``None`` means "inherit ours unchanged", which is ``subprocess``'s default and
    keeps the no-token path byte-for-byte what it was before secrets existed.
    """
    if not secrets:
        return None
    return {**os.environ, **secrets}


def _dev_run(
    docker: str,
    workspace: Path,
    inner_argv: list[str],
    *,
    build: bool = False,
    env: dict[str, str] | None = None,
    secrets: dict[str, str] | None = None,
    stdout_path: Path | None = None,
) -> int:
    """Dev mode: run *inner_argv* via the checked-out mono-control's Compose.

    Runs ``docker-compose.yml`` and bind-mounts the live `mono-control/` checkout
    over the image's baked-in copy, so the editable install resolves to the working
    tree — code edits take effect with no rebuild. ``--build`` is still needed for
    image / dependency changes, which the mount does not shadow.
    """
    compose = workspace / "mono-control" / ".devcontainer" / "docker-compose.yml"
    if not compose.is_file():
        print(
            f"error: mono-control/ is present but its compose file is missing at {compose}",
            file=sys.stderr,
        )
        return 1
    cmd = [docker, "compose", "-f", str(compose), "run", "--rm"]
    if build:
        cmd.append("--build")
    for key, value in (env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd += _secret_args(secrets)
    # Mount live source over the baked copy so working-tree edits are reflected,
    # plus a persistent uv cache so repeated runs don't re-download deps. The managed
    # workspace dirs (mono-repos-bare / mono-work / mono-config) are NOT mounted: the broker
    # touches them on the host. Still warn if they are missing — the broker needs them.
    cmd += ["-v", f"{workspace / 'mono-control'}:/workspaces/mono-control"]
    cmd += ["-v", f"{_UV_CACHE_VOLUME}:/home/codespace/.cache/uv"]
    _warn_if_workspace_incomplete(workspace)
    cmd += ["mono-control", *inner_argv]  # compose service name, then the command
    # Only thread stdout_path when capturing (json-schema-control), so the ordinary
    # streaming call shape is byte-for-byte unchanged.
    extra = {"stdout_path": stdout_path} if stdout_path is not None else {}
    return _exec(cmd, env=_secret_environ(secrets), watch_startup=True, **extra)


def _artifact_run(
    docker: str,
    workspace: Path,
    inner_argv: list[str],
    *,
    env: dict[str, str] | None = None,
    secrets: dict[str, str] | None = None,
    stdout_path: Path | None = None,
) -> int:
    """Artifact mode: run *inner_argv* in the prebuilt image (no source on disk)."""
    # No source to build from here, so detect a missing image and tell the user
    # how to build one from a checkout, rather than failing obscurely.
    probe = subprocess.run(
        [docker, "image", "inspect", MONO_CONTROL_IMAGE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode != 0:
        print(
            f"error: image '{MONO_CONTROL_IMAGE}' not found.\n"
            f"  Build it with `mproj build-control` from a workspace that has a\n"
            f"  mono-control/ checkout, or directly:\n"
            f"    docker build -t {MONO_CONTROL_IMAGE} -f .devcontainer/Dockerfile .\n"
            f"  (Distribution via ghcr.io is planned.)",
            file=sys.stderr,
        )
        return 1
    cmd = [
        docker, "run", "--rm",
        "-e", "MONO_CONTROL_IN_CONTAINER=1",
        "-w", "/workspaces/mono-control",
        # Make `host.docker.internal` (the broker's address from inside the container)
        # resolve on native-Linux hosts too. Docker Desktop provides the name itself
        # and accepts this redundantly, so it is unconditional rather than conditioned
        # on a host-platform check we would then have to keep true.
        "--add-host", "host.docker.internal:host-gateway",
    ]
    for key, value in (env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd += _secret_args(secrets)
    if stdout_path is None and sys.stdin.isatty() and sys.stdout.isatty():
        # No pseudo-TTY when capturing stdout to a file: `-t` would inject terminal
        # control bytes and corrupt the JSON we are trying to save.
        cmd.append("-it")  # interactive (e.g. mono-control repl / shell-control)
    # The managed workspace dirs are not mounted (the broker touches them on the host);
    # still warn if any is missing, since the broker needs them present.
    _warn_if_workspace_incomplete(workspace)
    cmd += [MONO_CONTROL_IMAGE, *inner_argv]
    extra = {"stdout_path": stdout_path} if stdout_path is not None else {}
    return _exec(cmd, env=_secret_environ(secrets), watch_startup=True, **extra)


def _run_control(
    workspace: Path, command_args: list[str], *, build: bool = False, artifact: bool = False
) -> int:
    """`mproj control` — run the mono-control artifact (forward to its own CLI)."""
    return _dispatch(workspace, ["mono-control", *command_args], build=build, artifact=artifact)


def _run_shell_control(workspace: Path, *, artifact: bool = False) -> int:
    """`mproj shell-control` — interactive login shell in the artifact container."""
    return _dispatch(workspace, ["bash", "-l"], artifact=artifact)


def _run_test_control(workspace: Path, command_args: list[str]) -> int:
    """`mproj test-control` — run mono-control's test suite (dev only)."""
    return _dispatch(
        workspace,
        ["uv", "run", "pytest", *command_args],
        dev_only=True,
        # Redirect uv's project venv off the live-source mount (the host .venv it
        # exposes); UV_LINK_MODE=copy avoids a noisy hardlink warning because the
        # cache volume and that venv live on different filesystems. UV_LOCKED makes
        # `uv run` install exactly what uv.lock pins — the image build is already
        # locked, and this was the last uv path that still resolved freely.
        env={
            "UV_PROJECT_ENVIRONMENT": _TEST_VENV,
            "UV_LINK_MODE": "copy",
            "UV_LOCKED": "1",
        },
    )


# Where `json-schema-control` writes the emitted wire contract. Inside this
# package's tree so the generated file is checked in and diffable — the host side
# implements to this schema, so a drift shows up in review.
SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "wire-schema.json"


def _run_json_schema_control(workspace: Path) -> int:
    """`mproj json-schema-control` — refresh this repo's checked-in wire schema.

    Runs the container's ``mono-control emit-schema`` and captures its stdout (the
    JSON Schema of the broker's 20 wire models) into ``SCHEMA_PATH``. Sibling of
    ``test-control``; like it, it needs the container (dev mode via Compose, or the
    prebuilt artifact image). The broker's own diagnostics stay on stderr, so only
    the schema JSON lands in the file.
    """
    rc = _dispatch(
        workspace, ["mono-control", "emit-schema"], stdout_path=SCHEMA_PATH
    )
    if rc == 0:
        print(f"wrote: {SCHEMA_PATH}", file=sys.stderr)
    return rc


def _exec(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    stdout_path: Path | None = None,
    watch_startup: bool = False,
) -> int:
    """Run *cmd*, inheriting stdio, and return its exit code as ours.

    ``env`` replaces the child's environment wholesale; ``None`` inherits ours. It is
    how secrets reach docker without passing through argv (see ``_secret_environ``).

    ``stdout_path`` redirects the child's stdout to that file (stderr still streams
    to ours) — how ``json-schema-control`` captures ``emit-schema``'s JSON into the
    repo while the broker's diagnostics stay on the terminal.

    ``watch_startup`` arms the startup watchdog for container runs. It is opt-in
    because there is nothing to watch for on an image build, and because the child's
    exit code and stdio must be exactly what they were without it: the watchdog runs
    on its own daemon thread, reads docker, and prints at most one line.
    """
    watcher: threading.Thread | None = None
    done = threading.Event()
    try:
        if watch_startup and cmd:
            watcher = threading.Thread(
                target=_watch_startup, args=(cmd[0], done), daemon=True
            )
            watcher.start()

        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            with open(stdout_path, "w", encoding="utf-8", newline="\n") as out:
                return subprocess.run(cmd, check=False, env=env, stdout=out).returncode
        return subprocess.run(cmd, check=False, env=env).returncode
    except (OSError, subprocess.SubprocessError) as e:
        print(f"error: failed to launch container: {e}", file=sys.stderr)
        return 1
    finally:
        # Release the watchdog whichever way we left: a finished run must not be
        # followed by a hint about it not having started.
        done.set()


def _run_build_control(workspace: Path) -> int:
    """Build the canonical mono-control image (``mono-control:latest``) locally.

    Builds from the workspace's `mono-control/` checkout — the same standalone
    `docker build` the artifact-mode error suggests — so artifact-mode `control`
    (and any other consumer) can find the image in the local docker store.
    Requires the source checkout; a checkout-less workspace has nothing to build
    from. This is also the natural seam for a future `--push` to ghcr.io.
    """
    source = workspace / "mono-control"
    dockerfile = source / ".devcontainer" / "Dockerfile"
    if not dockerfile.is_file():
        print(
            f"error: no mono-control checkout to build from.\n"
            f"  Expected a Dockerfile at {dockerfile}.\n"
            f"  `build-control` needs the source - clone mono-control/ beside mono-config/.",
            file=sys.stderr,
        )
        return 1
    docker = shutil.which("docker")
    if docker is None:
        print("error: docker not found on PATH", file=sys.stderr)
        return 1
    cmd = [docker, "build", "-t", MONO_CONTROL_IMAGE, "-f", str(dockerfile), str(source)]
    return _exec(cmd)


def _add_workspace_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace",
        help="Path to the mono workspace root (a directory containing "
        "mono-config/). Falls back to the "
        f"{WORKSPACE_ENV_VAR} env var, then to walking up from the current directory.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mproj",
        description="Thin host shim that locates the mono workspace and hands off to mono-control.",
    )
    _add_workspace_arg(parser)

    subparsers = parser.add_subparsers(dest="command")
    init_parser = subparsers.add_parser(
        "init",
        help="Create the mono-repos-bare/, mono-work/ and mono-config/ "
        "directories the broker acts on, and populate mono-config/ by cloning an "
        "existing config repo or creating a fresh one. Asks when run on a terminal "
        "with none of the flags below.",
    )
    _add_workspace_arg(init_parser)
    # Mutually exclusive so a contradictory pair is rejected by the parser rather
    # than resolved by a precedence rule nobody can remember.
    config_source = init_parser.add_mutually_exclusive_group()
    config_source.add_argument(
        "--config-url",
        metavar="URL",
        help="Clone the config repo from URL into mono-config/ (no prompt).",
    )
    config_source.add_argument(
        "--config-fresh",
        action="store_true",
        help="Create a fresh, empty config repo in mono-config/ (no prompt).",
    )
    config_source.add_argument(
        "--no-config",
        action="store_true",
        help="Create mono-config/ as an empty directory and nothing more - the "
        "behavior of `init` before it learned to populate it (no prompt).",
    )

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Diagnose the workspace and the docker engine, including a live test "
        "that a container can actually start (which plain reachability cannot prove).",
    )
    _add_workspace_arg(doctor_parser)

    control_parser = subparsers.add_parser(
        "control",
        help="Run mono-control inside its container against the workspace: dev "
        "mode (Docker Compose) when a mono-control/ checkout is present, else "
        "artifact mode (the prebuilt image).",
    )
    _add_workspace_arg(control_parser)
    control_parser.add_argument(
        "--build",
        action="store_true",
        help="Dev mode only: rebuild the image before running (picks up "
        "mono-control source changes).",
    )
    control_parser.add_argument(
        "--artifact",
        action="store_true",
        help="Run the built artifact image (mono-control:latest) instead of the "
        "live checkout, even when a mono-control/ checkout is present.",
    )
    control_parser.add_argument(
        "command_args",
        nargs="*",
        help="Arguments forwarded to mono-control. Precede flags with -- "
        "(e.g. `mproj control -- --version`).",
    )

    build_parser = subparsers.add_parser(
        "build-control",
        help="Build the mono-control image (mono-control:latest) from the "
        "workspace's mono-control/ checkout, for artifact-mode `control` to run.",
    )
    _add_workspace_arg(build_parser)

    shell_parser = subparsers.add_parser(
        "shell-control",
        help="Open an interactive shell inside the mono-control container "
        "(dev mode: live source via Compose; artifact mode: the prebuilt image).",
    )
    _add_workspace_arg(shell_parser)
    shell_parser.add_argument(
        "--artifact",
        action="store_true",
        help="Shell into the built artifact image instead of the live checkout.",
    )

    test_parser = subparsers.add_parser(
        "test-control",
        help="Run mono-control's test suite inside the dev container "
        "(requires a mono-control/ checkout).",
    )
    _add_workspace_arg(test_parser)
    test_parser.add_argument(
        "command_args",
        nargs="*",
        help="Arguments forwarded to pytest. Precede flags with -- "
        "(e.g. `mproj test-control -- -k foo -q`).",
    )

    schema_parser = subparsers.add_parser(
        "json-schema-control",
        help="Emit mono-control's broker wire-contract JSON Schema into this "
        f"repo ({SCHEMA_PATH.name}), for a checked-in, diffable contract.",
    )
    _add_workspace_arg(schema_parser)

    args = parser.parse_args(argv)

    # `init` bootstraps the workspace, so it resolves its target without
    # requiring the mono-config marker to already exist.
    if args.command == "init":
        return _run_init(
            _resolve_init_target(args.workspace),
            config_url=args.config_url,
            config_fresh=args.config_fresh,
            no_config=args.no_config,
        )

    # `doctor` diagnoses; a workspace it cannot find is one of its findings, so it
    # runs before the gate that turns that into a hard error.
    if args.command == "doctor":
        return _run_doctor(resolve_workspace(args.workspace))

    workspace = resolve_workspace(args.workspace)
    if workspace is None:
        print(
            "error: could not locate a mono workspace.\n"
            f"  Looked for a directory containing {WORKSPACE_MARKER}/.\n"
            "  Pass --workspace PATH, set "
            f"{WORKSPACE_ENV_VAR}, or run from inside a workspace.",
            file=sys.stderr,
        )
        return 1

    if args.command == "control":
        return _run_control(
            workspace, args.command_args, build=args.build, artifact=args.artifact
        )

    if args.command == "build-control":
        return _run_build_control(workspace)

    if args.command == "shell-control":
        return _run_shell_control(workspace, artifact=args.artifact)

    if args.command == "test-control":
        return _run_test_control(workspace, args.command_args)

    if args.command == "json-schema-control":
        return _run_json_schema_control(workspace)

    return _run_status(workspace)


if __name__ == "__main__":
    raise SystemExit(main())
