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
from pathlib import Path

from mono_control_shim.broker import BrokerServer, HostContext

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

# GitHub credential carried into the container (see mono-control's
# docs/design/github-auth.md). Like the host platform, this is host-side knowledge the
# container cannot obtain for itself — a credential lives in an OS keyring no Linux
# container can reach — so the shim is again the component that must supply it.
#
# Unlike the host platform it is a SECRET, which changes how it is passed, not whether:
# it is put in the environment handed to `docker` and named by a VALUELESS `-e`, so it
# never appears in this process's argv (where any local process could read it off the
# process table). It is never printed, and never given a value on a command line.
GITHUB_TOKEN_ENV = "MONO_CONTROL_GITHUB_TOKEN"

# Environment variables consulted for a token, in order, before falling back to `gh`.
# The first is our own; the rest are the ecosystem's de-facto standards, honored so a
# CI runner or an existing shell setup works without special-casing mono-control.
_TOKEN_ENV_VARS = (GITHUB_TOKEN_ENV, "GH_TOKEN", "GITHUB_TOKEN")

# Host-callback broker coordinates carried into the container (see broker.py). Same
# shape as the two above — host-side knowledge the container cannot derive — split
# by sensitivity: the host and port are not secret and ride the `-e KEY=VALUE` path,
# while the per-run token is a SECRET and takes the same valueless-`-e` route as the
# GitHub token, so it never appears in argv.
#
# `host.docker.internal` is the name Docker Desktop gives the host from inside a
# container. The shim names it rather than an address because the address is not
# stable across Docker networks, and the container has no way to learn either.
BROKER_HOST_ENV = "MONO_BROKER_HOST"
BROKER_PORT_ENV = "MONO_BROKER_PORT"
BROKER_TOKEN_ENV = "MONO_BROKER_TOKEN"

BROKER_CONTAINER_HOST = "host.docker.internal"

_GH_FALLBACK_WARNING = (
    f"warning: no {GITHUB_TOKEN_ENV} set; falling back to your `gh` OAuth token.\n"
    "  That token typically carries repo + workflow + gist WRITE access to every repo\n"
    "  you own, but mono-control only ever reads (clone / ls-remote / checkout).\n"
    f"  Prefer a fine-grained PAT with read-only Contents, exported as {GITHUB_TOKEN_ENV}."
)


def _resolve_github_token() -> str | None:
    """Return a GitHub token for the container, or ``None`` if none is available.

    Precedence mirrors ``_detect_host_platform``: an explicit environment value wins,
    otherwise we go and find one. Here "find one" means asking `gh` for the token it
    already holds in the host keyring.

    Returning ``None`` is a normal outcome, NOT an error — public remotes need no
    credential and most of mono-control needs no network at all. A private remote with
    no token fails inside the container, where the failure is specific and can say so;
    guessing here that the user will need one would break every offline invocation.

    The token is never logged. The `gh` fallback prints a warning naming the write
    scopes it drags along, so the convenient path is never also the silent one.
    """
    for name in _TOKEN_ENV_VARS:
        token = os.environ.get(name)
        if token:
            return token

    gh = shutil.which("gh")
    if gh is None:
        return None
    try:
        result = subprocess.run(
            [gh, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None  # gh present but not logged in.

    token = result.stdout.strip()
    if not token:
        return None
    print(_GH_FALLBACK_WARNING, file=sys.stderr)
    return token


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


def _dev_container_available(workspace: Path) -> tuple[bool, str]:
    """Best-effort check for mono-control's dev container availability.

    Returns (available, human_readable_detail). Stdlib only; never raises.
    """
    control = workspace / "mono-control"

    devcontainer = control / ".devcontainer"
    has_config = (
        devcontainer.is_dir()
        or (control / ".devcontainer.json").is_file()
    )

    docker = shutil.which("docker")
    if docker is None:
        detail = "docker not found on PATH"
        return False, detail

    if not has_config:
        return False, f"docker found ({docker}) but no .devcontainer config in mono-control"

    # docker exists and config exists; probe the daemon non-fatally.
    try:
        result = subprocess.run(
            [docker, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        daemon_up = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        daemon_up = False

    if not daemon_up:
        return False, "docker installed and .devcontainer present, but docker daemon is not responding"

    return True, "docker daemon reachable and .devcontainer config present"


# Directories that `mproj init` ensures exist in the workspace root. The broker acts
# on these host-side dirs directly (they feed the HostContext), so they are no longer
# bind-mounted into the container, but they MUST still exist on the host: mono-repos is
# the materialized root, mono-repos-offline the non-destructive retire holding area
# (whose unpushed work would be lost if it were absent), and mono-config the manifest dir.
INIT_DIRS = ("mono-repos", "mono-repos-offline", "mono-config")


def _run_status(workspace: Path) -> int:
    """Default command: report the workspace and dev container availability."""
    print(f"workspace: {workspace}")

    available, detail = _dev_container_available(workspace)
    status = "available" if available else "unavailable"
    print(f"mono-control dev container: {status} ({detail})")

    return 0


def _run_init(workspace: Path) -> int:
    """Ensure the workspace has the directories the dev container bind-mounts.

    Creates ``mono-repos/``, ``mono-repos-offline/`` and ``mono-config/`` in the
    workspace root if they are missing. Idempotent: already-present directories are
    left untouched.
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
        print("nothing to do: all workspace directories already exist")

    return 0


def _warn_if_workspace_incomplete(workspace: Path) -> None:
    """Warn (and suggest `mproj init`) for any missing managed workspace dir.

    The broker now performs git/FS effects on these host paths directly (they feed
    the ``HostContext``), so they are no longer bind-mounted into the container.
    But they must still EXIST on the host for the broker to work, so the
    missing-dir hint that used to ride along with the (now removed) bind mounts is
    preserved here as a pure existence check that mounts nothing.
    """
    for name in INIT_DIRS:  # mono-repos, mono-repos-offline, mono-config
        source = workspace / name
        if not source.is_dir():
            print(f"warning: {source} does not exist; run `mproj init`.", file=sys.stderr)


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

    # The shim is likewise the host-side authority for the GitHub credential, for the
    # same reason: the container cannot reach the host keyring. It is no longer handed
    # to the container at all — the broker now performs every git effect on the host and
    # holds this token itself (see the HostContext below). We still resolve it here (and
    # warn on the `gh` fallback) precisely to feed that HostContext. No token is a normal
    # state, not an error.
    secrets: dict[str, str] = {}
    token = _resolve_github_token()

    # Stand up the host-callback broker for the lifetime of the container run, so the
    # container can ask the host to do the few things only the host can (see broker.py).
    # It is BEST-EFFORT: a failure to bind must not turn a working command into a
    # broken one. Warn and run without it.
    #
    # Importing the verb packs is what registers their handlers (@verb runs at import);
    # the host context carries the host paths + token those handlers act on — knowledge
    # the container cannot have and must not be handed. The paths are the managed
    # workspace dirs (INIT_DIRS): mono-repos is the materialized root, mono-repos-offline
    # the offline holding area, mono-config the manifest dir.
    from mono_control_shim import verbs  # noqa: F401  (import = register packs)

    host_context = HostContext(
        workspace_root=workspace / "mono-repos",
        offline_root=workspace / "mono-repos-offline",
        config_dir=workspace / "mono-config",
        github_token=token,
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

    Runs the base ``docker-compose.yml`` (not the VS Code overlay) and bind-mounts
    the live `mono-control/` checkout over the image's baked-in copy, so the
    editable install resolves to the working tree — code edits take effect with no
    rebuild. ``--build`` is still needed for image / dependency changes.
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
    # workspace dirs (mono-repos / -offline / mono-config) are NOT mounted: the broker
    # touches them on the host. Still warn if they are missing — the broker needs them.
    cmd += ["-v", f"{workspace / 'mono-control'}:/workspaces/mono-control"]
    cmd += ["-v", f"{_UV_CACHE_VOLUME}:/home/codespace/.cache/uv"]
    _warn_if_workspace_incomplete(workspace)
    cmd += ["mono-control", *inner_argv]  # compose service name, then the command
    # Only thread stdout_path when capturing (json-schema-control), so the ordinary
    # streaming call shape is byte-for-byte unchanged.
    extra = {"stdout_path": stdout_path} if stdout_path is not None else {}
    return _exec(cmd, env=_secret_environ(secrets), **extra)


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
    return _exec(cmd, env=_secret_environ(secrets), **extra)


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
) -> int:
    """Run *cmd*, inheriting stdio, and return its exit code as ours.

    ``env`` replaces the child's environment wholesale; ``None`` inherits ours. It is
    how secrets reach docker without passing through argv (see ``_secret_environ``).

    ``stdout_path`` redirects the child's stdout to that file (stderr still streams
    to ours) — how ``json-schema-control`` captures ``emit-schema``'s JSON into the
    repo while the broker's diagnostics stay on the terminal.
    """
    try:
        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            with open(stdout_path, "w", encoding="utf-8", newline="\n") as out:
                return subprocess.run(cmd, check=False, env=env, stdout=out).returncode
        return subprocess.run(cmd, check=False, env=env).returncode
    except (OSError, subprocess.SubprocessError) as e:
        print(f"error: failed to launch container: {e}", file=sys.stderr)
        return 1


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
            f"  `build-control` needs the source — clone mono-control/ beside mono-config/.",
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
        help="Create the mono-repos/, mono-repos-offline/ and mono-config/ "
        "directories the container bind-mounts, if they do not already exist.",
    )
    _add_workspace_arg(init_parser)

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
        return _run_init(_resolve_init_target(args.workspace))

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
