"""Tests for the ``git`` verb pack, against real git on real temp directories.

Stdlib ``unittest`` + the real ``git`` binary, no mocks of git itself: the whole
point of re-hosting these effects is that they run natively, so a test that faked
git would prove nothing about the thing this pack exists to fix. Each case mirrors
a scenario ``mono-control/tests/broker_shim.py`` exercises, so behavior provably
matches what the container expects.

    python -m unittest discover -s tests -t .

Physical model: **bare repos + git worktrees**. ``acquire`` creates a bare repo
under ``bare_root``; ``place`` adds a worktree under ``work_root`` (additive — it
moves nothing, which is exactly what fixed the old ``WinError 5`` / drvfs move
bug); ``retire`` removes the worktree while the bare repo (and its commits)
survive; ``relocate`` is a native ``git worktree move``.

Hermetic and offline: ``acquire`` clones from a *local* bare repo (a path, not a
network URL), and the ``remote_default_branch`` git mechanics are exercised
against a local bare repo too — no test reaches the network.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mono_control_shim import broker
from mono_control_shim.broker import HostContext, VerbError
from mono_control_shim.verbs import git


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


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content)
    _git(["add", "."], repo)
    _git(["commit", "-m", f"add {name}"], repo)
    return _git(["rev-parse", "HEAD"], repo)


def _is_bare(path: Path) -> bool:
    return _git(["-C", str(path), "rev-parse", "--is-bare-repository"], path) == "true"


class GitVerbsCase(unittest.TestCase):
    """A temp work root / bare root / mono-config, plus a local bare origin."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.work = root / "mono-work"
        self.bare = root / "mono-repos-bare"
        self.config = root / "mono-config"
        for d in (self.work, self.bare, self.config, self.config / "repos"):
            d.mkdir(parents=True, exist_ok=True)
        self.ctx = HostContext(
            work_root=self.work,
            bare_root=self.bare,
            config_dir=self.config,
        )

    # -- fixtures ---------------------------------------------------------- #
    def _make_origin(self, name: str = "origin") -> tuple[Path, list[str]]:
        """A local *bare* repo on branch ``main`` with two commits; returns commits."""
        work = Path(self._tmp.name) / f"{name}-work"
        work.mkdir()
        _git(["init", "-b", "main"], work)
        c1 = _commit(work, "a.txt", "one\n")
        c2 = _commit(work, "b.txt", "two\n")
        bare = Path(self._tmp.name) / f"{name}.git"
        _git(["clone", "--bare", str(work), str(bare)], Path(self._tmp.name))
        return bare, [c1, c2]

    def _write_repo_def(self, slug: str, sources: dict[str, str] | None) -> None:
        data: dict = {"slug": slug, "name": slug}
        if sources is not None:
            data["sources"] = sources
        (self.config / "repos" / f"{slug}.json").write_text(json.dumps(data))

    def _scan(self) -> dict:
        return git._scan({}, self.ctx)

    # -- scan -------------------------------------------------------------- #
    def test_scan_empty_roots(self) -> None:
        self.assertEqual(self._scan(), {"repos": [], "unmanaged": []})

    def test_scan_reports_offline_bare_and_unmanaged(self) -> None:
        # A managed bare (slug-stamped), no worktree -> offline, located by its slug.
        self._write_repo_def("demo", sources=None)
        git._acquire({"slug": "demo"}, self.ctx)  # -> init bare_root/demo (no source)
        # An unmanaged (unstamped) bare repo sitting under the bare root.
        _git(["init", "--bare", str(self.bare / "foreign")], self.bare)

        result = self._scan()

        self.assertEqual(len(result["repos"]), 1)
        repo = result["repos"][0]
        self.assertEqual(repo["slug"], "demo")
        self.assertEqual(repo["location"], "demo")  # slug (relative to the bare root)
        self.assertEqual(repo["state"], "offline")
        self.assertFalse(repo["dirty"])
        self.assertEqual(result["unmanaged"], [{"location": "foreign", "state": "offline"}])

    def test_scan_reports_materialized_when_worktree_present(self) -> None:
        commits = self._acquire_offline("proj")
        git._place({"slug": "proj", "location": "cluster/proj"}, self.ctx)

        repo = self._scan()["repos"][0]
        self.assertEqual(repo["slug"], "proj")
        self.assertEqual(repo["location"], "cluster/proj")  # worktree, rel to work root
        self.assertEqual(repo["state"], "materialized")
        self.assertEqual(repo["commit"], commits[-1])  # worktree HEAD
        self.assertFalse(repo["dirty"])

    def test_scan_reports_dirty_worktree(self) -> None:
        self._acquire_offline("proj")
        git._place({"slug": "proj", "location": "live"}, self.ctx)
        (self.work / "live" / "a.txt").write_text("dirtied\n")
        repo = self._scan()["repos"][0]
        self.assertTrue(repo["dirty"])

    # -- acquire (creates a BARE repo) ------------------------------------- #
    def test_acquire_definition_missing(self) -> None:
        out = git._acquire({"slug": "ghost", "refs": []}, self.ctx)
        self.assertEqual(out["status"], "definition-missing")

    def test_acquire_initializes_sourceless_bare_repo(self) -> None:
        self._write_repo_def("fresh", sources=None)
        out = git._acquire({"slug": "fresh", "initial_branch": "main"}, self.ctx)
        self.assertEqual(out["status"], "initialized")
        # It is a BARE repo (no working tree), stamped with its slug.
        self.assertTrue(_is_bare(self.bare / "fresh"))
        self.assertFalse((self.bare / "fresh" / ".git").exists())  # bare: no .git subdir
        self.assertEqual(git.GitRepo(self.bare / "fresh").slug(), "fresh")

    def test_acquire_source_missing_when_refs_requested_without_source(self) -> None:
        self._write_repo_def("fresh", sources=None)
        out = git._acquire({"slug": "fresh", "refs": ["refs/heads/main"]}, self.ctx)
        self.assertEqual(out["status"], "source-missing")
        self.assertEqual(out["unresolved_refs"], ["refs/heads/main"])

    def test_acquire_clones_bare_and_resolves_branch_head(self) -> None:
        bare, commits = self._make_origin()
        self._write_repo_def("proj", sources={"origin": str(bare)})

        out = git._acquire({"slug": "proj", "refs": ["refs/heads/main"]}, self.ctx)

        self.assertEqual(out["status"], "cloned")
        self.assertEqual(out["unresolved_refs"], [])
        # A bare clone keeps branch heads directly under refs/heads/*.
        self.assertEqual(out["resolved"], {"refs/heads/main": commits[-1]})
        self.assertTrue(_is_bare(self.bare / "proj"))

    def test_acquire_ref_missing_for_unknown_ref(self) -> None:
        bare, _ = self._make_origin()
        self._write_repo_def("proj", sources={"origin": str(bare)})
        out = git._acquire({"slug": "proj", "refs": ["refs/heads/nope"]}, self.ctx)
        self.assertEqual(out["status"], "ref-missing")
        self.assertEqual(out["unresolved_refs"], ["refs/heads/nope"])

    def test_acquire_fetches_when_already_present(self) -> None:
        bare, commits = self._make_origin()
        self._write_repo_def("proj", sources={"origin": str(bare)})
        git._acquire({"slug": "proj", "refs": []}, self.ctx)  # clone (bare)
        out = git._acquire({"slug": "proj", "refs": ["refs/heads/main"]}, self.ctx)
        self.assertEqual(out["status"], "fetched")
        self.assertEqual(out["resolved"], {"refs/heads/main": commits[-1]})

    # -- place / relocate / retire ----------------------------------------- #
    def _acquire_offline(self, slug: str = "proj") -> list[str]:
        bare, commits = self._make_origin(slug)
        self._write_repo_def(slug, sources={"origin": str(bare)})
        git._acquire({"slug": slug, "refs": []}, self.ctx)
        return commits

    def test_place_adds_worktree_and_leaves_bare_unmoved(self) -> None:
        self._acquire_offline("proj")
        out = git._place({"slug": "proj", "location": "cluster/proj"}, self.ctx)
        self.assertEqual(out["status"], "placed")
        self.assertIn("cluster/proj", out["summary"])
        # The worktree exists with the tracked files present...
        wt = self.work / "cluster" / "proj"
        self.assertTrue((wt / "a.txt").exists())
        self.assertTrue((wt / "b.txt").exists())
        # ...and the bare repo never moved (additive worktree add, no dir move).
        self.assertTrue(_is_bare(self.bare / "proj"))
        # git records the worktree against the bare repo.
        listing = _git(["-C", str(self.bare / "proj"), "worktree", "list"], self.bare / "proj")
        self.assertIn(str(wt.resolve()).replace("\\", "/"), listing.replace("\\", "/"))
        # scan now reports it materialized at the relative location.
        repo = self._scan()["repos"][0]
        self.assertEqual((repo["location"], repo["state"]), ("cluster/proj", "materialized"))

    def test_place_onto_occupied_destination_is_race_aborted(self) -> None:
        self._acquire_offline("proj")
        (self.work / "taken").mkdir()
        (self.work / "taken" / "keep").write_text("x")
        out = git._place({"slug": "proj", "location": "taken"}, self.ctx)
        self.assertEqual(out["status"], "race-aborted")

    def test_place_vanished_repo_is_race_aborted(self) -> None:
        self._write_repo_def("proj", sources=None)  # def exists, nothing on disk
        out = git._place({"slug": "proj", "location": "here"}, self.ctx)
        self.assertEqual(out["status"], "race-aborted")

    def test_relocate_moves_worktree(self) -> None:
        self._acquire_offline("proj")
        git._place({"slug": "proj", "location": "one"}, self.ctx)
        out = git._relocate({"slug": "proj", "location": "two/proj"}, self.ctx)
        self.assertEqual(out["status"], "relocated")
        self.assertTrue((self.work / "two" / "proj" / "a.txt").exists())
        self.assertFalse((self.work / "one").exists())
        # Still one worktree, now at the new location; scan agrees.
        repo = self._scan()["repos"][0]
        self.assertEqual((repo["location"], repo["state"]), ("two/proj", "materialized"))

    def test_retire_removes_worktree_and_bare_survives(self) -> None:
        self._acquire_offline("proj")
        git._place({"slug": "proj", "location": "live"}, self.ctx)
        out = git._retire({"slug": "proj"}, self.ctx)
        self.assertEqual(out["status"], "retired")
        self.assertFalse((self.work / "live").exists())  # worktree gone
        self.assertTrue(_is_bare(self.bare / "proj"))  # bare repo survives
        # Back to offline in the inventory.
        repo = self._scan()["repos"][0]
        self.assertEqual((repo["location"], repo["state"]), ("proj", "offline"))

    def test_retire_blocked_on_dirty_worktree(self) -> None:
        # Committed work is safe in the bare repo; an uncommitted worktree is refused.
        self._acquire_offline("proj")
        git._place({"slug": "proj", "location": "live"}, self.ctx)
        (self.work / "live" / "a.txt").write_text("uncommitted change\n")
        out = git._retire({"slug": "proj"}, self.ctx)
        self.assertEqual(out["status"], "blocked")
        self.assertTrue((self.work / "live").exists())  # worktree left in place

    def test_retire_vanished_worktree_is_race_aborted(self) -> None:
        # Offline (never placed): there is no worktree to retire.
        self._acquire_offline("proj")
        out = git._retire({"slug": "proj"}, self.ctx)
        self.assertEqual(out["status"], "race-aborted")

    # -- checkout ---------------------------------------------------------- #
    def test_checkout_switches_commit_in_worktree(self) -> None:
        commits = self._acquire_offline("proj")
        git._place({"slug": "proj", "location": "live"}, self.ctx)
        out = git._checkout({"slug": "proj", "commit": commits[0]}, self.ctx)
        self.assertEqual(out["status"], "checked-out")
        head = git.GitRepo(self.work / "live").current_commit()
        self.assertEqual(head, commits[0])

    def test_checkout_blocked_when_dirty(self) -> None:
        commits = self._acquire_offline("proj")
        git._place({"slug": "proj", "location": "live"}, self.ctx)
        (self.work / "live" / "a.txt").write_text("dirtied\n")
        out = git._checkout({"slug": "proj", "commit": commits[0]}, self.ctx)
        self.assertEqual(out["status"], "blocked")

    # -- read_layout / write_layout ---------------------------------------- #
    def test_write_then_read_layout_from_worktree(self) -> None:
        self._acquire_offline("cl")
        git._place({"slug": "cl", "location": "cl"}, self.ctx)
        payload = {"members": ["a", "b"]}
        self.assertEqual(
            git._write_layout({"cluster_slug": "cl", "layout": payload}, self.ctx),
            {"ok": True},
        )
        out = git._read_layout({"cluster_slug": "cl"}, self.ctx)
        self.assertEqual(out, {"exists": True, "layout": payload})

    def test_read_layout_absent_is_exists_false(self) -> None:
        self._acquire_offline("cl")
        git._place({"slug": "cl", "location": "cl"}, self.ctx)
        self.assertEqual(
            git._read_layout({"cluster_slug": "cl"}, self.ctx),
            {"exists": False, "layout": None},
        )

    def test_read_layout_reads_committed_blob_from_bare_when_offline(self) -> None:
        # A cluster repo whose origin already COMMITTED a layout doc; clone it bare
        # and, without placing a worktree, read the layout out of the bare HEAD.
        work = Path(self._tmp.name) / "cl-src"
        (work / "product-cluster").mkdir(parents=True)
        _git(["init", "-b", "main"], work)
        payload = {"members": ["x", "y"]}
        (work / "product-cluster" / "default-layout.json").write_text(json.dumps(payload))
        _git(["add", "."], work)
        _git(["commit", "-m", "layout"], work)
        bare = Path(self._tmp.name) / "cl.git"
        _git(["clone", "--bare", str(work), str(bare)], Path(self._tmp.name))
        self._write_repo_def("cl", sources={"origin": str(bare)})
        git._acquire({"slug": "cl", "refs": []}, self.ctx)  # bare clone, NOT placed

        obs = git._location_of(self.ctx, "cl")
        self.assertEqual(obs.state, "offline")  # no worktree
        out = git._read_layout({"cluster_slug": "cl"}, self.ctx)
        self.assertEqual(out, {"exists": True, "layout": payload})

    def test_read_layout_missing_slug_is_exists_false(self) -> None:
        self.assertEqual(
            git._read_layout({"cluster_slug": "nope"}, self.ctx),
            {"exists": False, "layout": None},
        )

    def test_write_layout_not_materialized_is_server_error(self) -> None:
        # Offline (bare, no worktree): writing requires a materialized worktree.
        self._acquire_offline("cl")
        with self.assertRaises(VerbError) as cm:
            git._write_layout({"cluster_slug": "cl", "layout": {}}, self.ctx)
        self.assertEqual(cm.exception.code, broker.SERVER_ERROR)

    def test_write_layout_missing_slug_is_server_error(self) -> None:
        with self.assertRaises(VerbError) as cm:
            git._write_layout({"cluster_slug": "nope", "layout": {}}, self.ctx)
        self.assertEqual(cm.exception.code, broker.SERVER_ERROR)

    # -- remote_default_branch (git mechanics, hermetic) ------------------- #
    def test_ls_remote_symref_reads_default_branch_from_local_bare(self) -> None:
        bare, _ = self._make_origin()
        # The verb rejects file://; the git-mechanics helper is exercised directly
        # against a local bare repo to prove the --symref parse without a network.
        self.assertEqual(git.ls_remote_symref(str(bare)), "main")


class SetRemote(GitVerbsCase):
    """``set_remote`` adds or repoints a remote on the managed bare repo (local, no net)."""

    def _remote_url(self, repo_dir: Path, name: str) -> str:
        return _git(["-C", str(repo_dir), "remote", "get-url", name], repo_dir)

    def test_set_remote_adds_when_absent(self) -> None:
        self._acquire_offline("proj")
        url = "https://github.com/o/fork.git"
        self.assertEqual(
            git._set_remote({"slug": "proj", "name": "upstream", "url": url}, self.ctx),
            {"ok": True},
        )
        self.assertEqual(self._remote_url(self.bare / "proj", "upstream"), url)

    def test_set_remote_repoints_existing(self) -> None:
        self._acquire_offline("proj")
        repo_dir = self.bare / "proj"
        _git(["-C", str(repo_dir), "remote", "add", "origin2", "https://github.com/o/old.git"], repo_dir)
        new_url = "https://github.com/o/new.git"
        self.assertEqual(
            git._set_remote(
                {"slug": "proj", "name": "origin2", "url": new_url}, self.ctx
            ),
            {"ok": True},
        )
        self.assertEqual(self._remote_url(repo_dir, "origin2"), new_url)

    def test_set_remote_repoints_repo_default_origin(self) -> None:
        # ``acquire`` clones, so ``origin`` already exists — repoint it in place.
        self._acquire_offline("proj")
        new_url = "https://github.com/o/moved.git"
        git._set_remote({"slug": "proj", "name": "origin", "url": new_url}, self.ctx)
        self.assertEqual(self._remote_url(self.bare / "proj", "origin"), new_url)

    def test_set_remote_not_on_disk_is_server_error(self) -> None:
        self._write_repo_def("ghost", sources=None)  # def exists, nothing on disk
        with self.assertRaises(VerbError) as cm:
            git._set_remote(
                {"slug": "ghost", "name": "origin", "url": "https://github.com/o/r.git"},
                self.ctx,
            )
        self.assertEqual(cm.exception.code, broker.SERVER_ERROR)

    def test_set_remote_rejects_invalid_slug(self) -> None:
        for bad in ("../escape", "a/b", "..", "", ".hidden"):
            with self.assertRaises(VerbError) as cm:
                git._set_remote(
                    {"slug": bad, "name": "origin", "url": "https://github.com/o/r.git"},
                    self.ctx,
                )
            self.assertEqual(cm.exception.code, broker.INVALID_PARAMS)

    def test_set_remote_rejects_bad_remote_name(self) -> None:
        self._acquire_offline("proj")
        for bad in ("a/b", "../x", "..", "", ".hidden", "-flag"):
            with self.assertRaises(VerbError) as cm:
                git._set_remote(
                    {"slug": "proj", "name": bad, "url": "https://github.com/o/r.git"},
                    self.ctx,
                )
            self.assertEqual(cm.exception.code, broker.INVALID_PARAMS)

    def test_set_remote_rejects_non_https_or_helper_url(self) -> None:
        self._acquire_offline("proj")
        for bad in ("file:///etc/passwd", "ext::sh -c id", "ssh://host/x", "http://x/y", "/local/path"):
            with self.assertRaises(VerbError) as cm:
                git._set_remote(
                    {"slug": "proj", "name": "upstream", "url": bad}, self.ctx
                )
            self.assertEqual(cm.exception.code, broker.INVALID_PARAMS)


class Validators(GitVerbsCase):
    """The boundary rejects hostile input before touching disk or spawning git."""

    def test_unknown_slug_shape_is_invalid_params(self) -> None:
        for bad in ("../escape", "a/b", "..", "", ".hidden"):
            with self.assertRaises(VerbError) as cm:
                git._acquire({"slug": bad}, self.ctx)
            self.assertEqual(cm.exception.code, broker.INVALID_PARAMS)

    def test_location_escape_is_invalid_params(self) -> None:
        self._acquire_offline("proj")
        for bad in ("../out", "/abs/path", "a/../../b"):
            with self.assertRaises(VerbError) as cm:
                git._place({"slug": "proj", "location": bad}, self.ctx)
            self.assertEqual(cm.exception.code, broker.INVALID_PARAMS)

    def test_non_hex_commit_is_invalid_params(self) -> None:
        for bad in ("main", "HEAD", "--force", "zzzz", "refs/heads/main"):
            with self.assertRaises(VerbError) as cm:
                git._checkout({"slug": "proj", "commit": bad}, self.ctx)
            self.assertEqual(cm.exception.code, broker.INVALID_PARAMS)

    def test_bad_url_scheme_is_invalid_params(self) -> None:
        for bad in ("file:///etc/passwd", "ext::sh -c id", "ssh://host/x", "http://x/y", "/local/path"):
            with self.assertRaises(VerbError) as cm:
                git._remote_default_branch({"url": bad}, self.ctx)
            self.assertEqual(cm.exception.code, broker.INVALID_PARAMS)

    def test_https_url_passes_the_scheme_gate(self) -> None:
        # It passes validation and then fails at the (unreachable) network probe,
        # surfacing as a server error — never an INVALID_PARAMS rejection.
        with self.assertRaises(VerbError) as cm:
            git._remote_default_branch(
                {"url": "https://127.0.0.1:1/nope.git"}, self.ctx
            )
        self.assertEqual(cm.exception.code, broker.SERVER_ERROR)


class ContextRequired(GitVerbsCase):
    """A git verb without a host context fails cleanly, never on a guessed root."""

    def test_missing_context_is_a_server_error(self) -> None:
        with self.assertRaises(VerbError) as cm:
            git._scan({}, None)
        self.assertEqual(cm.exception.code, broker.SERVER_ERROR)


class WorktreePlacementIsAdditive(GitVerbsCase):
    """Placement moves nothing held.

    This is the whole reason the physical model changed. The old model MOVED a
    directory (``mono-repos-offline/<slug>`` -> ``mono-repos/<loc>``) with
    ``os.rename``; on Windows an IDE holding those dirs made the move fail with
    ``WinError 5`` (and under drvfs, ``EACCES`` publishing a tree with read-only
    packfiles). ``place`` is now a ``git worktree add``: the bare repo stays put and
    a fresh worktree is written alongside it, so the class of held-directory move
    failure is gone by construction. There is nothing left to regression-test about
    a move — this case documents its removal and asserts the bare repo is untouched.
    """

    def test_place_leaves_the_bare_repo_in_place(self) -> None:
        self._acquire_offline("proj")
        bare_dir = self.bare / "proj"
        before = {p.name for p in bare_dir.iterdir()}
        git._place({"slug": "proj", "location": "cluster/proj"}, self.ctx)
        after = {p.name for p in bare_dir.iterdir()}
        # The bare repo is still a bare repo at the same path (nothing moved); the add
        # only *grows* it with worktree bookkeeping (a ``worktrees/`` dir), never
        # removes or relocates the object store.
        self.assertTrue(_is_bare(bare_dir))
        self.assertTrue(before <= after)  # existing entries all survive
        self.assertIn("objects", after)  # the object store is still right here


class NonInteractivePosture(GitVerbsCase):
    """Network git runs strictly non-interactively and injects NO credential.

    Host git resolves credentials from the host's own machinery; the broker's only
    job on the credential axis is to make sure a git op that could touch credentials
    fails fast (never hangs on a prompt / GUI popup) and hands git no token or helper.
    """

    def test_noninteractive_env_turns_off_the_terminal_prompt(self) -> None:
        env = git._noninteractive_env()
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        # No token / credential env is injected — host git supplies its own.
        self.assertNotIn("MONO_CONTROL_GITHUB_TOKEN", env)
        # Plain (non-https-only) network ops leave the protocol allow-list alone.
        self.assertNotIn("GIT_ALLOW_PROTOCOL", env)

    def test_https_only_adds_the_protocol_allowlist(self) -> None:
        env = git._noninteractive_env(https_only=True)
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "https")

    def test_noninteractive_config_suppresses_credential_gui_and_injects_no_helper(self) -> None:
        config = git._noninteractive_config()
        joined = " ".join(config)
        self.assertIn("credential.interactive=false", joined)
        # No credential helper and no token are ever put on git's command line.
        self.assertNotIn("helper", joined)
        self.assertNotIn("MONO_CONTROL_GITHUB_TOKEN", joined)

    def test_network_git_calls_carry_the_posture_and_no_token(self) -> None:
        # Spy on what the network subcommands (clone / fetch) hand run_git during a
        # real, hermetic acquire against a local bare repo.
        bare, _ = self._make_origin()
        self._write_repo_def("proj", sources={"origin": str(bare)})
        seen: list[tuple[str, dict, list]] = []
        real_run_git = git.run_git

        def spy(args, **kwargs):  # noqa: ANN001, ANN003
            if args and args[0] in ("clone", "fetch", "ls-remote"):
                seen.append((args[0], kwargs.get("env") or {}, kwargs.get("config") or []))
            return real_run_git(args, **kwargs)

        with mock.patch.object(git, "run_git", spy):
            git._acquire({"slug": "proj", "refs": []}, self.ctx)  # clone
            git._acquire({"slug": "proj", "refs": ["refs/heads/main"]}, self.ctx)  # fetch

        self.assertTrue(seen, "expected at least one network git subcommand")
        self.assertEqual({name for name, _, _ in seen}, {"clone", "fetch"})
        for name, env, config in seen:
            self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0", name)
            joined = " ".join(config)
            self.assertIn("credential.interactive=false", joined)
            self.assertNotIn("helper", joined)  # no injected credential helper
            self.assertNotIn("MONO_CONTROL_GITHUB_TOKEN", env)  # no injected token


class AuthFailureSummary(GitVerbsCase):
    """An auth-style git failure yields the actionable 'set up gh/credentials' summary.

    Hermetic: the failure is simulated by monkeypatching ``run_git`` to raise a
    ``GitError`` whose message carries an auth marker — no network is touched.
    """

    _AUTH_STDERR = (
        "`git clone https://github.com/o/r.git` failed: "
        "fatal: Authentication failed for 'https://github.com/o/r.git'"
    )

    def test_is_auth_failure_matches_auth_markers_only(self) -> None:
        self.assertTrue(git.is_auth_failure("fatal: Authentication failed for 'x'"))
        self.assertTrue(git.is_auth_failure(
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
        ))
        # A non-auth network error is not misclassified.
        self.assertFalse(git.is_auth_failure("fatal: unable to access 'x': Could not resolve host"))

    def _assert_actionable(self, summary: str) -> None:
        low = summary.lower()
        self.assertIn("authentication failed", low)
        self.assertIn("gh auth login", low)
        self.assertIn("github.com", low)

    def test_clone_auth_failure_returns_the_setup_hint(self) -> None:
        self._write_repo_def("proj", sources={"origin": "https://github.com/o/r.git"})

        def boom(args, **kwargs):  # noqa: ANN001, ANN003
            raise git.GitError(self._AUTH_STDERR)

        with mock.patch.object(git, "run_git", boom):
            out = git._acquire({"slug": "proj", "refs": []}, self.ctx)

        self.assertEqual(out["status"], "create-failed")
        self._assert_actionable(out["summary"])

    def test_fetch_auth_failure_returns_the_setup_hint(self) -> None:
        # A real offline bare repo first, then auth failure on the fetch only.
        self._acquire_offline("proj")
        real_run_git = git.run_git

        def spy(args, **kwargs):  # noqa: ANN001, ANN003
            if args and args[0] == "fetch":
                raise git.GitError(
                    "`git fetch origin` failed: remote: Invalid username or password."
                )
            return real_run_git(args, **kwargs)

        with mock.patch.object(git, "run_git", spy):
            out = git._acquire({"slug": "proj", "refs": ["refs/heads/main"]}, self.ctx)

        self.assertEqual(out["status"], "fetch-failed")
        self._assert_actionable(out["summary"])

    def test_remote_default_branch_auth_failure_is_actionable_server_error(self) -> None:
        def boom(args, **kwargs):  # noqa: ANN001, ANN003
            raise git.GitError(
                "`git ls-remote --symref https://github.com/o/r.git HEAD` failed: "
                "fatal: Authentication failed for 'https://github.com/o/r.git'"
            )

        with mock.patch.object(git, "run_git", boom):
            with self.assertRaises(VerbError) as cm:
                git._remote_default_branch({"url": "https://github.com/o/r.git"}, self.ctx)

        self.assertEqual(cm.exception.code, broker.SERVER_ERROR)
        self._assert_actionable(cm.exception.message)

    def test_non_auth_clone_failure_keeps_the_raw_error(self) -> None:
        # A non-auth failure must NOT be dressed up as an auth problem.
        self._write_repo_def("proj", sources={"origin": "https://github.com/o/r.git"})

        def boom(args, **kwargs):  # noqa: ANN001, ANN003
            raise git.GitError("`git clone ...` failed: fatal: could not resolve host")

        with mock.patch.object(git, "run_git", boom):
            out = git._acquire({"slug": "proj", "refs": []}, self.ctx)

        self.assertEqual(out["status"], "create-failed")
        self.assertNotIn("gh auth login", out["summary"])
        self.assertIn("could not resolve host", out["summary"])


if __name__ == "__main__":
    unittest.main()
