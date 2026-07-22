"""Tests for the ``mono_config`` verb pack (host-side ``mono-config/`` file I/O)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mono_control_shim import broker
from mono_control_shim.broker import HostContext, VerbError
from mono_control_shim.verbs import mono_config


class MonoConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.config = root / "mono-config"
        self.config.mkdir(parents=True)
        self.ctx = HostContext(
            workspace_root=root / "mono-repos",
            offline_root=root / "mono-repos-offline",
            config_dir=self.config,
        )

    def test_get_repo_defs_empty(self) -> None:
        self.assertEqual(mono_config._get_repo_defs({}, self.ctx), {"repos": {}})

    def test_save_then_get_repo_def(self) -> None:
        repo = {"slug": "demo", "name": "Demo", "sources": {"o": "https://x/y"}}
        self.assertEqual(mono_config._save_repo_def({"repo": repo}, self.ctx), {"ok": True})
        out = mono_config._get_repo_defs({}, self.ctx)
        self.assertEqual(out["repos"], {"demo": repo})
        # Written pretty-printed and slug-named on disk.
        self.assertEqual(json.loads((self.config / "repos" / "demo.json").read_text()), repo)

    def test_get_repo_defs_filters_by_slug(self) -> None:
        for slug in ("a", "b", "c"):
            mono_config._save_repo_def({"repo": {"slug": slug, "name": slug}}, self.ctx)
        out = mono_config._get_repo_defs({"slugs": ["a", "c"]}, self.ctx)
        self.assertEqual(sorted(out["repos"]), ["a", "c"])

    def test_purge_removes_and_missing_is_server_error(self) -> None:
        mono_config._save_repo_def({"repo": {"slug": "gone", "name": "g"}}, self.ctx)
        self.assertEqual(mono_config._purge_repo_def({"slug": "gone"}, self.ctx), {"ok": True})
        self.assertFalse((self.config / "repos" / "gone.json").exists())
        with self.assertRaises(VerbError) as cm:
            mono_config._purge_repo_def({"slug": "gone"}, self.ctx)
        self.assertEqual(cm.exception.code, broker.SERVER_ERROR)

    def test_system_roundtrip(self) -> None:
        self.assertEqual(mono_config._get_system({}, self.ctx), {"system": None})
        system = {"version": 1, "name": "sys"}
        mono_config._save_system({"system": system}, self.ctx)
        self.assertEqual(mono_config._get_system({}, self.ctx), {"system": system})

    def test_save_repo_def_with_hostile_slug_is_rejected(self) -> None:
        with self.assertRaises(VerbError) as cm:
            mono_config._save_repo_def({"repo": {"slug": "../escape", "name": "x"}}, self.ctx)
        self.assertEqual(cm.exception.code, broker.INVALID_PARAMS)
        self.assertFalse((self.config.parent / "escape.json").exists())


class Registration(unittest.TestCase):
    """Importing the packs registers exactly the expected verb set (the guard)."""

    def test_broker_lists_the_full_step2_verb_set(self) -> None:
        import mono_control_shim.verbs  # noqa: F401  (import = register)

        expected = {
            # Step 1 transport core
            "ping",
            "broker.info",
            # git pack
            "scan",
            "acquire",
            "place",
            "relocate",
            "retire",
            "checkout",
            "read_layout",
            "write_layout",
            "remote_default_branch",
            # mono_config pack
            "get_repo_defs",
            "save_repo_def",
            "purge_repo_def",
            "get_system",
            "save_system",
        }
        self.assertEqual(set(broker._VERBS), expected)


if __name__ == "__main__":
    unittest.main()
