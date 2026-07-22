"""The ``git`` verb pack: the host-side git + filesystem effects.

This re-hosts, natively on the host, the call layer PR #18 deleted from the
container (``git/repo.py``, ``git/runner.py``, the source engine's
clone/init/fetch decision tree, ``engines/layout/execute.py``'s move, and
``on_disk/scanner.py``'s walk). The behavioral reference is that deleted code
and ``mono-control/tests/broker_shim.py`` — the in-process fake the container's
whole suite passes against. These verbs perform the *same* operations with the
*same* request/response shapes, differing only in that they run against the real
host paths and host git rather than a temp fixture.

Why native, not in the container: git and ``os.rename`` here run on the host's
own filesystem (NTFS), so the move never crosses the 9p/drvfs bind-mount seam —
which is exactly what made the original ``EACCES``-on-move bug possible.

Security surface. This module is where container-supplied input meets the host,
so every verb validates at the boundary before it touches disk or spawns git:

* a slug must be a bare name — never a path — so it cannot escape the config or
  offline roots (``../../etc``);
* a layout ``location`` is normalized *inside* the workspace root, and a
  symlinked parent that would redirect the move out of it is refused;
* a ``checkout`` commit must be hex — never a ref or an option — so it cannot
  smuggle a flag or a branch name into ``git checkout``;
* ``remote_default_branch``'s URL comes from the container (guided-add is still
  *defining* the remote), so its scheme is allow-listed to ``https`` and git is
  run under ``GIT_ALLOW_PROTOCOL=https`` — no ``file://`` / ``ext::`` side
  channels.

Credentials are the host's, not ours. Git here runs host-side *as the developer*,
so it inherits the host's own credential machinery (gh helper, Git Credential
Manager, OS keyring, ``~/.gitconfig``). The broker injects no token and no
credential helper. What it *does* enforce is a strictly non-interactive posture
(``GIT_TERMINAL_PROMPT=0`` plus ``credential.interactive=false``) so a network op
with no usable credential fails fast — on every platform — instead of hanging on a
TTY prompt or a GUI popup; the failure is then reworded into an actionable
"set up gh / a credential helper" summary (see ``is_auth_failure``).
"""

from __future__ import annotations

import errno
import json
import os
import platform
import re
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Iterator, Optional

from mono_control_shim.broker import (
    INVALID_PARAMS,
    SERVER_ERROR,
    HostContext,
    VerbError,
    verb,
)

# The stamp that makes a checkout self-identifying. Reading it (location -> slug)
# needs no external index; its absence marks a foreign / unmanaged tree.
_SLUG_KEY = "mono-control.slug"

# FS-capability config stamped at create time, in (git config key, attr) form —
# so every later git on the tree (ours or a developer's) behaves consistently.
_PROFILE_KEYS = (
    ("core.filemode", "filemode"),
    ("core.symlinks", "symlinks"),
    ("core.ignorecase", "ignorecase"),
)

# URL schemes the container is allowed to have the host probe. HTTPS only: a repo
# definition is data, and a mistyped or malicious URL must never make git open a
# local file, an ssh session, or a transport helper.
_ALLOWED_URL_SCHEMES = frozenset({"https"})

# stderr substrings (matched case-insensitively) that mark a git network failure as
# an auth / credential problem — as opposed to a missing repo, a DNS failure, or a
# refused connection. Adapted for the host-side reality: with the non-interactive
# posture below, a private remote with no usable credential fails with one of these
# rather than hanging on a prompt.
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

# The actionable hint appended to an auth failure. Reworded for host-side git: the
# fix is no longer "export a token for the container" but "give host git a credential".
_AUTH_HINT = (
    "git runs on the host now and found no usable GitHub credential. Set one up on "
    "the host — run `gh auth login` (easiest; makes gh git's credential helper), or "
    "configure a credential helper / fine-grained PAT for github.com."
)


def is_auth_failure(stderr: str) -> bool:
    """True when *stderr* (or a ``GitError`` message wrapping it) looks like an
    auth / credential failure rather than any other network error."""
    low = stderr.lower()
    return any(marker in low for marker in AUTH_MARKERS)


def _auth_summary(target: str) -> str:
    """The actionable summary for an auth failure against *target* (a slug or URL)."""
    return f"authentication failed for {target}: {_AUTH_HINT}"


# --------------------------------------------------------------------------- #
# FS-capability profile (relocated from host_platform)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FsProfile:
    """The host filesystem's git-relevant capabilities, stamped into new repos."""

    filemode: bool
    symlinks: bool
    ignorecase: bool


def host_profile() -> FsProfile:
    """Derive the FS profile from the host OS.

    The shim *is* the host, so it stamps the host's own capabilities rather than
    guessing: POSIX hosts track filemode and real symlinks; Windows does neither
    and is case-insensitive; macOS keeps symlinks but is case-insensitive too.
    """
    system = platform.system()
    if system == "Windows":
        return FsProfile(filemode=False, symlinks=False, ignorecase=True)
    if system == "Darwin":
        return FsProfile(filemode=True, symlinks=True, ignorecase=True)
    return FsProfile(filemode=True, symlinks=True, ignorecase=False)


# --------------------------------------------------------------------------- #
# The single git subprocess chokepoint (relocated from git/runner.py)
# --------------------------------------------------------------------------- #
class GitError(Exception):
    """A git operation failed: a missing binary or a non-zero exit."""


class UnmanagedCheckoutError(GitError):
    """A checkout carries no ``mono-control.slug`` stamp (foreign / unmanaged)."""


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
        # No secret to redact: the broker injects no token — host git supplies its
        # own credential — so the raw stderr is safe to surface (and is what the
        # auth classifier reads).
        raise GitError(f"`git {' '.join(args)}` failed: {result.stderr.strip()}")
    return result.stdout.strip()


# ``-c`` config that makes a git call strictly non-interactive on the credential
# axis: ``credential.interactive=false`` tells Git Credential Manager (and other
# helpers that honor it) to fail rather than pop a GUI prompt. Paired with the
# ``GIT_TERMINAL_PROMPT=0`` env below, a missing credential becomes a fast, clean
# error on every platform instead of a hang.
_NONINTERACTIVE_CONFIG = ("-c", "credential.interactive=false")


def _noninteractive_config() -> list[str]:
    """``-c`` flags enforcing the non-interactive credential posture."""
    return list(_NONINTERACTIVE_CONFIG)


def _noninteractive_env(*, https_only: bool = False) -> dict[str, str]:
    """The environment for a network git call: strictly non-interactive, no token.

    ``GIT_TERMINAL_PROMPT=0`` stops git itself from prompting on a TTY, turning a
    missing credential into a clean error. Host git supplies its own credential, so
    nothing is injected here. ``https_only`` adds ``GIT_ALLOW_PROTOCOL=https`` for
    the one verb whose URL the container supplies (a security allow-list, unrelated
    to credentials).
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if https_only:
        env["GIT_ALLOW_PROTOCOL"] = "https"
    return env


# --------------------------------------------------------------------------- #
# A handle on a working tree (relocated from git/repo.py)
# --------------------------------------------------------------------------- #
class GitRepo:
    """A handle on a git working tree at ``path``."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _git(self, *args: str) -> str:
        return run_git(list(args), cwd=self.path)

    def current_commit(self) -> Optional[str]:
        return self.resolve_ref("HEAD")

    def resolve_ref(self, ref: str) -> Optional[str]:
        try:
            return self._git("rev-parse", "--verify", f"{ref}^{{commit}}")
        except GitError:
            return None

    def config_get(self, key: str) -> str:
        return self._git("config", "--get", key)

    def slug(self) -> str:
        try:
            return self.config_get(_SLUG_KEY)
        except GitError as e:
            raise UnmanagedCheckoutError(
                f"{self.path} has no {_SLUG_KEY!r} stamp"
            ) from e

    def is_dirty(self) -> bool:
        return bool(self._git("status", "--porcelain"))

    def fetch(self, remote: str, refs: Optional[Iterable[str]] = None) -> None:
        args = ["fetch", remote]
        if refs is not None:
            args.extend(refs)
        run_git(
            args,
            cwd=self.path,
            env=_noninteractive_env(),
            config=_noninteractive_config(),
        )

    def checkout(self, ref: str) -> None:
        # ``--`` guards against a ref that begins with ``-`` being read as a flag;
        # the caller has already checked it is bare hex, this is defense in depth.
        self._git("checkout", ref, "--")

    def _apply_profile(self, profile: FsProfile) -> None:
        for key, attr in _PROFILE_KEYS:
            self._git("config", key, "true" if getattr(profile, attr) else "false")

    def _apply_slug(self, slug: str) -> None:
        self._git("config", _SLUG_KEY, slug)
        readback = self.config_get(_SLUG_KEY)
        if readback != slug:
            raise UnmanagedCheckoutError(
                f"slug stamp on {self.path} did not round-trip: "
                f"wrote {slug!r}, read back {readback!r}"
            )


def clone(
    url: str | Path,
    dest: Path | str,
    *,
    profile: FsProfile,
    slug: str,
) -> GitRepo:
    """Clone ``url`` into ``dest``, stamping ``profile`` + ``slug`` before checkout.

    ``--no-checkout`` so the stamp governs the working-tree population (notably
    symlink / filemode handling). Network call: runs under the non-interactive
    posture, letting host git resolve credentials or fail fast.
    """
    dest = Path(dest)
    run_git(
        ["clone", "--no-checkout", str(url), str(dest)],
        env=_noninteractive_env(),
        config=_noninteractive_config(),
    )
    repo = GitRepo(dest)
    repo._apply_profile(profile)
    repo._apply_slug(slug)
    repo._git("checkout", "HEAD", "--", ".")
    return repo


def init(
    path: Path | str,
    *,
    profile: FsProfile,
    slug: str,
    initial_branch: Optional[str] = None,
) -> GitRepo:
    """Initialize a new empty repo at ``path`` and stamp ``profile`` + ``slug``."""
    path = Path(path)
    args = ["init"]
    if initial_branch is not None:
        args += ["--initial-branch", initial_branch]
    args.append(str(path))
    run_git(args)
    repo = GitRepo(path)
    repo._apply_profile(profile)
    repo._apply_slug(slug)
    return repo


def _parse_symref_head(out: str) -> Optional[str]:
    """Parse ``ls-remote --symref HEAD`` output into a branch name, or ``None``."""
    for line in out.splitlines():
        if line.startswith("ref:"):
            target = line[len("ref:"):].strip().split()[0]
            if target.startswith("refs/heads/"):
                return target[len("refs/heads/"):]
    return None


def ls_remote_symref(
    url: str,
    *,
    env: Optional[dict[str, str]] = None,
    config: Optional[list[str]] = None,
) -> Optional[str]:
    """Read a remote's default branch via ``ls-remote --symref HEAD``, or ``None``.

    Probes without cloning. Kept separate from the verb so the parse can be
    exercised against a local bare repo in tests without going near the
    (rejected-by-the-verb) ``file://`` scheme.
    """
    return _parse_symref_head(
        run_git(["ls-remote", "--symref", str(url), "HEAD"], env=env, config=config)
    )


# --------------------------------------------------------------------------- #
# Scanner walk + atomic move (relocated from scanner.py / execute.py)
# --------------------------------------------------------------------------- #
def _find_checkouts(root: Path) -> Iterator[Path]:
    """Yield each subtree under ``root`` containing a ``.git`` (descent stops there)."""
    if not root.is_dir():
        return
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        if (current / ".git").exists():
            yield current
            continue
        try:
            stack.extend(p for p in current.iterdir() if p.is_dir())
        except PermissionError:  # pragma: no cover
            continue


def _move(src: Path, dst: Path) -> None:
    """Move ``src`` -> ``dst``, publishing atomically; raise ``OSError`` families.

    Fast path is ``os.rename`` (single syscall — no partially-moved tree ever
    appears). ``FileExistsError`` when the destination is occupied. Across
    filesystems (``EXDEV`` — separate mounts) fall back to a copy that still
    publishes atomically: copy to a hidden temp dir on the destination filesystem,
    then rename it into place and drop the source.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise FileExistsError(dst)
    try:
        os.rename(src, dst)
        return
    except OSError as e:
        if e.errno not in (errno.EXDEV,):
            raise
    tmp = dst.parent / f".{dst.name}.tmp-move"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp, symlinks=True)
    os.rename(tmp, dst)
    shutil.rmtree(src)


# --------------------------------------------------------------------------- #
# Input validation (the security boundary)
# --------------------------------------------------------------------------- #
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_HEX_RE = re.compile(r"[0-9a-fA-F]{4,64}")


def _valid_slug(slug: Any) -> str:
    """Return ``slug`` if it is a bare, safe name; else reject with INVALID_PARAMS.

    A slug indexes files (``<config>/repos/<slug>.json``) and directories
    (``<offline>/<slug>``), so it must never be a path: no separators, no ``..``,
    no leading dot. That keeps a hostile slug from escaping a root.
    """
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise VerbError(INVALID_PARAMS, f"invalid slug: {slug!r}")
    return slug


def _valid_hex_commit(commit: Any) -> str:
    """Return ``commit`` if it is a bare hex object id; else reject.

    Hex only — never a ref name, ``HEAD``, or a ``-flag`` — so it cannot smuggle
    an option or a branch into ``git checkout``.
    """
    if not isinstance(commit, str) or not _HEX_RE.fullmatch(commit):
        raise VerbError(INVALID_PARAMS, f"commit must be a hex object id: {commit!r}")
    return commit


def _resolve_inside(root: Path, location: Any) -> Path:
    """Resolve a container-supplied ``location`` to an absolute path inside ``root``.

    Rejects absolutes and any ``..`` component, then guards against a symlinked
    parent that would redirect the move out of the workspace: the nearest existing
    ancestor of the destination must still resolve within ``root``.
    """
    if not isinstance(location, str) or not location:
        raise VerbError(INVALID_PARAMS, f"invalid location: {location!r}")
    pure = PurePosixPath(location)
    if (
        pure.is_absolute()
        or PureWindowsPath(location).is_absolute()
        or any(part == ".." for part in pure.parts)
    ):
        raise VerbError(INVALID_PARAMS, f"location escapes the workspace: {location!r}")
    dst = root.joinpath(*pure.parts)
    root_real = root.resolve()
    ancestor = dst
    while not ancestor.exists():
        ancestor = ancestor.parent
    ancestor_real = ancestor.resolve()
    if ancestor_real != root_real and root_real not in ancestor_real.parents:
        raise VerbError(INVALID_PARAMS, f"location escapes the workspace: {location!r}")
    return dst


def _sanitize_remote_url(url: Any) -> str:
    """Return ``url`` if its scheme is allow-listed (https); else reject.

    Also refuses the ``transport::address`` helper syntax (``ext::``, ``fd::`` …),
    which ``urlsplit`` would otherwise parse as scheme ``ext`` with a payload.
    """
    if not isinstance(url, str) or not url:
        raise VerbError(INVALID_PARAMS, f"invalid url: {url!r}")
    if "::" in url:
        raise VerbError(INVALID_PARAMS, "url uses a transport helper (refused)")
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise VerbError(
            INVALID_PARAMS,
            f"url scheme {scheme or '(none)'!r} not allowed (https only)",
        )
    return url


def _require_ctx(ctx: Optional[HostContext]) -> HostContext:
    """Every git verb needs a host context; a broker started without one is a bug."""
    if ctx is None:
        raise VerbError(SERVER_ERROR, "broker has no host context")
    return ctx


# --------------------------------------------------------------------------- #
# Observation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Observed:
    slug: str
    location: Path  # absolute
    state: str  # "materialized" | "offline"
    commit: Optional[str]
    dirty: bool


def _observe(checkout: Path, state: str) -> Optional[_Observed]:
    repo = GitRepo(checkout)
    try:
        slug = repo.slug()
    except UnmanagedCheckoutError:
        return None
    return _Observed(
        slug=slug,
        location=checkout,
        state=state,
        commit=repo.current_commit(),
        dirty=repo.is_dirty(),
    )


def _inventory(ctx: HostContext) -> tuple[dict[str, _Observed], list[tuple[Path, str]]]:
    """Walk both roots. First observation of a slug wins (workspace before offline)."""
    repos: dict[str, _Observed] = {}
    unmanaged: list[tuple[Path, str]] = []
    for root, state in (
        (ctx.workspace_root, "materialized"),
        (ctx.offline_root, "offline"),
    ):
        for checkout in _find_checkouts(root):
            observed = _observe(checkout, state)
            if observed is None:
                unmanaged.append((checkout, state))
            elif observed.slug not in repos:
                repos[observed.slug] = observed
    return repos, unmanaged


def _location_of(ctx: HostContext, slug: str) -> Optional[_Observed]:
    return _inventory(ctx)[0].get(slug)


def _relative(location: Path, root: Path) -> str:
    return location.relative_to(root).as_posix()


# --------------------------------------------------------------------------- #
# Verb: scan
# --------------------------------------------------------------------------- #
@verb("scan")
def _scan(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Walk workspace + offline roots into a ``WireInventory`` (relative + state)."""
    ctx = _require_ctx(ctx)
    repos, unmanaged = _inventory(ctx)

    def root_for(state: str) -> Path:
        return ctx.workspace_root if state == "materialized" else ctx.offline_root

    return {
        "repos": [
            {
                "slug": obs.slug,
                "location": _relative(obs.location, root_for(obs.state)),
                "state": obs.state,
                "commit": obs.commit,
                "dirty": obs.dirty,
            }
            for obs in repos.values()
        ],
        "unmanaged": [
            {"location": _relative(path, root_for(state)), "state": state}
            for path, state in unmanaged
        ],
    }


# --------------------------------------------------------------------------- #
# Verb: acquire (the source engine's effecting half — owns clone/init/fetch)
# --------------------------------------------------------------------------- #
def _src(
    status: str,
    summary: str,
    *,
    unresolved: Optional[list[str]] = None,
    resolved: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "summary": summary,
        "unresolved_refs": unresolved or [],
        "resolved": resolved or {},
    }


def _repo_def_path(ctx: HostContext, slug: str) -> Path:
    return ctx.config_dir / "repos" / f"{slug}.json"


def _source_url(ctx: HostContext, slug: str) -> Optional[str]:
    """The first declared source URL from the host repo def (never the container's)."""
    data = json.loads(_repo_def_path(ctx, slug).read_text())
    sources = data.get("sources") or {}
    return next(iter(sources.values())) if sources else None


def _resolve_ref(repo: GitRepo, ref: str) -> Optional[str]:
    """Resolve ``ref`` locally, falling back from ``refs/heads/x`` to origin's copy."""
    commit = repo.resolve_ref(ref)
    if commit is None and ref.startswith("refs/heads/"):
        commit = repo.resolve_ref("refs/remotes/origin/" + ref[len("refs/heads/"):])
    return commit


def _verify_refs(
    repo: GitRepo, refs: list[str], ok_status: str, ok_summary: str, slug: str
) -> dict[str, Any]:
    resolved: dict[str, str] = {}
    for ref in refs:
        commit = _resolve_ref(repo, ref)
        if commit is not None:
            resolved[ref] = commit
    unresolved = [r for r in refs if r not in resolved]
    if unresolved:
        return _src(
            "ref-missing",
            f"{slug!r}: {len(unresolved)} ref(s) did not resolve",
            unresolved=unresolved,
            resolved=resolved,
        )
    return _src(ok_status, ok_summary, resolved=resolved)


@verb("acquire")
def _acquire(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Make ``refs`` locally resolvable for ``slug``: clone / init / fetch + verify.

    Owns the clone-vs-init-vs-fetch decision. The source URL is resolved from the
    host repo def only — a URL is never accepted from the container here.
    """
    ctx = _require_ctx(ctx)
    slug = _valid_slug(params.get("slug"))
    refs = list(params.get("refs") or [])
    initial_branch = params.get("initial_branch")
    profile = host_profile()

    if not _repo_def_path(ctx, slug).is_file():
        return _src("definition-missing", f"repo def for {slug!r} not found")

    source_url = _source_url(ctx, slug)
    observed = _location_of(ctx, slug)

    if observed is None:
        # Absent locally -> create.
        if source_url is None:
            if refs:
                return _src(
                    "source-missing",
                    f"{slug!r} is absent and declares no sources",
                    unresolved=refs,
                )
            try:
                init(
                    ctx.offline_root / slug,
                    profile=profile,
                    slug=slug,
                    initial_branch=initial_branch,
                )
            except GitError as e:
                return _src("create-failed", f"init {slug!r} failed: {e}")
            return _src("initialized", f"initialized {slug!r}")
        try:
            repo = clone(source_url, ctx.offline_root / slug, profile=profile, slug=slug)
        except GitError as e:
            if is_auth_failure(str(e)):
                return _src("create-failed", _auth_summary(slug))
            return _src("create-failed", f"clone {slug!r} failed: {e}")
        return _verify_refs(repo, refs, "cloned", f"cloned {slug!r}", slug)

    # Present locally (offline or materialized) -> fetch (if there is a source).
    repo = GitRepo(observed.location)
    if source_url is not None:
        try:
            repo.fetch("origin")
        except GitError:
            try:
                repo.fetch(source_url)
            except GitError as e:
                if is_auth_failure(str(e)):
                    return _src("fetch-failed", _auth_summary(slug))
                return _src("fetch-failed", f"fetch {slug!r} failed: {e}")
    if source_url is None and not refs:
        return _src("ok", f"{slug!r} present, no source to fetch")
    return _verify_refs(repo, refs, "fetched", f"fetched {slug!r}", slug)


# --------------------------------------------------------------------------- #
# Verbs: layout effects (place / relocate / retire / checkout)
# --------------------------------------------------------------------------- #
def _lay(status: str, summary: str) -> dict[str, Any]:
    return {"status": status, "summary": summary}


def _race(verb_name: str, slug: str, detail: str) -> dict[str, Any]:
    return {"status": "race-aborted", "summary": f"{verb_name} aborted for {slug!r}: {detail}"}


def _move_into_workspace(
    ctx: HostContext, params: dict[str, Any], ok_status: str, verb_name: str
) -> dict[str, Any]:
    slug = _valid_slug(params.get("slug"))
    dst = _resolve_inside(ctx.workspace_root, params.get("location"))
    observed = _location_of(ctx, slug)
    if observed is None:
        return _race(verb_name, slug, "checkout vanished")
    location = _relative(dst, ctx.workspace_root)
    try:
        _move(observed.location, dst)
    except FileExistsError:
        return _race(verb_name, slug, f"destination {location} is occupied")
    except OSError as e:
        return _lay("failed", f"{verb_name} {slug!r} failed: {e}")
    return _lay(ok_status, f"{verb_name}d {slug!r} at {location}")


@verb("place")
def _place(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Move ``slug`` (offline) into ``location`` under the workspace root."""
    return _move_into_workspace(_require_ctx(ctx), params, "placed", "place")


@verb("relocate")
def _relocate(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Move ``slug`` between two materialized locations."""
    return _move_into_workspace(_require_ctx(ctx), params, "relocated", "relocate")


@verb("retire")
def _retire(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Move ``slug`` from the workspace back to its offline holding spot.

    Non-destructive: the destination is derived from the slug (``location`` is
    ignored), and an already-occupied offline spot is a ``blocked`` precondition,
    not a race.
    """
    ctx = _require_ctx(ctx)
    slug = _valid_slug(params.get("slug"))
    observed = _location_of(ctx, slug)
    if observed is None:
        return _race("retire", slug, "checkout vanished")
    dst = ctx.offline_root / slug
    if dst.exists():
        return _lay("blocked", f"offline holding spot {slug} already occupied")
    try:
        _move(observed.location, dst)
    except FileExistsError:
        return _race("retire", slug, "offline spot occupied")
    except OSError as e:
        return _lay("failed", f"retire {slug!r} failed: {e}")
    return _lay("retired", f"retired {slug!r} to offline")


@verb("checkout")
def _checkout(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Check ``commit`` (hex) out at ``slug``'s current location."""
    ctx = _require_ctx(ctx)
    slug = _valid_slug(params.get("slug"))
    commit = _valid_hex_commit(params.get("commit"))
    observed = _location_of(ctx, slug)
    if observed is None:
        return _race("checkout", slug, "checkout vanished")
    repo = GitRepo(observed.location)
    if repo.is_dirty():
        return _lay("blocked", f"{slug!r} became dirty between plan and execute")
    try:
        repo.checkout(commit)
    except GitError as e:
        return _lay("failed", f"checkout {commit[:12]} failed for {slug!r}: {e}")
    return _lay("checked-out", f"checked out {commit[:12]} for {slug!r}")


# --------------------------------------------------------------------------- #
# Verbs: a cluster's layout document
# --------------------------------------------------------------------------- #
def _layout_path(ctx: HostContext, cluster_slug: str) -> Optional[Path]:
    observed = _location_of(ctx, cluster_slug)
    if observed is None:
        return None
    return observed.location / "product-cluster" / "default-layout.json"


@verb("read_layout")
def _read_layout(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Read ``<cluster_slug>``'s ``product-cluster/default-layout.json`` contents."""
    ctx = _require_ctx(ctx)
    cluster_slug = _valid_slug(params.get("cluster_slug"))
    path = _layout_path(ctx, cluster_slug)
    if path is None or not path.is_file():
        return {"exists": False, "layout": None}
    return {"exists": True, "layout": json.loads(path.read_text())}


@verb("write_layout")
def _write_layout(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Author ``<cluster_slug>``'s layout document (inside its managed checkout)."""
    ctx = _require_ctx(ctx)
    cluster_slug = _valid_slug(params.get("cluster_slug"))
    layout = params.get("layout")
    path = _layout_path(ctx, cluster_slug)
    if path is None:
        raise VerbError(SERVER_ERROR, f"{cluster_slug!r} is not on disk")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(layout, indent=2) + "\n")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Verb: remote_default_branch (URL comes from the container — sanitize it)
# --------------------------------------------------------------------------- #
@verb("remote_default_branch")
def _remote_default_branch(
    params: dict[str, Any], ctx: Optional[HostContext]
) -> dict[str, Any]:
    """Probe a remote's default branch (symbolic HEAD), or ``None``.

    The URL is container-supplied, so it is scheme-checked (https only) and git is
    run under ``GIT_ALLOW_PROTOCOL=https`` — no file/ssh/helper side channels. Host
    git supplies any credential; on an auth failure the probe returns the actionable
    host-setup hint.
    """
    ctx = _require_ctx(ctx)
    url = _sanitize_remote_url(params.get("url"))
    try:
        branch = _ls_remote_symref_hardened(url)
    except GitError as e:
        if is_auth_failure(str(e)):
            raise VerbError(SERVER_ERROR, _auth_summary(url))
        raise VerbError(SERVER_ERROR, f"remote probe failed: {e}")
    return {"branch": branch}


def _ls_remote_symref_hardened(url: str) -> Optional[str]:
    """``ls-remote --symref`` under the hardened, non-interactive, https-only posture."""
    return ls_remote_symref(
        url,
        env=_noninteractive_env(https_only=True),
        config=_noninteractive_config(),
    )
