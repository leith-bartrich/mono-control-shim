"""Tests for the ``git`` verb pack, against real git on real temp directories.

Stdlib ``unittest`` + the real ``git`` binary, no mocks of git itself: the whole
point of re-hosting these effects is that they run natively, so a test that faked
git would prove nothing about the thing this pack exists to fix. Each case mirrors
a scenario ``mono-control/tests/broker_shim.py`` exercises, so behavior provably
matches what the container expects.

    python -m unittest discover -s tests -t .

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


class GitVerbsCase(unittest.TestCase):
    """A temp workspace / offline / mono-config, plus a local bare origin."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.workspace = root / "mono-repos"
        self.offline = root / "mono-repos-offline"
        self.config = root / "mono-config"
        for d in (self.workspace, self.offline, self.config, self.config / "repos"):
            d.mkdir(parents=True, exist_ok=True)
        self.ctx = HostContext(
            workspace_root=self.workspace,
            offline_root=self.offline,
            config_dir=self.config,
            github_token=None,
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

    def test_scan_reports_managed_and_unmanaged_with_relative_locations(self) -> None:
        # A managed checkout (slug-stamped) placed offline.
        self._write_repo_def("demo", sources=None)
        git._acquire({"slug": "demo"}, self.ctx)  # -> init offline/demo (no source)
        # An unmanaged (unstamped) checkout under the workspace root.
        foreign = self.workspace / "foreign"
        foreign.mkdir()
        _git(["init"], foreign)

        result = self._scan()

        self.assertEqual(len(result["repos"]), 1)
        repo = result["repos"][0]
        self.assertEqual(repo["slug"], "demo")
        self.assertEqual(repo["location"], "demo")  # relative to the offline root
        self.assertEqual(repo["state"], "offline")
        self.assertEqual(result["unmanaged"], [{"location": "foreign", "state": "materialized"}])

    # -- acquire ----------------------------------------------------------- #
    def test_acquire_definition_missing(self) -> None:
        out = git._acquire({"slug": "ghost", "refs": []}, self.ctx)
        self.assertEqual(out["status"], "definition-missing")

    def test_acquire_initializes_sourceless_repo(self) -> None:
        self._write_repo_def("fresh", sources=None)
        out = git._acquire({"slug": "fresh", "initial_branch": "main"}, self.ctx)
        self.assertEqual(out["status"], "initialized")
        self.assertTrue((self.offline / "fresh" / ".git").exists())
        self.assertEqual(git.GitRepo(self.offline / "fresh").slug(), "fresh")

    def test_acquire_source_missing_when_refs_requested_without_source(self) -> None:
        self._write_repo_def("fresh", sources=None)
        out = git._acquire({"slug": "fresh", "refs": ["refs/heads/main"]}, self.ctx)
        self.assertEqual(out["status"], "source-missing")
        self.assertEqual(out["unresolved_refs"], ["refs/heads/main"])

    def test_acquire_clones_and_resolves_branch_head(self) -> None:
        bare, commits = self._make_origin()
        self._write_repo_def("proj", sources={"origin": str(bare)})

        out = git._acquire({"slug": "proj", "refs": ["refs/heads/main"]}, self.ctx)

        self.assertEqual(out["status"], "cloned")
        self.assertEqual(out["unresolved_refs"], [])
        # Keyed by the requested ref, resolving via origin's copy of the branch head.
        self.assertEqual(out["resolved"], {"refs/heads/main": commits[-1]})
        self.assertTrue((self.offline / "proj" / ".git").exists())

    def test_acquire_ref_missing_for_unknown_ref(self) -> None:
        bare, _ = self._make_origin()
        self._write_repo_def("proj", sources={"origin": str(bare)})
        out = git._acquire({"slug": "proj", "refs": ["refs/heads/nope"]}, self.ctx)
        self.assertEqual(out["status"], "ref-missing")
        self.assertEqual(out["unresolved_refs"], ["refs/heads/nope"])

    def test_acquire_fetches_when_already_present(self) -> None:
        bare, commits = self._make_origin()
        self._write_repo_def("proj", sources={"origin": str(bare)})
        git._acquire({"slug": "proj", "refs": []}, self.ctx)  # clone
        out = git._acquire({"slug": "proj", "refs": ["refs/heads/main"]}, self.ctx)
        self.assertEqual(out["status"], "fetched")
        self.assertEqual(out["resolved"], {"refs/heads/main": commits[-1]})

    # -- place / relocate / retire ----------------------------------------- #
    def _acquire_offline(self, slug: str = "proj") -> list[str]:
        bare, commits = self._make_origin(slug)
        self._write_repo_def(slug, sources={"origin": str(bare)})
        git._acquire({"slug": slug, "refs": []}, self.ctx)
        return commits

    def test_place_moves_offline_checkout_into_workspace(self) -> None:
        self._acquire_offline("proj")
        out = git._place({"slug": "proj", "location": "cluster/proj"}, self.ctx)
        self.assertEqual(out["status"], "placed")
        self.assertIn("cluster/proj", out["summary"])
        self.assertTrue((self.workspace / "cluster" / "proj" / ".git").exists())
        self.assertFalse((self.offline / "proj").exists())
        # scan now reports it materialized at the relative location.
        repo = self._scan()["repos"][0]
        self.assertEqual((repo["location"], repo["state"]), ("cluster/proj", "materialized"))

    def test_place_onto_occupied_destination_is_race_aborted(self) -> None:
        self._acquire_offline("proj")
        (self.workspace / "taken").mkdir()
        (self.workspace / "taken" / "keep").write_text("x")
        out = git._place({"slug": "proj", "location": "taken"}, self.ctx)
        self.assertEqual(out["status"], "race-aborted")

    def test_place_vanished_checkout_is_race_aborted(self) -> None:
        self._write_repo_def("proj", sources=None)  # def exists, nothing on disk
        out = git._place({"slug": "proj", "location": "here"}, self.ctx)
        self.assertEqual(out["status"], "race-aborted")

    def test_relocate_between_materialized_locations(self) -> None:
        self._acquire_offline("proj")
        git._place({"slug": "proj", "location": "one"}, self.ctx)
        out = git._relocate({"slug": "proj", "location": "two/proj"}, self.ctx)
        self.assertEqual(out["status"], "relocated")
        self.assertTrue((self.workspace / "two" / "proj" / ".git").exists())
        self.assertFalse((self.workspace / "one").exists())

    def test_retire_moves_back_to_offline(self) -> None:
        self._acquire_offline("proj")
        git._place({"slug": "proj", "location": "live"}, self.ctx)
        out = git._retire({"slug": "proj"}, self.ctx)
        self.assertEqual(out["status"], "retired")
        self.assertTrue((self.offline / "proj" / ".git").exists())

    def test_retire_blocked_when_offline_spot_occupied(self) -> None:
        self._acquire_offline("proj")
        git._place({"slug": "proj", "location": "live"}, self.ctx)
        (self.offline / "proj").mkdir()  # spot already taken
        out = git._retire({"slug": "proj"}, self.ctx)
        self.assertEqual(out["status"], "blocked")

    # -- checkout ---------------------------------------------------------- #
    def test_checkout_switches_commit(self) -> None:
        commits = self._acquire_offline("proj")
        git._place({"slug": "proj", "location": "live"}, self.ctx)
        out = git._checkout({"slug": "proj", "commit": commits[0]}, self.ctx)
        self.assertEqual(out["status"], "checked-out")
        head = git.GitRepo(self.workspace / "live").current_commit()
        self.assertEqual(head, commits[0])

    def test_checkout_blocked_when_dirty(self) -> None:
        commits = self._acquire_offline("proj")
        git._place({"slug": "proj", "location": "live"}, self.ctx)
        (self.workspace / "live" / "a.txt").write_text("dirtied\n")
        out = git._checkout({"slug": "proj", "commit": commits[0]}, self.ctx)
        self.assertEqual(out["status"], "blocked")

    # -- read_layout / write_layout ---------------------------------------- #
    def test_write_then_read_layout(self) -> None:
        self._acquire_offline("cl")
        git._place({"slug": "cl", "location": "cl"}, self.ctx)
        payload = {"members": ["a", "b"]}
        self.assertEqual(git._write_layout({"cluster_slug": "cl", "layout": payload}, self.ctx), {"ok": True})
        out = git._read_layout({"cluster_slug": "cl"}, self.ctx)
        self.assertEqual(out, {"exists": True, "layout": payload})

    def test_read_layout_absent_is_exists_false(self) -> None:
        self._acquire_offline("cl")
        git._place({"slug": "cl", "location": "cl"}, self.ctx)
        self.assertEqual(git._read_layout({"cluster_slug": "cl"}, self.ctx), {"exists": False, "layout": None})

    def test_write_layout_not_on_disk_is_server_error(self) -> None:
        with self.assertRaises(VerbError) as cm:
            git._write_layout({"cluster_slug": "nope", "layout": {}}, self.ctx)
        self.assertEqual(cm.exception.code, broker.SERVER_ERROR)

    # -- remote_default_branch (git mechanics, hermetic) ------------------- #
    def test_ls_remote_symref_reads_default_branch_from_local_bare(self) -> None:
        bare, _ = self._make_origin()
        # The verb rejects file://; the git-mechanics helper is exercised directly
        # against a local bare repo to prove the --symref parse without a network.
        self.assertEqual(git.ls_remote_symref(str(bare)), "main")


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


class CredentialHardening(unittest.TestCase):
    """The token reaches git via env + a github-scoped helper, never on argv."""

    def test_credential_config_scopes_helper_to_github_and_omits_token(self) -> None:
        config = git._credential_config("s3cr3t-token")
        joined = " ".join(config)
        self.assertIn("credential.https://github.com.helper", joined)
        self.assertNotIn("s3cr3t-token", joined)  # THE assertion: no token in argv
        # References the env var by name so git's own shell expands it at call time.
        self.assertIn(git._GIT_TOKEN_ENV, joined)

    def test_no_token_registers_no_helper(self) -> None:
        self.assertEqual(git._credential_config(None), [])
        self.assertEqual(git._credential_config(""), [])

    def test_network_env_hardens_and_carries_token_in_env_only(self) -> None:
        env = git._network_env("tok", https_only=True)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "https")
        self.assertEqual(env[git._GIT_TOKEN_ENV], "tok")

    def test_network_env_without_https_only_leaves_protocol_unrestricted(self) -> None:
        env = git._network_env("tok")
        self.assertNotIn("GIT_ALLOW_PROTOCOL", env)


if __name__ == "__main__":
    unittest.main()
