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
  smuggle a flag or a branch name into ``git checkout``. Its sibling
  ``checkout_branch`` takes a branch instead, and is safe by a different route:
  the value must be a line the repo *expresses* — declared in its def's
  ``branches`` map, or the bare repo's own default ``HEAD`` — all read host-side,
  so the container names a line rather than a ref. That is
  narrower than the hex form, where any object the container can name is fair
  game — the hex rule was written when the container ran git itself, and it never
  prevented following a repointed branch anyway (``acquire`` resolves
  ``refs/heads/<branch>``, so the branch was already followed; detaching only hid
  it);
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

That posture, the subprocess seam it rides on, and the auth classifier now live in
``mono_control_shim.git_run`` — shared with the CLI, which runs git *before* any
container exists (``mproj init`` populating ``mono-config/``) and so cannot reach
this layer. They are re-exported here under their original names: this module
remains the only place that pairs them with the managed model (slugs, bare roots,
worktrees), which is what the container talks to.
"""

from __future__ import annotations

import json
import os
import platform
import re
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
from mono_control_shim.git_run import (  # the shared chokepoint (see module docstring)
    AUTH_MARKERS,
    FALLBACK_IDENTITY as _FALLBACK_IDENTITY,
    AUTH_HINT as _AUTH_HINT,
    GitError,
    auth_summary as _auth_summary,
    identity_config,
    is_auth_failure,
    noninteractive_config as _noninteractive_config,
    noninteractive_env as _noninteractive_env,
    run_git,
)

# The stamp that makes a bare repo self-identifying. Reading it (dir -> slug) needs
# no external index; its absence marks a foreign / unmanaged repo. It is stamped into
# the bare repo's config, so every worktree added off that bare inherits it.
_SLUG_KEY = "mono-control.slug"

# The cluster layout document, relative to a repo's working tree (a worktree, or the
# committed tree read out of the bare repo's HEAD).
_LAYOUT_REL = "product-cluster/default-layout.json"

# The governed source names, mirroring mono-control's ``config/source_names.py`` (the
# container owns the vocabulary; the host needs it to map sources onto git remotes).
# ``origin`` and ``upstream`` are mutually exclusive canonicals; ``fork-ours`` is our
# writable copy of an upstream.
ORIGIN = "origin"
UPSTREAM = "upstream"
FORK_OURS = "fork-ours"

# What every conformed remote fetches. Remote-tracking, deliberately **not** mirror
# (``+refs/heads/*:refs/heads/*``): a mirror refspec force-overwrites local branches —
# destroying work the moment anyone commits on one — and collapses under multiple
# remotes, since `upstream` and `fork-ours` would both map onto ``refs/heads/*`` and the
# last fetch would win. ``clone --bare`` sets no refspec at all, which is why a fetch
# currently updates nothing.
_FETCH_REFSPEC = "+refs/heads/*:refs/remotes/{name}/*"

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
# Managed-model errors (the seam itself lives in git_run)
# --------------------------------------------------------------------------- #
class UnmanagedCheckoutError(GitError):
    """A repo carries no ``mono-control.slug`` stamp (foreign / unmanaged)."""


def _identity_config(repo: "GitRepo") -> list[str]:
    """``-c user.*`` pairs for identity fields the host hasn't set.

    Reads through the repo handle (rather than ``git_run.config_reader``) so the
    lookup goes via ``GitRepo.config_get`` — the managed model's own accessor.
    """
    return identity_config(repo.config_get)


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

    def set_upstream(self, branch: str, remote: str = ORIGIN) -> bool:
        """Point ``branch`` at ``remote``'s copy of it. False if there is none.

        Config writes, not a network call: ``branch.<n>.remote`` + ``.merge`` are
        what ``git pull`` reads. Skipped rather than forced when the remote-tracking
        ref is absent — a line that exists only locally has nothing to track, and
        inventing an upstream would make ``pull`` fail later instead of now.
        """
        if self.resolve_ref(f"refs/remotes/{remote}/{branch}") is None:
            return False
        self._git("config", f"branch.{branch}.remote", remote)
        self._git("config", f"branch.{branch}.merge", f"refs/heads/{branch}")
        return True

    def local_branches(self) -> list[str]:
        """Every local branch name (``refs/heads/*``)."""
        out = self._git("for-each-ref", "--format=%(refname:short)", "refs/heads")
        return [line for line in out.splitlines() if line]

    def attached_branch(self) -> Optional[str]:
        """The branch HEAD is attached to, or ``None`` when detached.

        Distinct from ``head_branch``: that one answers "which branch does HEAD
        name" and is meaningful even while the branch is *unborn*, which is what
        a fresh bare repo needs. This one answers "is this worktree actually on a
        branch", where a detached HEAD must read as ``None`` rather than as the
        branch it happens to sit on.
        """
        try:
            return self._git("symbolic-ref", "--quiet", "--short", "HEAD") or None
        except GitError:
            return None  # detached: `symbolic-ref --quiet` exits non-zero

    def head_branch(self) -> str:
        """The branch HEAD names — meaningful even while that branch is *unborn*."""
        return self._git("symbolic-ref", "--short", "HEAD")

    def write_root_commit(self, message: str = "initial commit") -> str:
        """Give an empty repo an empty root commit, so HEAD resolves to something.

        ``git init --bare`` leaves HEAD pointing at an **unborn** branch. That is a
        valid repo, but ``git worktree add <path> HEAD`` cannot resolve it
        (``fatal: invalid reference: HEAD``), so a brand-new repo could be created and
        then never placed — ``repo init`` followed by ``mat moveto`` failed at the last
        step. Writing a root commit here makes the repo placeable from the moment it
        exists, and costs one commit carrying no content.

        Done with plumbing (``hash-object`` / ``commit-tree`` / ``update-ref``) rather
        than ``git worktree add --orphan`` because that flag needs git >= 2.42, and the
        in-container fake this file is mirrored by runs against an older git.
        """
        branch = self.head_branch()
        tree = self._git("hash-object", "-w", "-t", "tree", os.devnull)
        commit = run_git(
            ["commit-tree", tree, "-m", message],
            cwd=self.path,
            config=_identity_config(self),
        )
        self._git("update-ref", f"refs/heads/{branch}", commit)
        return commit

    def slug(self) -> str:
        try:
            return self.config_get(_SLUG_KEY)
        except GitError as e:
            raise UnmanagedCheckoutError(
                f"{self.path} has no {_SLUG_KEY!r} stamp"
            ) from e

    def is_dirty(self) -> bool:
        return bool(self._git("status", "--porcelain"))

    def unreachable_commit_count(self) -> int:
        """Commits reachable from *this worktree's* HEAD but from no branch, tag or remote.

        Non-zero means removing the worktree would **orphan** committed work. A detached
        HEAD's commits are anchored only by that worktree's own HEAD, so
        ``git worktree remove`` leaves them dangling — and the dirty gate cannot see it,
        because committing is exactly what makes the tree clean.

        ``--all`` is unusable here: it counts HEAD itself, so the answer would always be
        zero. Naming the ref namespaces explicitly is what makes the question meaningful,
        and it means putting the work on a branch *or* tagging it both clear the block.
        """
        try:
            out = self._git(
                "rev-list", "--count", "HEAD", "--not", "--branches", "--tags", "--remotes"
            )
        except GitError:
            return 0  # unborn HEAD: nothing committed here, nothing to orphan
        return int(out or 0)

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

    def remotes(self) -> list[str]:
        """The configured remote names."""
        return self._git("remote").split()

    def remote_url(self, name: str) -> Optional[str]:
        try:
            return self._git("remote", "get-url", name)
        except GitError:
            return None

    def remove_remote(self, name: str) -> None:
        """Drop a remote **and its remote-tracking refs**.

        Repointing is remove-then-add rather than ``set-url`` precisely for this: a
        ``set-url`` leaves ``refs/remotes/<name>/*`` describing the *old* remote, so the
        two would coexist under one name and both look current. No work is at risk —
        ``refs/heads/*`` is untouched and tracking refs are regenerable mirrored state.
        """
        self._git("remote", "remove", name)

    def set_fetch_refspec(self, name: str) -> None:
        """Give ``name`` the standard remote-tracking refspec (idempotent).

        ``git remote add`` sets this itself, but the ``origin`` created by
        ``clone --bare`` has a URL and no refspec — which is why fetching a bare repo
        updates nothing.
        """
        self._git("config", f"remote.{name}.fetch", _FETCH_REFSPEC.format(name=name))

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
    """Initialize a new **bare** repo at ``path`` and stamp ``profile`` + ``slug``.

    The repo is given an empty root commit (see
    :meth:`GitRepo.write_root_commit`) so it is placeable immediately: a repo whose
    HEAD is still unborn cannot have a worktree added off it.
    """
    path = Path(path)
    args = ["init", "--bare"]
    if initial_branch is not None:
        args += ["--initial-branch", initial_branch]
    args.append(str(path))
    run_git(args)
    repo = GitRepo(path)
    repo._apply_profile(profile)
    repo._apply_slug(slug)
    repo.write_root_commit()
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
    # The branch the worktree's HEAD is attached to; None when detached, and
    # always None for an offline bare (no worktree HEAD to speak of). The
    # container cannot tell "on the branch" from "detached at its tip" without
    # this, and those are different states it has to reconcile.
    branch: Optional[str] = None


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
        return _Observed(
            slug, bare, wt, "materialized",
            tree.current_commit(), tree.is_dirty(), tree.attached_branch(),
        )
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
                "branch": obs.branch,
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


def _sources(ctx: HostContext, slug: str) -> dict[str, str]:
    """The declared ``name -> url`` sources from the host repo def.

    Read off the host's own disk, never accepted from the container — which is why
    these URLs skip ``_sanitize_remote_url`` (that guards *container-supplied* URLs).
    """
    data = json.loads(_repo_def_path(ctx, slug).read_text())
    return dict(data.get("sources") or {})


def _branches(ctx: HostContext, slug: str) -> dict[str, str]:
    """The declared ``line -> branch`` map from the host repo def.

    Read off the host's own disk, never accepted from the container — the same
    rule ``_sources`` states for URLs. It is what makes ``checkout_branch`` safe
    without a ref sanitizer: the container names a line, and the host decides what
    that means.
    """
    data = json.loads(_repo_def_path(ctx, slug).read_text())
    return dict(data.get("branches") or {})


def _expressed_line(ctx: HostContext, slug: str, bare: Path, requested: str) -> Optional[str]:
    """Resolve *requested* to a branch this repo **expresses**, or ``None``.

    A repo expresses a line two ways, and both count:

    * its def **declares** one — ``branches`` maps a purpose to a branch, and
      either the purpose (``dev``) or the branch (``main``) names it;
    * git **carries** one — the bare repo's ``HEAD`` is its default branch, set
      from the remote at clone. ``repo.md`` says the default *is* the line unless
      a def overrides it, so a repo that declares nothing still expresses this.

    Without the second rule a repo with no ``branches`` map could never be put on
    a branch at all, which is most repos and would be a worse hole than the one
    this verb closes.
    """
    declared = _branches(ctx, slug)
    if requested in declared:
        return declared[requested]
    if requested in declared.values():
        return requested
    try:
        if requested == GitRepo(bare).head_branch():
            return requested
    except GitError:
        pass
    return None


def _expressed_lines(ctx: HostContext, slug: str, bare: Path) -> str:
    """Render what a repo expresses, for a refusal message."""
    parts = [f"{k}={v}" for k, v in sorted(_branches(ctx, slug).items())]
    try:
        parts.append(f"default={GitRepo(bare).head_branch()}")
    except GitError:
        pass
    return ", ".join(parts) or "nothing"


def _default_source(sources: dict[str, str]) -> Optional[str]:
    """The source name git's ``origin`` should alias, or ``None`` for a source-less repo.

    The remote you would reach for by default: **our writable canonical** where one
    exists (``origin`` if the repo is ours, else ``fork-ours``), otherwise the **read
    canonical** (``upstream``). Mirrors and third-party forks are tracked references and
    are never the default — see ``docs/design/layers/data/repo.md``.
    """
    for name in (ORIGIN, FORK_OURS, UPSTREAM):
        if sources.get(name):
            return name
    # No governed canonical: fall back to first-declared so an oddly-named source still
    # gets a usable default rather than leaving the repo with no `origin` at all.
    return next(iter(sources), None)


def _fetch_origin(repo: GitRepo, slug: str) -> Optional[dict[str, Any]]:
    """Fetch ``origin``; return a ``fetch-failed`` envelope on error, else ``None``.

    ``origin`` is conformed before this runs, so a failure here is a real one (network,
    auth, a bad URL) rather than the remote simply not existing — which is why there is
    no longer a fallback to fetching the URL anonymously. That fallback could not have
    helped anyway: an anonymous fetch lands in ``FETCH_HEAD`` and updates no refs.
    """
    try:
        repo.fetch(ORIGIN)
    except GitError as e:
        if is_auth_failure(str(e)):
            return _src("fetch-failed", _auth_summary(slug))
        return _src("fetch-failed", f"fetch {slug!r} failed: {e}")
    return None


def conform_remotes(repo: GitRepo, sources: dict[str, str]) -> list[str]:
    """Bring ``repo``'s git remotes into agreement with the declared sources.

    Two rules, both pure functions of the repo def:

    1. **Every declared source is a git remote under its governed name**, so
       ``git fetch <name>`` can be trusted.
    2. **``origin`` always exists and aliases the default source** (see
       :func:`_default_source`). The worktrees are worked in with *plain git*, which
       mono-control deliberately does not wrap — a checkout where ``git pull`` has no
       default is not usable by the tool the developer is actually holding.

    An upstream-based repo therefore carries both ``origin`` and ``fork-ours`` at the
    same URL. That duplication is intended: rule 1 holding *universally* is worth more
    than a tidy ``git remote -v``.

    **Additive** — remotes we do not recognise are left alone and returned, matching how
    ``scan`` reports unmanaged repos rather than deleting them. Idempotent: re-running
    changes nothing.
    """
    desired = dict(sources)
    default = _default_source(sources)
    if default is not None:
        desired[ORIGIN] = sources[default]

    existing = set(repo.remotes())
    for name, url in desired.items():
        if name in existing and repo.remote_url(name) != url:
            # Repoint: drop the stale tracking refs with the old remote.
            repo.remove_remote(name)
            existing.discard(name)
        if name not in existing:
            repo.set_remote(name, url)
        repo.set_fetch_refspec(name)
    return sorted(existing - set(desired))


def conform_tracking(repo: GitRepo) -> list[str]:
    """Re-point every local branch at ``origin``'s copy, where one exists.

    Conformance repoints a remote by remove-then-re-add, because ``set-url`` would
    leave ``refs/remotes/<name>/*`` describing the *old* remote — both remotes'
    branches under one name, all looking current. But ``git remote remove`` also
    wipes ``branch.<n>.remote`` / ``.merge`` for anything tracking it, so a
    repoint silently strips tracking and it stays gone until someone notices
    ``git pull`` complaining.

    Re-asserting it here is what closes that: conformance runs on every operation,
    so tracking is restored on the next one rather than left for a human to spot.
    Idempotent and additive, like the rest of conformance — branches with no
    counterpart on ``origin`` are skipped, not invented.
    """
    restored: list[str] = []
    try:
        branches = repo.local_branches()
    except GitError:
        return restored
    for branch in branches:
        try:
            if repo.set_upstream(branch):
                restored.append(branch)
        except GitError:
            continue
    return restored


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
    ``bare_root/<slug>``; there is no worktree yet (``place`` adds one later). Sources
    are resolved from the host repo def only — a URL is never accepted from the
    container here.

    Also **conforms the repo's git remotes** to those sources (see
    :func:`conform_remotes`) on both the create and already-present paths, so it is
    idempotent and self-healing: a def edited since the last run is picked up on the
    next operation. That is what makes fetching ``origin`` below meaningful rather than
    a guess, and — because ``clone --bare`` sets no fetch refspec — what makes a fetch
    update refs at all.
    """
    ctx = _require_ctx(ctx)
    slug = _valid_slug(params.get("slug"))
    refs = list(params.get("refs") or [])
    initial_branch = params.get("initial_branch")
    profile = host_profile()

    if not _repo_def_path(ctx, slug).is_file():
        return _src("definition-missing", f"repo def for {slug!r} not found")

    sources = _sources(ctx, slug)
    default = _default_source(sources)
    source_url = sources.get(default) if default is not None else None
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
        conform_remotes(repo, sources)
        # Populate remote-tracking refs, which `clone --bare` does not create. Nearly
        # free (the objects just arrived) and it means an acquired repo has the same ref
        # layout however it got here, rather than gaining `refs/remotes/*` only on a
        # later re-acquire.
        failed = _fetch_origin(repo, slug)
        if failed is not None:
            return failed
        conform_tracking(repo)
        return _verify_refs(repo, refs, "cloned", f"cloned {slug!r}", slug)

    # Present locally (offline or materialized) -> conform remotes, then fetch.
    repo = GitRepo(observed.bare)
    conform_remotes(repo, sources)
    if source_url is not None:
        failed = _fetch_origin(repo, slug)
        if failed is not None:
            return failed
        # After the fetch, so a branch whose counterpart only just arrived gets
        # tracking too. This is also the self-heal for a repoint, which drops
        # tracking along with the old remote.
        conform_tracking(repo)
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

    ``location`` is ignored — the worktree is derived from the slug. Two guards, and
    they cover opposite halves of the same promise ("retire never loses work"):

    - **uncommitted** changes are refused rather than silently discarded;
    - **committed** work that no ref anchors is refused too. Committed work is only
      safe in the bare repo if something points at it, and a detached HEAD's commits
      are held by the worktree alone — so removing it would leave them dangling. The
      dirty check cannot catch this, because committing is precisely what cleans the
      tree.
    """
    ctx = _require_ctx(ctx)
    slug = _valid_slug(params.get("slug"))
    observed = _location_of(ctx, slug)
    if observed is None or observed.worktree is None:
        return _race("retire", slug, "worktree vanished")
    if observed.dirty:
        return _lay("blocked", f"{slug!r} has uncommitted changes; refusing to discard its worktree")
    orphans = GitRepo(observed.worktree).unreachable_commit_count()
    if orphans:
        return _lay(
            "blocked",
            f"{slug!r} has {orphans} commit(s) on no branch, tag or remote — removing its "
            f"worktree would leave them unreachable; put them on a branch or tag them first",
        )
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


@verb("checkout_branch")
def _checkout_branch(params: dict[str, Any], ctx: Optional[HostContext]) -> dict[str, Any]:
    """Attach ``slug``'s worktree to ``branch`` — a line the repo **declares**.

    Why this exists beside ``checkout`` rather than relaxing it: checking out a
    hex commit always leaves HEAD detached, so the pin verb structurally cannot
    express "on this branch". One verb carrying both meanings lost the second.

    Why it is safe without sanitizing a ref: the branch is not taken on trust. It
    must resolve to a line the repo expresses — see ``_expressed_line`` — all read
    host-side. A value that does not never reaches git. That makes the container's
    reach here *narrower* than in ``checkout``, where any object it can name is
    fair game.

    Deliberately no branch creation: ``repo.md`` is explicit that mono-control
    does not create branches (commits do). An unborn declared line is reported,
    not conjured.
    """
    ctx = _require_ctx(ctx)
    slug = _valid_slug(params.get("slug"))
    requested = params.get("branch")
    if not isinstance(requested, str) or not requested:
        raise VerbError(INVALID_PARAMS, f"branch must be a non-empty string: {requested!r}")

    observed = _location_of(ctx, slug)
    if observed is None or observed.worktree is None:
        return _race("checkout_branch", slug, "worktree vanished")

    branch = _expressed_line(ctx, slug, observed.bare, requested)
    if branch is None:
        raise VerbError(
            INVALID_PARAMS,
            f"{requested!r} is not a line {slug!r} expresses "
            f"(expressed: {_expressed_lines(ctx, slug, observed.bare)})",
        )

    repo = GitRepo(observed.worktree)
    if repo.is_dirty():
        return _lay("blocked", f"{slug!r} became dirty between plan and execute")
    if repo.resolve_ref(f"refs/heads/{branch}") is None:
        return _lay(
            "failed",
            f"branch {branch!r} does not exist in {slug!r}; mono-control does not "
            f"create branches",
        )
    try:
        repo.checkout(branch)
    except GitError as e:
        return _lay("failed", f"checkout of branch {branch!r} failed for {slug!r}: {e}")
    # Attaching without tracking leaves `git pull` with "no tracking information",
    # which is most of what a developer wanted the branch for.
    tracked = repo.set_upstream(branch)
    detail = f" tracking {ORIGIN}/{branch}" if tracked else ""
    return _lay("checked-out", f"attached {slug!r} to branch {branch!r}{detail}")


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
