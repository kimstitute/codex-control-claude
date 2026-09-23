"""AG-UI adapter tests over sanitized observation event and replay shapes."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import observation_agui  # noqa: E402

SECRET = "DO-NOT-EXPORT-AGUI-8731"


def decoded_events(state="completed"):
    return [
        {
            "cursor": 2,
            "contract": "claude-control.observation.v1",
            "kind": "node_created",
            "entity_kind": "run",
            "entity_id": "run-a",
            "recorded_at": 1.0,
            "effective_at": 1.0,
            "payload": {
                "node": "run",
                "id": "run-a",
                "session_id": "session-a",
                "state": "pending",
                "models": ["claude-opus-5"],
                "prompt": SECRET,
            },
        },
        {
            "cursor": 3,
            "contract": "claude-control.observation.v1",
            "kind": "node_state",
            "entity_kind": "run",
            "entity_id": "run-a",
            "recorded_at": 2.0,
            "effective_at": 2.0,
            "payload": {
                "node": "run",
                "id": "run-a",
                "session_id": "session-a",
                "state": state,
                "models": ["claude-opus-5"],
                "reason": SECRET,
                "result": SECRET,
            },
        },
        {
            "cursor": 4,
            "contract": "claude-control.observation.v1",
            "kind": "run_telemetry",
            "entity_kind": "run",
            "entity_id": "run-a",
            "recorded_at": 2.1,
            "effective_at": 2.1,
            "payload": {
                "node": "run_telemetry",
                "run_id": "run-a",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 4,
                    "cache_creation_input_tokens": 3,
                    "output_tokens": 5,
                    "output_tokens_details": {"thinking_tokens": 2},
                },
                "model_usage": {
                    "claude-opus-5": {"inputTokens": 10, "outputTokens": 5}
                },
                "provider_cost_usd": 0.25,
                "message": SECRET,
            },
        },
        {
            "cursor": 5,
            "contract": "claude-control.observation.v1",
            "kind": "edge_created",
            "entity_kind": "task_run",
            "entity_id": "task-a:1>run-a",
            "recorded_at": 2.2,
            "effective_at": 2.2,
            "payload": {"edge": "task_run", "from_id": "task-a", "to_id": "run-a"},
        },
    ]


class ObservationAguiTests(unittest.TestCase):
    def test_state_snapshot_uses_standard_shape_and_sanitizes_recursively(self):
        snapshot = {
            "contract": "claude-control.observation.v1",
            "cursor": 5,
            "fidelity": "complete",
            "nodes": {"run": {"run-a": {"id": "run-a", "reason": SECRET}}},
            "edges": {},
            "telemetry": {"run-a": {"usage": {"input_tokens": 1}, "path": SECRET}},
            "prompt": SECRET,
        }
        event = observation_agui.state_snapshot(snapshot)

        self.assertEqual(event["type"], "STATE_SNAPSHOT")
        self.assertEqual(event["snapshot"]["cursor"], 5)
        self.assertEqual(event["snapshot"]["adapter_profile"], observation_agui.PROFILE)
        self.assertNotIn(SECRET, json.dumps(event, sort_keys=True))

    def test_run_lifecycle_and_token_usage_follow_agui_accounting(self):
        converted = observation_agui.events(decoded_events())
        started, finished, telemetry, relation = converted

        self.assertEqual(
            (started["type"], started["threadId"], started["runId"]),
            ("RUN_STARTED", "session-a", "run-a"),
        )
        self.assertNotIn("input", started)
        self.assertEqual(finished["type"], "RUN_FINISHED")
        self.assertNotIn("result", finished)
        self.assertEqual(
            finished["usage"],
            [
                {
                    "provider": "anthropic",
                    "model": "claude-opus-5",
                    "inputTokens": 17,
                    "outputTokens": 5,
                    "totalTokens": 22,
                    "reasoningTokens": 2,
                    "cachedInputTokens": 4,
                    "cacheWriteInputTokens": 3,
                }
            ],
        )
        self.assertEqual((telemetry["type"], relation["type"]), ("CUSTOM", "CUSTOM"))
        self.assertEqual(relation["name"], observation_agui.CUSTOM_NAME)

    def test_failed_run_uses_generic_error_and_base_metadata_for_ids(self):
        converted = observation_agui.events(decoded_events("failed"))
        error = converted[1]

        self.assertEqual(error["type"], "RUN_ERROR")
        self.assertEqual(error["message"], "Agent run ended with state failed.")
        self.assertEqual(error["code"], "failed")
        self.assertNotIn("threadId", error)
        self.assertNotIn("runId", error)
        self.assertEqual(error["metadata"]["claude-control"]["runId"], "run-a")

    def test_serialized_events_never_expose_content_fields(self):
        rendered = json.dumps(observation_agui.events(decoded_events()), sort_keys=True)
        self.assertNotIn(SECRET, rendered)
        for forbidden in (
            '"prompt"',
            '"result"',
            '"message"',
            '"policy"',
            '"path"',
            '"reason"',
            '"receipt"',
            '"account"',
            '"credential"',
        ):
            self.assertNotIn(forbidden, rendered)

    def test_malformed_event_fails_without_fabricating_lifecycle(self):
        malformed = decoded_events()[:1]
        malformed[0]["payload"] = {"node": "run", "id": "run-a", "state": "pending"}
        with self.assertRaisesRegex(ValueError, "session id"):
            observation_agui.events(malformed)


if __name__ == "__main__":
    unittest.main()
