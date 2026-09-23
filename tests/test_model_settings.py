"""Validation tests for versioned model selectors and per-role defaults."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

import unittest

from claude_control.model_settings import (  # noqa: E402
    CONTRACT,
    ROLES,
    model_matches,
    normalize_defaults,
    validate_model,
)


class ModelSettingsTests(unittest.TestCase):
    def document(self):
        return {
            "contract": CONTRACT,
            "roles": {role: {"model": "claude-opus-5", "effort": "high"} for role in ROLES},
        }

    def test_family_aliases_and_exact_versions_have_distinct_matching_rules(self):
        self.assertEqual(validate_model("opus"), "opus")
        self.assertEqual(validate_model("claude-opus-5"), "claude-opus-5")
        self.assertTrue(model_matches("opus", "claude-opus-5-1"))
        self.assertTrue(model_matches("claude-opus-5", "claude-opus-5"))
        self.assertFalse(model_matches("claude-opus-5", "claude-opus-5-1"))

    def test_one_million_context_selectors_preserve_model_identity_checks(self):
        self.assertEqual(validate_model("opus[1m]"), "opus[1m]")
        self.assertEqual(validate_model("claude-fable-5-1[1m]"), "claude-fable-5-1[1m]")
        self.assertTrue(model_matches("opus[1m]", "claude-opus-5"))
        self.assertTrue(model_matches("claude-fable-5-1[1m]", "claude-fable-5-1"))

    def test_unknown_alias_and_option_like_selector_are_rejected(self):
        for value in ("best", "--model", "Opus 5", "claude-"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_model(value)

    def test_role_document_is_complete_and_effort_can_be_omitted(self):
        document = self.document()
        del document["roles"]["researcher"]["effort"]

        normalized = normalize_defaults(document)

        self.assertNotIn("effort", normalized["roles"]["researcher"])
        self.assertEqual(normalized["roles"]["critic"]["effort"], "high")

    def test_partial_or_null_effort_configuration_fails_closed(self):
        partial = self.document()
        del partial["roles"]["verifier"]
        with self.assertRaises(ValueError):
            normalize_defaults(partial)

        null_effort = self.document()
        null_effort["roles"]["critic"]["effort"] = None
        with self.assertRaises(ValueError):
            normalize_defaults(null_effort)


if __name__ == "__main__":
    unittest.main()
