"""Read-only monitor snapshot and graph rendering tests."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import monitor  # noqa: E402
from claude_control.store import Store  # noqa: E402
from test_controller import ControllerTestCase  # noqa: E402


class MonitorTests(ControllerTestCase):
    def test_snapshot_reports_agent_history_and_provider_totals(self) -> None:
        started = self.start("monitor-sonnet", {"text": "observed"}, "monitor-run")
        finished = self.wait_terminal(started["id"])
        self.assertEqual(finished["status"], "completed")

        data = monitor.snapshot(Store(self.state), history=10)

        self.assertEqual(data["protocol"], monitor.PROTOCOL)
        self.assertEqual(data["summary"]["agents"], 1)
        self.assertEqual(data["summary"]["active_agents"], 0)
        self.assertEqual(data["summary"]["telemetry"]["input_tokens"], 4)
        self.assertEqual(data["summary"]["telemetry"]["output_tokens"], 2)
        self.assertEqual(data["summary"]["telemetry"]["provider_cost_usd"], 0.001)
        self.assertEqual(data["agents"][0]["requested_model"], "sonnet")
        self.assertEqual(data["agents"][0]["actual_model"], "claude-sonnet-test")
        self.assertEqual(data["runs"][0]["telemetry"]["duration_api_ms"], 25.0)
        self.assertTrue(any(node["kind"] == "agent" for node in data["nodes"]))

    def test_cli_snapshot_is_json_and_tui_requires_terminal(self) -> None:
        data = self.cli("monitor", "snapshot", "--history", "5", "--no-live")
        rejected = self.cli("monitor", "tui", expected=2)

        self.assertEqual(data["protocol"], monitor.PROTOCOL)
        self.assertEqual(rejected["error"], "monitor_terminal")

    def test_live_stream_uses_estimate_until_terminal_result(self) -> None:
        session = "00000000-0000-4000-8000-000000000001"
        events = [
            {
                "type": "system",
                "subtype": "thinking_tokens",
                "session_id": session,
                "estimated_tokens": 250,
                "estimated_tokens_delta": 50,
            },
            {
                "type": "assistant",
                "session_id": session,
                "message": {
                    "usage": {
                        "input_tokens": 3,
                        "cache_creation_input_tokens": 7,
                        "cache_read_input_tokens": 11,
                        "output_tokens": 13,
                    }
                },
            },
        ]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n{")
            observed = monitor._live_usage(path)

        self.assertEqual(observed["estimated_output_tokens"], 250)
        self.assertEqual(observed["usage"]["output_tokens"], 13)
        self.assertFalse(observed["exact"])

    def test_graph_renderer_preserves_labeled_agent_relationships(self) -> None:
        data = {
            "nodes": [
                {"id": "workflow:w", "kind": "workflow", "label": "plan", "state": "active"},
                {"id": "task:t", "kind": "task", "label": "implement", "state": "running"},
                {
                    "id": "agent:a",
                    "kind": "agent",
                    "label": "sonnet-1",
                    "state": "running",
                    "role": "executor",
                    "model": "claude-sonnet-test",
                    "work": "implement",
                    "estimated_output_tokens": 1250,
                },
            ],
            "edges": [
                {"from": "workflow:w", "to": "task:t", "label": "worker"},
                {"from": "task:t", "to": "agent:a", "label": "session"},
            ],
        }

        rendered = "\n".join(monitor.graph_lines(data, active_only=True))

        self.assertIn("worker → [task] implement", rendered)
        self.assertIn("session → [agent] sonnet-1", rendered)
        self.assertIn("claude-sonnet-test", rendered)
        self.assertIn("~1.2k tok", rendered)
