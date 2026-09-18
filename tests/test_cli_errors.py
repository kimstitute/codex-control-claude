"""Expected storage failures remain machine-readable at the public CLI boundary."""

import contextlib
import io
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))
from claude_control import cli  # noqa: E402


class StorageErrorTests(unittest.TestCase):
    def test_database_permission_failure_emits_json(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["claude-control", "list"]):
            with patch.object(
                cli, "execute", side_effect=sqlite3.OperationalError("unable to open database file")
            ):
                with contextlib.redirect_stdout(output):
                    code = cli.main()
        self.assertEqual(code, 2)
        error = json.loads(output.getvalue())
        self.assertEqual(error["error"], "operation_failed")
        self.assertIn("state directory write access", error["message"])


if __name__ == "__main__":
    unittest.main()
