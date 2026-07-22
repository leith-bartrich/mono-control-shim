"""The ``mono_config`` verb pack: host-side reads/writes of ``mono-config/``.

This re-hosts the file I/O of the container's old ``RepoStore`` (the directory
*is* the registry: one ``<slug>.json`` per repo, plus a single ``system.json``),
so the container needs no ``mono-config/`` mount — it asks the broker for the raw
JSON and keeps ownership of pydantic validation and the retire/restore logic.

The container authors this data, so ``save_*`` are legitimate writes across the
seam; the boundary defends only against a *slug* that is secretly a path (which
would let a write escape the config directory). ``get`` verbs read; the single
``purge`` verb hard-deletes, reporting a missing slug with the same server-error
code the executable spec (``broker_shim.py``) uses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from mono_control_shim.broker import SERVER_ERROR, HostContext, VerbError, verb
from mono_control_shim.verbs.git import _require_ctx, _valid_slug


def _repos_dir(ctx: HostContext) -> Path:
    return ctx.config_dir / "repos"


def _repo_def_path(ctx: HostContext, slug: str) -> Path:
    return _repos_dir(ctx) / f"{slug}.json"


@verb("get_repo_defs")
def _get_repo_defs(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Read raw ``<slug>.json`` repo definitions (all, or the named ``slugs``)."""
    ctx = _require_ctx(ctx)
    slugs = params.get("slugs")
    repos: dict[str, Any] = {}
    repos_dir = _repos_dir(ctx)
    if not repos_dir.is_dir():
        return {"repos": repos}
    for path in sorted(repos_dir.glob("*.json")):
        stem = path.stem
        if slugs is not None and stem not in slugs:
            continue
        repos[stem] = json.loads(path.read_text())
    return {"repos": repos}


@verb("save_repo_def")
def _save_repo_def(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Create or overwrite one repo definition (``repo`` is the raw ``<slug>.json``)."""
    ctx = _require_ctx(ctx)
    repo = params.get("repo")
    if not isinstance(repo, dict):
        raise VerbError(SERVER_ERROR, "repo must be an object")
    slug = _valid_slug(repo.get("slug"))
    repos_dir = _repos_dir(ctx)
    repos_dir.mkdir(parents=True, exist_ok=True)
    (repos_dir / f"{slug}.json").write_text(json.dumps(repo, indent=2) + "\n")
    return {"ok": True}


@verb("purge_repo_def")
def _purge_repo_def(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Hard-delete one repo definition (server error if it is absent)."""
    ctx = _require_ctx(ctx)
    slug = _valid_slug(params.get("slug"))
    try:
        _repo_def_path(ctx, slug).unlink()
    except FileNotFoundError as e:
        raise VerbError(SERVER_ERROR, f"repo {slug!r} not found") from e
    return {"ok": True}


@verb("get_system")
def _get_system(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Read the raw ``system.json`` contents (``None`` if absent)."""
    ctx = _require_ctx(ctx)
    path = ctx.config_dir / "system.json"
    if not path.is_file():
        return {"system": None}
    return {"system": json.loads(path.read_text())}


@verb("save_system")
def _save_system(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Create or overwrite ``system.json``."""
    ctx = _require_ctx(ctx)
    system = params.get("system")
    if not isinstance(system, dict):
        raise VerbError(SERVER_ERROR, "system must be an object")
    ctx.config_dir.mkdir(parents=True, exist_ok=True)
    (ctx.config_dir / "system.json").write_text(json.dumps(system, indent=2) + "\n")
    return {"ok": True}
