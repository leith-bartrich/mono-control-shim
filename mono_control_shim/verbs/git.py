"""The ``git`` verb pack: the host-side git + filesystem effects.

Physical model: **bare repos + git worktrees**. Every managed repository is a
*bare* repo under ``bare_root`` (``mono-repos-bare``) that is created once and
**never moves**; placing it into the workspace is an additive ``git worktree add``
under ``work_root`` (``mono-work``), and retiring it is ``git worktree remove``.
This replaces the earlier "clone into an offline dir, then ``os.rename`` it into
the workspace" model, whose move failed with ``WinError 5`` / ``EACCES`` when an
IDE (or drvfs) held the directory being renamed. A worktree add moves nothing that
anything holds, so the class of failure is gone by construction.

This re-hosts, natively on the host, the call layer PR #18 deleted from the
container. The behavioral reference is that deleted code and
``mono-control/tests/broker_shim.py`` — the in-process fake the container's whole
suite passes against. These verbs perform the *same* operations with the *same*
request/response shapes (the ``state`` literal stays ``"offline"`` / ``"materialized"``,
reinterpreted: **offline = a bare repo with no worktree**, **materialized = a bare
repo with a worktree under ``work_root``**), differing only in that they run against
the real host paths and host git rather than a temp fixture.

Security surface. This module is where container-supplied input meets the host,
so every verb validates at the boundary before it touches disk or spawns git:

* a slug must be a bare name — never a path — so it cannot escape the config or
  bare roots (``../../etc``);
* a layout ``location`` is normalized *inside* the workspace root, and a
  symlinked parent that would redirect the worktree out of it is refused;
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

import json
import os
import platform
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Optional

from mono_control_shim.broker import (
    INVALID_PARAMS,
    SERVER_ERROR,
    HostContext,
    VerbError,
    verb,
)

# The stamp that makes a bare repo self-identifying. Reading it (dir -> slug) needs
# no external index; its absence marks a foreign / unmanaged repo. It is stamped into
# the bare repo's config, so every worktree added off that bare inherits it.
_SLUG_KEY = "mono-control.slug"

# The cluster layout document, relative to a repo's working tree (a worktree, or the
# committed tree read out of the bare repo's HEAD).
_LAYOUT_REL = "product-cluster/default-layout.json"

# FS-capability config stamped at create time, in (git config key, attr) form —
# so every later git on the repo (ours or a developer's) behaves consistently.
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
    """A repo carries no ``mono-control.slug`` stamp (foreign / unmanaged)."""


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
# A handle on a git directory — a bare repo or a worktree (relocated from git/repo.py)
# --------------------------------------------------------------------------- #
class GitRepo:
    """A handle on a git directory at ``path`` (a bare repo, or one of its worktrees).

    Every method runs ``git -C <path> ...``, so the same handle works for a bare
    repo (config / rev-parse / worktree management / ``show``) and for a worktree
    (status / checkout). The worktree-management methods are only meaningful on the
    bare repo, whose config governs its whole worktree family.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _git(self, *args: str) -> str:
        return run_git(list(args), cwd=self.path)

    def is_bare_repository(self) -> bool:
        """True if ``path`` is a bare repository; False if it is a worktree or not a
        git dir at all (``rev-parse`` failing is treated as 'not a managed bare')."""
        try:
            return self._git("rev-parse", "--is-bare-repository") == "true"
        except GitError:
            return False

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

    def set_remote(self, name: str, url: str) -> None:
        """Point remote ``name`` at ``url``: add it, or repoint it if it exists.

        A purely local config edit (no network, no credential env). Membership is
        checked against ``git remote``'s listing — not inferred from a failing
        ``add`` — so the add / set-url choice is explicit. Caller has already
        validated ``name`` (bare remote name) and ``url`` (https only).
        """
        existing = self._git("remote").split()
        if name in existing:
            self._git("remote", "set-url", name, url)
        else:
            self._git("remote", "add", name, url)

    def show_head_blob(self, rel: str) -> str:
        """Return the contents of ``rel`` as committed at HEAD (``git show HEAD:rel``).

        Reads a file out of the bare repo without a worktree. Raises ``GitError`` if
        HEAD has no such path (or there is no HEAD yet).
        """
        return self._git("show", f"HEAD:{rel}")

    # -- worktree management (meaningful on the bare repo) ------------------- #
    def worktree_add(self, dest: Path, ref: str) -> None:
        """Materialize a worktree at ``dest`` checked out at ``ref`` (additive)."""
        self._git("worktree", "add", str(dest), ref)

    def worktree_move(self, src: Path, dst: Path) -> None:
        """Move the worktree at ``src`` to ``dst`` (native; preserves its state)."""
        self._git("worktree", "move", str(src), str(dst))

    def worktree_remove(self, path: Path) -> None:
        """Remove the worktree at ``path`` (the bare repo — and its commits — survive)."""
        self._git("worktree", "remove", str(path))

    def worktree_under(self, root: Path) -> Optional[Path]:
        """The path of this bare repo's worktree that lives under ``root``, or ``None``.

        Parses ``git worktree list --porcelain``: the bare repo lists itself with a
        ``bare`` marker (skipped); a real worktree lists its path and HEAD. The first
        worktree whose resolved path is at or under ``root`` is returned.
        """
        out = self._git("worktree", "list", "--porcelain")
        root_real = root.resolve()
        for record in out.split("\n\n"):
            lines = record.splitlines()
            if not lines or not lines[0].startswith("worktree "):
                continue
            if any(line == "bare" for line in lines):
                continue  # the bare repo's own entry, not a worktree
            wt = Path(lines[0][len("worktree "):])
            try:
                wt_real = wt.resolve()
            except OSError:  # pragma: no cover - defensive
                continue
            if wt_real == root_real or root_real in wt_real.parents:
                return wt
        return None

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
    """Clone ``url`` into a **bare** repo at ``dest``, stamping ``profile`` + ``slug``.

    ``--bare`` — there is no working tree to check out; a worktree is added later by
    ``place``. The stamp and FS-capability profile go into the bare repo's config, so
    every worktree added off it inherits them. Network call: runs under the
    non-interactive posture, letting host git resolve credentials or fail fast.
    """
    dest = Path(dest)
    run_git(
        ["clone", "--bare", str(url), str(dest)],
        env=_noninteractive_env(),
        config=_noninteractive_config(),
    )
    repo = GitRepo(dest)
    repo._apply_profile(profile)
    repo._apply_slug(slug)
    return repo


def init(
    path: Path | str,
    *,
    profile: FsProfile,
    slug: str,
    initial_branch: Optional[str] = None,
) -> GitRepo:
    """Initialize a new empty **bare** repo at ``path`` and stamp ``profile`` + ``slug``."""
    path = Path(path)
    args = ["init", "--bare"]
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
# Input validation (the security boundary)
# --------------------------------------------------------------------------- #
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_HEX_RE = re.compile(r"[0-9a-fA-F]{4,64}")


def _valid_slug(slug: Any) -> str:
    """Return ``slug`` if it is a bare, safe name; else reject with INVALID_PARAMS.

    A slug indexes files (``<config>/repos/<slug>.json``) and directories
    (``<bare_root>/<slug>``), so it must never be a path: no separators, no ``..``,
    no leading dot. That keeps a hostile slug from escaping a root.
    """
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise VerbError(INVALID_PARAMS, f"invalid slug: {slug!r}")
    return slug


def _valid_remote_name(name: Any) -> str:
    """Return ``name`` if it is a bare git remote name; else reject.

    Held to the same character class as a slug (``_SLUG_RE``): a plain name, no
    separators and no ``..``, so it cannot smuggle a path, an option, or a refspec
    into ``git remote add/set-url``. It is written verbatim into the config as a
    section key, so constraining it here is the security boundary for that write.
    """
    if not isinstance(name, str) or not _SLUG_RE.fullmatch(name):
        raise VerbError(INVALID_PARAMS, f"invalid remote name: {name!r}")
    return name


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
    parent that would redirect the worktree out of the workspace: the nearest
    existing ancestor of the destination must still resolve within ``root``.
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
# Observation (scan the bare root; a worktree under work_root => materialized)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Observed:
    slug: str
    bare: Path  # absolute path to the bare repo (always present)
    worktree: Optional[Path]  # absolute worktree path if materialized, else None
    state: str  # "materialized" | "offline"
    commit: Optional[str]
    dirty: bool


def _observe(ctx: HostContext, bare: Path) -> Optional[_Observed]:
    """Observe one bare repo. ``None`` if it is unstamped (foreign / unmanaged).

    Offline = the bare repo has no worktree under ``work_root``; its ``commit`` is the
    bare HEAD (``None`` for an empty init) and it is never dirty. Materialized = it
    has such a worktree; ``commit`` and ``dirty`` are read from that worktree.
    """
    repo = GitRepo(bare)
    try:
        slug = repo.slug()
    except UnmanagedCheckoutError:
        return None
    wt = repo.worktree_under(ctx.work_root)
    if wt is not None:
        tree = GitRepo(wt)
        return _Observed(slug, bare, wt, "materialized", tree.current_commit(), tree.is_dirty())
    return _Observed(slug, bare, None, "offline", repo.current_commit(), False)


def _inventory(ctx: HostContext) -> tuple[dict[str, _Observed], list[Path]]:
    """Iterate ``bare_root/*``: managed bares keyed by slug, plus unstamped bares.

    Only *bare repositories* are considered (``rev-parse --is-bare-repository``);
    anything else under the root is ignored. A bare with no slug stamp is reported
    unmanaged. First observation of a slug wins (defensive against a duplicate stamp).
    """
    repos: dict[str, _Observed] = {}
    unmanaged: list[Path] = []
    if not ctx.bare_root.is_dir():
        return repos, unmanaged
    for entry in sorted(ctx.bare_root.iterdir()):
        if not entry.is_dir() or not GitRepo(entry).is_bare_repository():
            continue
        observed = _observe(ctx, entry)
        if observed is None:
            unmanaged.append(entry)
        elif observed.slug not in repos:
            repos[observed.slug] = observed
    return repos, unmanaged


def _location_of(ctx: HostContext, slug: str) -> Optional[_Observed]:
    return _inventory(ctx)[0].get(slug)


def _relative(location: Path, root: Path) -> str:
    return location.relative_to(root).as_posix()


def _wire_location(ctx: HostContext, obs: _Observed) -> str:
    """The ``WireRepo`` location: worktree-relative when materialized, else the slug."""
    if obs.state == "materialized" and obs.worktree is not None:
        return _relative(obs.worktree, ctx.work_root)
    return _relative(obs.bare, ctx.bare_root)


# --------------------------------------------------------------------------- #
# Verb: scan
# --------------------------------------------------------------------------- #
@verb("scan")
def _scan(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Walk the bare root into a ``WireInventory`` (relative location + state)."""
    ctx = _require_ctx(ctx)
    repos, unmanaged = _inventory(ctx)
    return {
        "repos": [
            {
                "slug": obs.slug,
                "location": _wire_location(ctx, obs),
                "state": obs.state,
                "commit": obs.commit,
                "dirty": obs.dirty,
            }
            for obs in repos.values()
        ],
        # An unstamped bare has no worktree by definition, so it reports as offline,
        # located by its dir name relative to the bare root.
        "unmanaged": [
            {"location": _relative(path, ctx.bare_root), "state": "offline"}
            for path in unmanaged
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

    Owns the clone-vs-init-vs-fetch decision. The repo is a **bare** repo at
    ``bare_root/<slug>``; there is no worktree yet (``place`` adds one later). The
    source URL is resolved from the host repo def only — a URL is never accepted from
    the container here.
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
        # Absent locally -> create the bare repo.
        if source_url is None:
            if refs:
                return _src(
                    "source-missing",
                    f"{slug!r} is absent and declares no sources",
                    unresolved=refs,
                )
            try:
                init(
                    ctx.bare_root / slug,
                    profile=profile,
                    slug=slug,
                    initial_branch=initial_branch,
                )
            except GitError as e:
                return _src("create-failed", f"init {slug!r} failed: {e}")
            return _src("initialized", f"initialized {slug!r}")
        try:
            repo = clone(source_url, ctx.bare_root / slug, profile=profile, slug=slug)
        except GitError as e:
            if is_auth_failure(str(e)):
                return _src("create-failed", _auth_summary(slug))
            return _src("create-failed", f"clone {slug!r} failed: {e}")
        return _verify_refs(repo, refs, "cloned", f"cloned {slug!r}", slug)

    # Present locally (offline or materialized) -> fetch on the bare repo.
    repo = GitRepo(observed.bare)
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


@verb("place")
def _place(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Materialize ``slug`` as a worktree at ``location`` under the work root.

    ``git worktree add`` off the bare repo — additive, so it moves nothing an IDE
    holds. The worktree is created at the bare repo's default ``HEAD``; the exact
    commit is set by the composite ``checkout`` the container issues right after.
    """
    ctx = _require_ctx(ctx)
    slug = _valid_slug(params.get("slug"))
    dst = _resolve_inside(ctx.work_root, params.get("location"))
    observed = _location_of(ctx, slug)
    if observed is None:
        return _race("place", slug, "repository vanished")
    location = _relative(dst, ctx.work_root)
    if dst.exists():
        return _race("place", slug, f"destination {location} is occupied")
    try:
        GitRepo(observed.bare).worktree_add(dst, "HEAD")
    except GitError as e:
        return _lay("failed", f"place {slug!r} failed: {e}")
    return _lay("placed", f"placed {slug!r} at {location}")


@verb("relocate")
def _relocate(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Move ``slug``'s worktree to a new ``location`` (native ``git worktree move``)."""
    ctx = _require_ctx(ctx)
    slug = _valid_slug(params.get("slug"))
    dst = _resolve_inside(ctx.work_root, params.get("location"))
    observed = _location_of(ctx, slug)
    if observed is None or observed.worktree is None:
        return _race("relocate", slug, "worktree vanished")
    location = _relative(dst, ctx.work_root)
    if dst.exists():
        return _race("relocate", slug, f"destination {location} is occupied")
    # ``git worktree move`` (unlike ``worktree add``) does not create intermediate
    # parent dirs, so make the destination's parent before handing it the leaf.
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        GitRepo(observed.bare).worktree_move(observed.worktree, dst)
    except GitError as e:
        return _lay("failed", f"relocate {slug!r} failed: {e}")
    return _lay("relocated", f"relocated {slug!r} at {location}")


@verb("retire")
def _retire(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Remove ``slug``'s worktree; the bare repo (and its commits) survive.

    ``location`` is ignored — the worktree is derived from the slug. Dirty-gated:
    committed work is safe in the bare repo, but a worktree with *uncommitted*
    changes is refused (``blocked``) rather than silently discarded.
    """
    ctx = _require_ctx(ctx)
    slug = _valid_slug(params.get("slug"))
    observed = _location_of(ctx, slug)
    if observed is None or observed.worktree is None:
        return _race("retire", slug, "worktree vanished")
    if observed.dirty:
        return _lay("blocked", f"{slug!r} has uncommitted changes; refusing to discard its worktree")
    try:
        GitRepo(observed.bare).worktree_remove(observed.worktree)
    except GitError as e:
        return _lay("failed", f"retire {slug!r} failed: {e}")
    return _lay("retired", f"retired {slug!r} to offline")


@verb("checkout")
def _checkout(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Check ``commit`` (hex) out at ``slug``'s materialized worktree."""
    ctx = _require_ctx(ctx)
    slug = _valid_slug(params.get("slug"))
    commit = _valid_hex_commit(params.get("commit"))
    observed = _location_of(ctx, slug)
    if observed is None or observed.worktree is None:
        return _race("checkout", slug, "worktree vanished")
    repo = GitRepo(observed.worktree)
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
@verb("read_layout")
def _read_layout(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Read ``<cluster_slug>``'s ``product-cluster/default-layout.json`` contents.

    Bare-aware: when the cluster is materialized, read the file from its worktree;
    otherwise read the blob committed at the bare repo's ``HEAD``. A missing file (or
    an absent slug / HEAD) is ``{"exists": False, "layout": None}``.
    """
    ctx = _require_ctx(ctx)
    cluster_slug = _valid_slug(params.get("cluster_slug"))
    observed = _location_of(ctx, cluster_slug)
    if observed is None:
        return {"exists": False, "layout": None}
    if observed.worktree is not None:
        path = observed.worktree / "product-cluster" / "default-layout.json"
        if not path.is_file():
            return {"exists": False, "layout": None}
        return {"exists": True, "layout": json.loads(path.read_text())}
    try:
        blob = GitRepo(observed.bare).show_head_blob(_LAYOUT_REL)
    except GitError:
        return {"exists": False, "layout": None}
    return {"exists": True, "layout": json.loads(blob)}


@verb("write_layout")
def _write_layout(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Author ``<cluster_slug>``'s layout document (requires a materialized worktree)."""
    ctx = _require_ctx(ctx)
    cluster_slug = _valid_slug(params.get("cluster_slug"))
    layout = params.get("layout")
    observed = _location_of(ctx, cluster_slug)
    if observed is None or observed.worktree is None:
        raise VerbError(SERVER_ERROR, f"{cluster_slug!r} is not materialized")
    path = observed.worktree / "product-cluster" / "default-layout.json"
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


# --------------------------------------------------------------------------- #
# Verb: set_remote (add / repoint a remote on a managed bare repo)
# --------------------------------------------------------------------------- #
@verb("set_remote")
def _set_remote(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Add or repoint remote ``name`` -> ``url`` on ``slug``'s bare repo config.

    The fork-adoption flow's effecting half. A LOCAL config edit only — no network,
    no credential env — but the ``url`` is written into the bare repo's config and
    *later* fetched, so it is constrained exactly like ``remote_default_branch``'s:
    https only, no ``::`` transport helper. ``name`` is held to a bare remote name so
    it cannot smuggle a path or flag into the config write.

    Wire (plain JSON, no request model — match the other verbs):
        ``SetRemoteRequest`` = ``{"slug": str, "name": str, "url": str}``
        result = ``OkResult`` = ``{"ok": True}``
    """
    ctx = _require_ctx(ctx)
    slug = _valid_slug(params.get("slug"))
    name = _valid_remote_name(params.get("name"))
    url = _sanitize_remote_url(params.get("url"))
    observed = _location_of(ctx, slug)
    if observed is None:
        # The container only calls this after observing the repo on disk; re-verify
        # rather than trust that, so a vanished / never-created slug is a clean
        # reported failure, not a git error against a guessed path.
        raise VerbError(SERVER_ERROR, f"{slug!r} is not on disk")
    GitRepo(observed.bare).set_remote(name, url)
    return {"ok": True}
