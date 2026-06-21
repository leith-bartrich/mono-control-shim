"""Host-installed shim CLI for mono-control.

This module is deliberately stdlib-only. It is installed on the *host* and its
only job is to locate the mono workspace and hand off to the real mono-control
tooling (which runs inside a dev container). Keeping the host surface minimal
and dependency-free is a security goal, not an accident.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# A directory is recognized as a mono workspace when it contains the manifest
# directory `mono-config/`. A `mono-control/` checkout may or may not sit beside
# it; its presence selects dev vs. prod execution (see `control`), not whether
# this is a workspace.
WORKSPACE_MARKER = "mono-config"

# Env var consulted when --workspace is not passed.
WORKSPACE_ENV_VAR = "MONO_WORKSPACE"

# Canonical local image ref for prod mode (no mono-control/ checkout present).
# Distribution via ghcr.io is planned; for now it is built from a checkout.
MONO_CONTROL_IMAGE = "mono-control:latest"


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


# Directories that `mproj init` ensures exist in the workspace root. These are
# the bind-mount sources the mono-control dev container expects to find.
INIT_DIRS = ("mono-repos", "mono-config")


def _run_status(workspace: Path) -> int:
    """Default command: report the workspace and dev container availability."""
    print(f"workspace: {workspace}")

    available, detail = _dev_container_available(workspace)
    status = "available" if available else "unavailable"
    print(f"mono-control dev container: {status} ({detail})")

    return 0


def _run_init(workspace: Path) -> int:
    """Ensure the workspace has the directories the dev container bind-mounts.

    Creates ``mono-repos/`` and ``mono-config/`` in the workspace root if they
    are missing. Idempotent: already-present directories are left untouched.
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


def _volume_args(workspace: Path) -> list[str]:
    """Per-call bind mounts for the managed workspace dirs.

    The container-side targets are fixed (they match src/mono_control/paths.py in
    mono-control); only the host sources vary per invocation.
    """
    args: list[str] = []
    for name in INIT_DIRS:  # mono-repos, mono-config
        source = workspace / name
        if not source.is_dir():
            print(f"warning: {source} does not exist; run `mproj init`.", file=sys.stderr)
        args += ["-v", f"{source}:/workspaces/{name}"]
    return args


def _run_control(workspace: Path, command_args: list[str], *, build: bool = False) -> int:
    """Hand off to mono-control inside a container.

    The presence of a `mono-control/` checkout selects the backend: Docker
    Compose against live source (dev), or the prebuilt image directly (prod).
    """
    docker = shutil.which("docker")
    if docker is None:
        print("error: docker not found on PATH", file=sys.stderr)
        return 1
    if (workspace / "mono-control").is_dir():
        return _control_dev(docker, workspace, command_args, build=build)
    return _control_prod(docker, workspace, command_args, build=build)


def _control_dev(
    docker: str, workspace: Path, command_args: list[str], *, build: bool
) -> int:
    """Dev mode: run the checked-out mono-control via its Docker Compose.

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
    # Mount live source over the baked copy so working-tree edits are reflected.
    cmd += ["-v", f"{workspace / 'mono-control'}:/workspaces/mono-control"]
    cmd += _volume_args(workspace)
    cmd += ["mono-control", "mono-control", *command_args]
    return _exec(cmd)


def _control_prod(
    docker: str, workspace: Path, command_args: list[str], *, build: bool
) -> int:
    """Prod mode: run the prebuilt image directly (no source on disk)."""
    if build:
        print(
            "warning: --build has no effect in prod mode (no mono-control source).",
            file=sys.stderr,
        )
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
    ]
    if sys.stdin.isatty() and sys.stdout.isatty():
        cmd.append("-it")  # interactive (e.g. mono-control repl); compose does this in dev
    cmd += _volume_args(workspace)
    cmd += [MONO_CONTROL_IMAGE, "mono-control", *command_args]
    return _exec(cmd)


def _exec(cmd: list[str]) -> int:
    """Run *cmd*, inheriting stdio, and return its exit code as ours."""
    try:
        return subprocess.run(cmd, check=False).returncode
    except (OSError, subprocess.SubprocessError) as e:
        print(f"error: failed to launch container: {e}", file=sys.stderr)
        return 1


def _run_build_control(workspace: Path) -> int:
    """Build the canonical mono-control image (``mono-control:latest``) locally.

    Builds from the workspace's `mono-control/` checkout — the same standalone
    `docker build` the prod-mode error suggests — so prod-mode `control` (and any
    other consumer) can find the image in the local docker store. Requires the
    source checkout; a prod-only workspace has nothing to build from. This is also
    the natural seam for a future `--push` to ghcr.io (see docs/todo).
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
        help="Create the mono-repos/ and mono-config/ directories the dev "
        "container bind-mounts, if they do not already exist.",
    )
    _add_workspace_arg(init_parser)

    control_parser = subparsers.add_parser(
        "control",
        help="Run mono-control inside its container against the workspace: dev "
        "mode (Docker Compose) when a mono-control/ checkout is present, else "
        "prod mode (the prebuilt image).",
    )
    _add_workspace_arg(control_parser)
    control_parser.add_argument(
        "--build",
        action="store_true",
        help="Dev mode only: rebuild the image before running (picks up "
        "mono-control source changes).",
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
        "workspace's mono-control/ checkout, for prod-mode `control` to run.",
    )
    _add_workspace_arg(build_parser)

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
        return _run_control(workspace, args.command_args, build=args.build)

    if args.command == "build-control":
        return _run_build_control(workspace)

    return _run_status(workspace)


if __name__ == "__main__":
    raise SystemExit(main())
