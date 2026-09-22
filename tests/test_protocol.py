"""Behavioral tests for Claude CLI argument and stream protocol handling."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "plugins" / "claude-control" / "scripts"
sys.path.insert(0, str(PACKAGE_ROOT))

from claude_control.protocol import build_argv, parse_stream  # noqa: E402

SESSION_ID = "00000000-0000-4000-8000-000000000001"


def _write_events(directory: Path, events: list[object]) -> Path:
    path = directory / "events.jsonl"
    path.write_text(
        "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
        encoding="utf-8",
    )
    return path


def _success_events(
    *,
    assistant_model: str = "claude-sonnet-test",
    session_id: str = SESSION_ID,
) -> list[dict[str, object]]:
    return [
        {
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "model": "claude-untrusted-init-model",
        },
        {
            "type": "assistant",
            "session_id": session_id,
            "message": {
                "model": assistant_model,
                "content": [{"type": "text", "text": "OK"}],
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "session_id": session_id,
            "is_error": False,
            "result": "OK",
            "usage": {"input_tokens": 2, "output_tokens": 1},
            "modelUsage": {"claude-auxiliary-test": {"inputTokens": 1}},
            "total_cost_usd": 0.0125,
            "duration_api_ms": 321,
        },
    ]


class BuildArgvTests(unittest.TestCase):
    def test_new_session_disables_tools_and_uses_explicit_session(self) -> None:
        argv = build_argv("/opt/claude", "sonnet", SESSION_ID)

        self.assertEqual(argv[0], "/opt/claude")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--session-id") + 1], SESSION_ID)
        self.assertNotIn("--resume", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(argv[argv.index("--mcp-config") + 1], '{"mcpServers":{}}')
        for required_flag in (
            "--safe-mode",
            "--strict-mcp-config",
            "--output-format",
            "--verbose",
            "-p",
        ):
            self.assertIn(required_flag, argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")

    def test_resume_uses_only_the_explicit_backend_session(self) -> None:
        argv = build_argv("claude", "fable", SESSION_ID, resume=True)

        self.assertEqual(argv[argv.index("--resume") + 1], SESSION_ID)
        self.assertNotIn("--session-id", argv)

    def test_structured_schema_is_canonical_and_explicit(self) -> None:
        schema = {"required": ["status"], "type": "object"}
        argv = build_argv("claude", "sonnet", SESSION_ID, json_schema=schema)

        self.assertEqual(
            argv[argv.index("--json-schema") + 1],
            '{"required":["status"],"type":"object"}',
        )

    def test_rejects_unknown_model_alias(self) -> None:
        with self.assertRaises(ValueError):
            build_argv("claude", "opus", SESSION_ID)

    def test_rejects_non_uuid_session_identifier(self) -> None:
        with self.assertRaises(ValueError):
            build_argv("claude", "sonnet", "latest")


class ParseStreamTests(unittest.TestCase):
    def test_accepts_complete_tool_free_matching_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = _write_events(Path(temp), _success_events())

            parsed = parse_stream(path, "sonnet", SESSION_ID)

        self.assertEqual(parsed["response"], "OK")
        self.assertEqual(parsed["actual_models"], ["claude-sonnet-test"])
        self.assertEqual(parsed["observed_session_ids"], [SESSION_ID])
        self.assertEqual(parsed["tool_use_count"], 0)
        self.assertIs(parsed["has_result"], True)
        self.assertIs(parsed["result_is_error"], False)
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(parsed["usage"], {"input_tokens": 2, "output_tokens": 1})
        self.assertEqual(
            parsed["model_usage"], {"claude-auxiliary-test": {"inputTokens": 1}}
        )
        self.assertEqual(parsed["provider_cost_usd"], 0.0125)
        self.assertEqual(parsed["duration_api_ms"], 321.0)
        self.assertIs(parsed["validated_success"], True)

    def test_structured_output_becomes_canonical_response(self) -> None:
        events = _success_events()
        assistant = events[1]
        assert isinstance(assistant["message"], dict)
        assistant["message"]["content"] = [
            {
                "type": "tool_use",
                "id": "structured-output-1",
                "name": "StructuredOutput",
                "input": {"status": "complete", "revision": 1},
            }
        ]
        events[-1]["result"] = ""
        events[-1]["structured_output"] = {"status": "complete", "revision": 1}
        with tempfile.TemporaryDirectory() as temp:
            path = _write_events(Path(temp), events)
            parsed = parse_stream(
                path, "sonnet", SESSION_ID, expect_structured_output=True
            )

        self.assertEqual(parsed["response"], '{"revision":1,"status":"complete"}')
        self.assertEqual(parsed["structured_output"], {"status": "complete", "revision": 1})
        self.assertEqual(parsed["tool_use_count"], 0)
        self.assertEqual(parsed["errors"], [])

    def test_structured_run_still_rejects_real_tool_use(self) -> None:
        events = _success_events()
        assistant = events[1]
        assert isinstance(assistant["message"], dict)
        assistant["message"]["content"] = [
            {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {}}
        ]
        events[-1]["structured_output"] = {"status": "complete", "revision": 1}
        with tempfile.TemporaryDirectory() as temp:
            path = _write_events(Path(temp), events)
            parsed = parse_stream(
                path, "sonnet", SESSION_ID, expect_structured_output=True
            )

        self.assertEqual(parsed["tool_use_count"], 1)
        self.assertTrue(parsed["errors"])

    def test_structured_run_rejects_missing_structured_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = _write_events(Path(temp), _success_events())
            parsed = parse_stream(
                path, "sonnet", SESSION_ID, expect_structured_output=True
            )

        self.assertIn("terminal result missing structured_output object", parsed["errors"][0])
        self.assertIs(parsed["validated_success"], False)

    def test_ignores_init_and_auxiliary_models_as_model_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = _write_events(Path(temp), _success_events())

            parsed = parse_stream(path, "sonnet", SESSION_ID)

        self.assertEqual(parsed["actual_models"], ["claude-sonnet-test"])
        self.assertNotIn("claude-untrusted-init-model", parsed["actual_models"])
        self.assertNotIn("claude-auxiliary-test", parsed["actual_models"])

    def test_rejects_mismatched_assistant_model_family(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = _write_events(Path(temp), _success_events(assistant_model="claude-fable-test"))

            parsed = parse_stream(path, "sonnet", SESSION_ID)

        self.assertTrue(parsed["errors"])

    def test_rejects_synthetic_assistant_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = _write_events(Path(temp), _success_events(assistant_model="<synthetic>"))

            parsed = parse_stream(path, "sonnet", SESSION_ID)

        self.assertTrue(parsed["errors"])
        self.assertEqual(parsed["actual_models"], [])

    def test_rejects_assistant_event_after_terminal_result(self) -> None:
        events = _success_events()
        events.append(events[1])
        with tempfile.TemporaryDirectory() as temp:
            path = _write_events(Path(temp), events)

            parsed = parse_stream(path, "sonnet", SESSION_ID)

        self.assertTrue(parsed["errors"])

    def test_rejects_any_observed_root_session_mismatch(self) -> None:
        mismatched = str(uuid.uuid4())
        events = _success_events()
        events[0]["session_id"] = mismatched
        with tempfile.TemporaryDirectory() as temp:
            path = _write_events(Path(temp), events)

            parsed = parse_stream(path, "sonnet", SESSION_ID)

        self.assertIn(mismatched, parsed["observed_session_ids"])
        self.assertTrue(parsed["errors"])

    def test_rejects_tool_use_content(self) -> None:
        events = _success_events()
        assistant = events[1]
        assert isinstance(assistant["message"], dict)
        assistant["message"]["content"] = [
            {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {}}
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = _write_events(Path(temp), events)

            parsed = parse_stream(path, "sonnet", SESSION_ID)

        self.assertEqual(parsed["tool_use_count"], 1)
        self.assertTrue(parsed["errors"])

    def test_rejects_missing_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = _write_events(Path(temp), _success_events()[:-1])

            parsed = parse_stream(path, "sonnet", SESSION_ID)

        self.assertIs(parsed["has_result"], False)
        self.assertTrue(parsed["errors"])
        self.assertIs(parsed["validated_success"], False)

    def test_rejects_missing_or_null_terminal_result_text(self) -> None:
        for result_value in (None, "missing"):
            with self.subTest(result_value=result_value):
                events = _success_events()
                if result_value == "missing":
                    del events[-1]["result"]
                else:
                    events[-1]["result"] = None
                with tempfile.TemporaryDirectory() as temp:
                    path = _write_events(Path(temp), events)

                    parsed = parse_stream(path, "sonnet", SESSION_ID)

                self.assertTrue(parsed["errors"])
                self.assertIs(parsed["validated_success"], False)

    def test_rejects_missing_session_id_on_assistant_or_result(self) -> None:
        for event_index in (1, 2):
            with self.subTest(event_index=event_index):
                events = _success_events()
                del events[event_index]["session_id"]
                with tempfile.TemporaryDirectory() as temp:
                    path = _write_events(Path(temp), events)

                    parsed = parse_stream(path, "sonnet", SESSION_ID)

                self.assertTrue(parsed["errors"])
                self.assertIs(parsed["validated_success"], False)

    def test_rejects_error_terminal_result(self) -> None:
        events = _success_events()
        events[-1]["is_error"] = True
        events[-1]["subtype"] = "error"
        with tempfile.TemporaryDirectory() as temp:
            path = _write_events(Path(temp), events)

            parsed = parse_stream(path, "sonnet", SESSION_ID)

        self.assertIs(parsed["result_is_error"], True)
        self.assertTrue(parsed["errors"])

    def test_rejects_is_error_true_even_when_subtype_claims_success(self) -> None:
        events = _success_events()
        events[-1]["is_error"] = True
        with tempfile.TemporaryDirectory() as temp:
            path = _write_events(Path(temp), events)

            parsed = parse_stream(path, "sonnet", SESSION_ID)

        self.assertIs(parsed["result_is_error"], True)
        self.assertTrue(parsed["errors"])

    def test_non_boolean_is_error_fails_closed(self) -> None:
        events = _success_events()
        events[-1]["is_error"] = "false"
        with tempfile.TemporaryDirectory() as temp:
            path = _write_events(Path(temp), events)

            parsed = parse_stream(path, "sonnet", SESSION_ID)

        self.assertTrue(parsed["errors"])

    def test_malformed_json_fails_closed_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            path.write_text('{"type":"system"}\n{broken\n', encoding="utf-8")

            parsed = parse_stream(path, "sonnet", SESSION_ID)

        self.assertTrue(parsed["errors"])
        self.assertIs(parsed["has_result"], False)

    def test_stream_larger_than_limit_raises_bounded_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            with path.open("wb") as stream:
                stream.seek(16 * 1024 * 1024)
                stream.write(b"x")

            with self.assertRaises(ValueError):
                parse_stream(path, "sonnet", SESSION_ID)


if __name__ == "__main__":
    unittest.main()
