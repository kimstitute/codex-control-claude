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
    def test_claude_usage_is_exactly_aggregated_and_duplicate_messages_are_ignored(self):
        rows = [
            {
                "type": "assistant",
                "sessionId": "usage-session",
                "uuid": "row-a",
                "requestId": "request-a",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "id": "message-a",
                    "model": "claude-opus-5",
                    "content": "first",
                    "usage": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 11,
                        "cache_read_input_tokens": 13,
                        "output_tokens": 17,
                        "output_tokens_details": {"thinking_tokens": 5},
                    },
                },
            },
            {
                "type": "assistant",
                "sessionId": "usage-session",
                "uuid": "row-a-copy",
                "requestId": "request-a",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "id": "message-a",
                    "model": "claude-opus-5",
                    "content": "duplicate",
                    "usage": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 11,
                        "cache_read_input_tokens": 13,
                        "output_tokens": 17,
                        "output_tokens_details": {"thinking_tokens": 5},
                    },
                },
            },
            {
                "type": "assistant",
                "sessionId": "usage-session",
                "uuid": "row-b",
                "requestId": "request-b",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "id": "message-b",
                    "model": "claude-opus-5",
                    "content": "second",
                    "usage": {
                        "input_tokens": 3,
                        "cache_creation_input_tokens": 19,
                        "cache_read_input_tokens": 23,
                        "output_tokens": 29,
                        "output_tokens_details": {"thinking_tokens": 7},
                    },
                },
            },
        ]

        parsed = local_detail._parse_claude(
            Path("usage-session.jsonl"),
            rows,
            {"malformed": 0, "truncated": False, "read_error": None},
        )

        local_usage = parsed["agents"][0]["local_usage"]
        self.assertEqual(
            local_usage["totals"],
            {
                "input_tokens": 5,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 36,
                "output_tokens": 46,
                "thinking_tokens": 12,
            },
        )
        self.assertTrue(local_usage["complete"])
        self.assertEqual(local_usage["responses"], 2)
        self.assertEqual(local_usage["cost"], {"status": "unavailable", "usd": None})
        graph = local_detail._local_events(parsed)
        created = next(
            event for event in graph if event.get("value", {}).get("kind") == "node_created"
        )
        self.assertEqual(
            created["value"]["payload"]["telemetry"],
            local_detail._usage_telemetry(local_usage),
        )

    def test_claude_usage_tracks_streaming_growth_and_marks_unkeyed_rows_incomplete(self):
        def row(output, thinking, *, request="request", message="message"):
            return {
                "type": "assistant",
                "sessionId": "usage-session",
                "requestId": request,
                "message": {
                    "id": message,
                    "model": "claude-opus-5",
                    "content": "chunk",
                    "usage": {
                        "input_tokens": 1,
                        "cache_creation_input_tokens": 2,
                        "cache_read_input_tokens": 3,
                        "output_tokens": output,
                        "output_tokens_details": {"thinking_tokens": thinking},
                    },
                },
            }

        rows = [row(4, 1), row(9, 5), row(100, 50, request="")]
        usage = local_detail._claude_usage(
            rows, {"malformed": 0, "truncated": False, "read_error": None}
        )

        self.assertEqual(usage["responses"], 1)
        self.assertEqual(usage["totals"]["output_tokens"], 9)
        self.assertEqual(usage["totals"]["thinking_tokens"], 5)
        self.assertEqual(usage["skipped"]["unkeyed"], 1)
        self.assertFalse(usage["complete"])

    def test_claude_terminal_metadata_is_catalog_safe_and_detail_local_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_id = "11111111-1111-4111-8111-111111111111"
            transcript = root / "project" / f"{session_id}.jsonl"
            write_jsonl(
                transcript,
                [
                    {
                        "type": "assistant",
                        "sessionId": session_id,
                        "timestamp": "2026-01-01T00:00:00Z",
                        "message": {"model": "claude-opus-5", "content": "answer"},
                    }
                ],
            )
            terminal = {
                "id": "11111111",
                "session_id": session_id,
                "kind": "background",
                "status": "idle",
                "attachable": True,
            }
            with (
                mock.patch.object(
                    local_detail.terminal_cli,
                    "catalog",
                    return_value={"terminals": [terminal]},
                ),
                mock.patch.object(
                    local_detail.terminal_cli,
                    "snapshot",
                    return_value={**terminal, "recent_output": "local terminal sentinel"},
                ),
            ):
                catalog = local_detail.catalog(store=object(), source="claude", claude_root=root)
                self.assertTrue(catalog["sessions"][0]["terminal_attachable"])
                self.assertNotIn("local terminal sentinel", json.dumps(catalog))
                detail = list(
                    local_detail.stream(
                        object(),
                        source=catalog["sessions"][0]["selector"],
                        claude_root=root,
                    )
                )[-1]
            self.assertEqual(
                detail["source"]["terminal"]["recent_output"], "local terminal sentinel"
            )

    def test_idle_terminal_without_transcript_remains_selectable(self):
        with tempfile.TemporaryDirectory() as directory:
            session_id = "22222222-2222-4222-8222-222222222222"
            terminal = {
                "id": "22222222",
                "session_id": session_id,
                "name": "idle terminal",
                "cwd": "/work/project",
                "kind": "background",
                "status": "idle",
                "attachable": True,
                "role": "executor",
                "requested_model": "opus",
                "safe_profile": True,
                "started_at": 1.0,
            }
            with (
                mock.patch.object(
                    local_detail.terminal_cli,
                    "catalog",
                    return_value={"terminals": [terminal]},
                ),
                mock.patch.object(
                    local_detail.terminal_cli,
                    "snapshot",
                    return_value={**terminal, "recent_output": "waiting for input"},
                ),
            ):
                catalog = local_detail.catalog(
                    store=object(), source="claude", claude_root=Path(directory)
                )
                self.assertEqual(catalog["sessions"][0]["selector"], f"terminal:{session_id}")
                events = list(
                    local_detail.stream(
                        object(),
                        source=f"terminal:{session_id}",
                        claude_root=Path(directory),
                    )
                )
                detail = events[-1]
            self.assertEqual(detail["agents"][0]["role"], "executor")
            self.assertEqual(detail["source"]["terminal"]["recent_output"], "waiting for input")
            created = next(
                event for event in events if event.get("value", {}).get("kind") == "node_created"
            )
            self.assertEqual(created["value"]["payload"]["state"], "idle")

    def test_following_terminal_promotes_to_transcript_without_restarting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_id = "2a2a2a2a-2222-4222-8222-222222222222"
            terminal = {
                "id": "2a2a2a2a",
                "session_id": session_id,
                "name": "promoted terminal",
                "cwd": "/work/project",
                "kind": "background",
                "status": "idle",
                "attachable": True,
                "role": "executor",
                "requested_model": "opus",
                "safe_profile": True,
                "started_at": 1.0,
            }
            with mock.patch.object(
                local_detail.terminal_cli,
                "snapshot",
                return_value={**terminal, "recent_output": "ready"},
            ):
                events = local_detail.stream(
                    object(),
                    source=f"terminal:{session_id}",
                    claude_root=root,
                    follow=True,
                    poll_seconds=0.05,
                )
                initial = []
                while not initial or initial[-1].get("type") != local_detail.DETAIL_TYPE:
                    initial.append(next(events))
                self.assertEqual(initial[-1]["source"]["selector"], f"terminal:{session_id}")

                write_jsonl(
                    root / "project" / f"{session_id}.jsonl",
                    [
                        {
                            "type": "user",
                            "sessionId": session_id,
                            "timestamp": "2026-01-01T00:00:00Z",
                            "message": {"content": "new prompt"},
                        },
                        {
                            "type": "assistant",
                            "sessionId": session_id,
                            "timestamp": "2026-01-01T00:00:01Z",
                            "message": {
                                "id": "promoted-message",
                                "model": "claude-opus-5",
                                "content": "new response",
                                "usage": {"input_tokens": 2, "output_tokens": 3},
                            },
                        },
                    ],
                )
                promoted = []
                for _ in range(12):
                    value = next(events)
                    promoted.append(value)
                    if value.get("type") == local_detail.DETAIL_TYPE:
                        break
                events.close()

            reset = next(
                value for value in promoted if value.get("type") == "CLAUDE_CONTROL_DETAIL_RESET"
            )
            detail = next(
                value for value in promoted if value.get("type") == local_detail.DETAIL_TYPE
            )
            self.assertEqual(reset["reason"], "source_promoted")
            self.assertTrue(detail["source"]["selector"].startswith(f"claude:{session_id}@"))
            self.assertEqual(detail["agents"][0]["agent_id"], session_id)
            self.assertEqual([event["kind"] for event in detail["events"]], ["prompt", "response"])

    def test_stopped_terminal_with_recent_transcript_is_not_live(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_id = "33333333-3333-4333-8333-333333333333"
            transcript = root / "project" / f"{session_id}.jsonl"
            write_jsonl(
                transcript,
                [
                    {
                        "type": "assistant",
                        "sessionId": session_id,
                        "timestamp": "2026-01-01T00:00:00Z",
                        "message": {"model": "claude-opus-5", "content": "done"},
                    }
                ],
            )
            terminal = {
                "id": "33333333",
                "session_id": session_id,
                "kind": "background",
                "status": "stopped",
                "attachable": False,
            }
            with mock.patch.object(
                local_detail.terminal_cli,
                "catalog",
                return_value={"terminals": [terminal]},
            ):
                result = local_detail.catalog(store=object(), source="claude", claude_root=root)
            self.assertFalse(result["sessions"][0]["live"])

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
