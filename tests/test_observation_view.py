"""Pure projection tests for the cursor-based observation replay viewer."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import observation_view  # noqa: E402


def snapshot():
    return {
        "nodes": {
            "composition": {},
            "workflow": {},
            "workspace": {},
            "task": {
                "task-a": {
                    "id": "task-a",
                    "name": "implement viewer",
                    "session_id": "session-a",
                    "active_run_id": "run-a",
                }
            },
            "run": {
                "run-a": {
                    "id": "run-a",
                    "session_id": "session-a",
                    "state": "running",
                    "created": 2.0,
                    "models": ["claude-opus-5"],
                }
            },
            "session": {
                "session-a": {
                    "id": "session-a",
                    "name": "editor",
                    "role": "executor",
                    "model": "opus[1m]",
                    "blocked": 0,
                }
            },
        },
        "edges": {
            "task_run": {
                "task-a:1>run-a": {
                    "edge": "task_run",
                    "from_kind": "task",
                    "from_id": "task-a",
                    "to_kind": "run",
                    "to_id": "run-a",
                }
            }
        },
    }


class ObservationViewTests(unittest.TestCase):
    def test_graph_preserves_relationships_and_derives_agent_ownership(self):
        projected = observation_view.graph(snapshot())
        nodes = {node["id"]: node for node in projected["nodes"]}
        edges = {(edge["from"], edge["to"], edge["label"]) for edge in projected["edges"]}

        self.assertEqual(nodes["agent:session-a"]["state"], "running")
        self.assertEqual(nodes["agent:session-a"]["role"], "executor")
        self.assertEqual(nodes["run:run-a"]["model"], "claude-opus-5")
        self.assertIn(("task:task-a", "run:run-a", "task_run"), edges)
        self.assertIn(("run:run-a", "agent:session-a", "session"), edges)
        self.assertIn(("task:task-a", "agent:session-a", "assigned"), edges)

    def test_timeline_minimap_marks_start_middle_and_latest(self):
        self.assertEqual(observation_view.timeline_bar(1, 9, 5), "[●────]")
        self.assertEqual(observation_view.timeline_bar(5, 9, 5), "[──●──]")
        self.assertEqual(observation_view.timeline_bar(9, 9, 5), "[────●]")

    def test_invalid_graph_shape_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "node graph"):
            observation_view.graph({})


if __name__ == "__main__":
    unittest.main()
