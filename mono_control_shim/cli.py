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

# A directory is recognized as a mono workspace when it contains both of these
# as immediate subdirectories.
WORKSPACE_MARKERS = ("mono-control", "mono-config")

# Env var consulted when --workspace is not passed.
WORKSPACE_ENV_VAR = "MONO_WORKSPACE"


def _is_workspace(path: Path) -> bool:
    """True if *path* looks like a mono workspace root."""
    return all((path / marker).is_dir() for marker in WORKSPACE_MARKERS)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mono",
        description="Thin host shim that locates the mono workspace and hands off to mono-control.",
    )
    parser.add_argument(
        "--workspace",
        help="Path to the mono workspace root (a directory containing "
        "mono-control/ and mono-config/). Falls back to the "
        f"{WORKSPACE_ENV_VAR} env var, then to walking up from the current directory.",
    )
    args = parser.parse_args(argv)

    workspace = resolve_workspace(args.workspace)
    if workspace is None:
        print(
            "error: could not locate a mono workspace.\n"
            "  Looked for a directory containing both "
            f"{WORKSPACE_MARKERS[0]}/ and {WORKSPACE_MARKERS[1]}/.\n"
            "  Pass --workspace PATH, set "
            f"{WORKSPACE_ENV_VAR}, or run from inside a workspace.",
            file=sys.stderr,
        )
        return 1

    print(f"workspace: {workspace}")

    available, detail = _dev_container_available(workspace)
    status = "available" if available else "unavailable"
    print(f"mono-control dev container: {status} ({detail})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
