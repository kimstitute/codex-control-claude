import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "claude-control" / "scripts"))

from claude_control import local_detail  # noqa: E402


def write_jsonl(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


class LocalDetailTests(unittest.TestCase):
    def test_claude_catalog_and_stream_preserve_prompt_tool_and_markers_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "project" / "session-a.jsonl"
            write_jsonl(
                transcript,
                [
                    {
                        "type": "user",
                        "sessionId": "session-a",
                        "cwd": "/work/project",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "message": {"content": "local sentinel prompt"},
                    },
                    {
                        "type": "assistant",
                        "sessionId": "session-a",
                        "timestamp": "2026-01-01T00:00:01Z",
                        "message": {
                            "model": "claude-test",
                            "content": [
                                {"type": "text", "text": "local response"},
                                {
                                    "type": "tool_use",
                                    "id": "call-1",
                                    "name": "Read",
                                    "input": {"description": "inspect source"},
                                },
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "session-a",
                        "timestamp": "2026-01-01T00:00:02Z",
                        "message": {
                            "content": [
                                {"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}
                            ]
                        },
                    },
                ],
            )
            catalog = local_detail.catalog(source="claude", claude_root=root)
            self.assertEqual(catalog["protocol"], local_detail.PROTOCOL)
            self.assertEqual(len(catalog["sessions"]), 1)
            self.assertNotIn("local response", json.dumps(catalog))

            class Store:
                pass

            values = list(
                local_detail.stream(
                    Store(), source=catalog["sessions"][0]["selector"], claude_root=root
                )
            )
            detail = values[-1]
            self.assertEqual(detail["type"], local_detail.DETAIL_TYPE)
            self.assertEqual(detail["source"]["last_prompt"], "local sentinel prompt")
            self.assertEqual(detail["agents"][0]["reasoning"], "local response")
            self.assertEqual(
                [event.get("text") for event in detail["events"] if event["kind"] == "prompt"],
                ["local sentinel prompt"],
            )
            self.assertEqual(detail["tools"][0]["state"], "ok")
            self.assertEqual(
                [event["metadata"]["claude-control"]["cursor"] for event in values[:-1]],
                list(range(1, len(values))),
            )

    def test_codex_payload_envelopes_are_parsed_without_encrypted_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "rollout.jsonl"
            write_jsonl(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "payload": {
                            "id": "thread-a",
                            "cwd": "/work/project",
                            "agent_role": "reviewer",
                        },
                    },
                    {
                        "type": "turn_context",
                        "timestamp": "2026-01-01T00:00:01Z",
                        "payload": {"model": "gpt-test", "approval_policy": "never"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": "2026-01-01T00:00:02Z",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "codex prompt"}],
                            "encrypted_content": "must-not-appear",
                        },
                    },
                    {
                        "type": "response_item",
                        "timestamp": "2026-01-01T00:00:03Z",
                        "payload": {
                            "type": "function_call",
                            "call_id": "tool-a",
                            "name": "exec_command",
                        },
                    },
                    {
                        "type": "response_item",
                        "timestamp": "2026-01-01T00:00:04Z",
                        "payload": {"type": "function_call_output", "call_id": "tool-a"},
                    },
                ],
            )
            catalog = local_detail.catalog(source="codex", codex_root=root)

            class Store:
                pass

            detail = list(
                local_detail.stream(
                    Store(), source=catalog["sessions"][0]["selector"], codex_root=root
                )
            )[-1]
            encoded = json.dumps(detail)
            self.assertEqual(detail["agents"][0]["model"], "gpt-test")
            self.assertEqual(detail["agents"][0]["role"], "reviewer")
            self.assertEqual(detail["tools"][0]["state"], "ok")
            self.assertNotIn("must-not-appear", encoded)

    def test_claude_subagents_are_grouped_with_parent_edges_and_exact_tool_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            main = project / "root-session.jsonl"
            child = project / "root-session" / "subagents" / "agent-child.jsonl"
            write_jsonl(
                main,
                [
                    {
                        "type": "assistant",
                        "sessionId": "root-session",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "message": {"model": "claude-root", "content": "root response"},
                    }
                ],
            )
            write_jsonl(
                child,
                [
                    {
                        "type": "user",
                        "sessionId": "root-session",
                        "agentId": "child-agent",
                        "isSidechain": True,
                        "timestamp": "2026-01-01T00:00:01Z",
                        "message": {"content": "child prompt"},
                    },
                    {
                        "type": "assistant",
                        "sessionId": "root-session",
                        "agentId": "child-agent",
                        "timestamp": "2026-01-01T00:00:02Z",
                        "message": {
                            "model": "claude-child",
                            "content": [
                                {"type": "tool_use", "id": "a", "name": "Read"},
                                {"type": "tool_use", "id": "b", "name": "Write"},
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "root-session",
                        "agentId": "child-agent",
                        "timestamp": "2026-01-01T00:00:03Z",
                        "message": {"content": [{"type": "tool_result", "tool_use_id": "b"}]},
                    },
                    {
                        "type": "user",
                        "sessionId": "root-session",
                        "agentId": "child-agent",
                        "timestamp": "2026-01-01T00:00:04Z",
                        "message": {"content": "ordinary follow-up"},
                    },
                    {
                        "type": "user",
                        "sessionId": "root-session",
                        "agentId": "child-agent",
                        "timestamp": "2026-01-01T00:00:05Z",
                        "message": {"content": [{"type": "tool_result", "tool_use_id": "a"}]},
                    },
                ],
            )
            catalog = local_detail.catalog(source="claude", claude_root=root)
            selected = next(
                item for item in catalog["sessions"] if item["session_id"] == "root-session"
            )

            class Store:
                pass

            values = list(
                local_detail.stream(Store(), source=selected["selector"], claude_root=root)
            )
            baseline, detail = values[0], values[-1]
            self.assertEqual(
                {agent["agent_id"] for agent in detail["agents"]}, {"root-session", "child-agent"}
            )
            child_agent = next(
                agent for agent in detail["agents"] if agent["agent_id"] == "child-agent"
            )
            self.assertEqual(child_agent["parent_key"], "session:root-session")
            self.assertEqual(baseline["value"]["payload"]["nodes"], {})
            child_created = next(
                event
                for event in values[:-1]
                if event["value"]["kind"] == "node_created"
                and event["value"]["entity_id"] == "child-agent"
            )
            parent_edge = next(
                event for event in values[:-1] if event["value"]["kind"] == "edge_created"
            )
            self.assertEqual(child_created["timestamp"], 1767225601.0)
            self.assertEqual(parent_edge["timestamp"], child_created["timestamp"])
            terminal_states = [
                event["value"]["payload"]["state"]
                for event in values[:-1]
                if event["value"]["kind"] == "node_state"
            ]
            self.assertNotIn("completed", terminal_states)
            self.assertIn("idle", terminal_states)
            self.assertEqual([tool["state"] for tool in detail["tools"]], ["ok", "ok"])
            self.assertEqual(
                [event["summary"] for event in detail["events"] if event["kind"] == "tool_end"],
                ["Write", "Read"],
            )

    def test_codex_current_envelopes_preserve_parent_and_semantic_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            rows = [
                {
                    "type": "session_meta",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "payload": {"id": "child", "parent_thread_id": "parent"},
                },
                {
                    "type": "session_meta",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "payload": {"id": "child"},
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T00:00:02Z",
                    "payload": {
                        "type": "agent_message",
                        "author": "assistant",
                        "recipient": "user",
                        "content": [{"type": "output_text", "text": "visible answer"}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T00:00:03Z",
                    "payload": {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "visible reasoning"}],
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-01-01T00:00:04Z",
                    "payload": {"type": "task_started"},
                },
            ]
            write_jsonl(path, rows)
            parsed = local_detail._parse_codex(
                path, rows, {"malformed": 0, "truncated": False, "read_error": None}
            )
            self.assertEqual(parsed["agents"][0]["parent_key"], "session:parent")
            self.assertEqual(
                [event.get("text") for event in parsed["events"]],
                ["visible answer", "visible reasoning"],
            )
            self.assertNotIn("spawn", [event["kind"] for event in parsed["events"]])

    def test_large_file_keeps_head_identity_and_latest_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout-large.jsonl"
            rows = [
                {
                    "type": "session_meta",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "payload": {"id": "stable-thread"},
                }
            ]
            rows.extend(
                {
                    "type": "event_msg",
                    "timestamp": f"2026-01-01T00:00:{index:02d}Z",
                    "payload": {"type": "noise", "padding": "x" * 80},
                }
                for index in range(1, 20)
            )
            rows.append(
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T00:01:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "latest tail prompt"}],
                    },
                }
            )
            write_jsonl(path, rows)
            with (
                mock.patch.object(local_detail, "MAX_BYTES", 512),
                mock.patch.object(local_detail, "MAX_HEAD_BYTES", 160),
                mock.patch.object(local_detail, "MAX_RECORDS", 3),
            ):
                catalog = local_detail.catalog(source="codex", codex_root=root)
                self.assertEqual(catalog["sessions"][0]["session_id"], "stable-thread")
                self.assertEqual(catalog["sessions"][0]["title"], "latest tail prompt")
                self.assertTrue(catalog["stats"]["truncated"])

    def test_read_failures_are_reported_as_partial_coverage(self):
        rows, coverage = local_detail._records(Path("/definitely/missing/transcript.jsonl"))
        self.assertEqual(rows, [])
        self.assertTrue(coverage["read_error"])

        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "missing-run"

            class Store:
                def get_run(self, run_id):
                    return {
                        "id": run_id,
                        "role": "editor",
                        "model": "opus",
                        "created": 1.0,
                    }

                def run_dir(self, run_id):
                    return run_dir

            with mock.patch.object(local_detail, "_managed_events", return_value=[]):
                detail = list(local_detail.stream(Store(), source="managed:run-a"))[-1]
            self.assertTrue(detail["source"]["partial"])
            self.assertIn("managed prompt", detail["source"]["read_error"])
            self.assertIn("managed event stream", detail["source"]["read_error"])

    def test_file_count_cap_is_reported_as_truncated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(2):
                write_jsonl(
                    root / f"session-{index}.jsonl",
                    [
                        {
                            "type": "user",
                            "sessionId": f"session-{index}",
                            "timestamp": f"2026-01-01T00:00:0{index}Z",
                            "message": {"content": f"prompt {index}"},
                        }
                    ],
                )
            with mock.patch.object(local_detail, "MAX_FILES", 1):
                catalog = local_detail.catalog(source="claude", claude_root=root)

                class Store:
                    pass

                detail = list(
                    local_detail.stream(
                        Store(), source=catalog["sessions"][0]["selector"], claude_root=root
                    )
                )[-1]
            self.assertTrue(catalog["stats"]["truncated"])
            self.assertTrue(detail["source"]["truncated"])


if __name__ == "__main__":
    unittest.main()
