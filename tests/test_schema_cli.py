"""Tests for the ``json-schema-control`` subcommand and the checked-in schema.

No docker here (the suite is hermetic): the container invocation is mocked to
capture how ``_dispatch`` is asked to run, and the committed schema file is
validated as the contract of record.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from mono_control_shim import cli


class CheckedInSchema(unittest.TestCase):
    def test_schema_file_exists_and_is_valid_json(self) -> None:
        self.assertTrue(cli.SCHEMA_PATH.is_file(), f"missing {cli.SCHEMA_PATH}")
        data = json.loads(cli.SCHEMA_PATH.read_text(encoding="utf-8"))
        # A spot-check that this is the broker wire contract, not something else.
        for model in ("AcquireRequest", "AcquireResult", "WireInventory", "OkResult"):
            self.assertIn(model, data)


class JsonSchemaControlWiring(unittest.TestCase):
    def test_dispatch_captures_emit_schema_stdout_into_the_repo(self) -> None:
        seen: dict = {}

        def fake_dispatch(workspace, inner_argv, *, stdout_path=None, **kw):  # noqa: ANN001
            seen["argv"] = inner_argv
            seen["stdout_path"] = stdout_path
            return 0

        workspace = mock.MagicMock(spec=cli.Path)
        with mock.patch.object(cli, "_dispatch", fake_dispatch):
            rc = cli._run_json_schema_control(workspace)

        self.assertEqual(rc, 0)
        self.assertEqual(seen["argv"], ["mono-control", "emit-schema"])
        self.assertEqual(seen["stdout_path"], cli.SCHEMA_PATH)

    def test_subcommand_is_registered(self) -> None:
        # Resolving with no workspace exits 1 (workspace error) rather than raising
        # an argparse error — proving the subcommand parses and routes.
        with mock.patch.object(cli, "resolve_workspace", return_value=None):
            rc = cli.main(["json-schema-control"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
