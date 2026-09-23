"""OTLP GenAI/OpenInference export from the public observation replay shape."""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import observation_otel  # noqa: E402


def snapshot():
    secret = "DO-NOT-EXPORT-4217"
    return {
        "contract": "claude-control.observation.v1",
        "version": 1,
        "cursor": 42,
        "fidelity": "complete",
        "nodes": {
            "session": {
                "session-a": {
                    "node": "session",
                    "id": "session-a",
                    "name": "editor",
                    "role": "executor",
                    "model": "opus[1m]",
                    "effort": "high",
                }
            },
            "run": {
                "run-active": {
                    "node": "run",
                    "id": "run-active",
                    "session_id": "session-a",
                    "state": "running",
                    "started": 5.0,
                    "reason": secret,
                },
                "run-failed": {
                    "node": "run",
                    "id": "run-failed",
                    "session_id": "session-a",
                    "state": "failed",
                    "started": 3.0,
                    "finished": 4.5,
                    "models": ["claude-opus-5"],
                    "reason": secret,
                    "path": f"/private/{secret}",
                },
                "run-ok": {
                    "node": "run",
                    "id": "run-ok",
                    "session_id": "session-a",
                    "state": "completed",
                    "effort": "high",
                    "started": 1.0,
                    "finished": 2.0,
                    "models": ["claude-opus-5"],
                    "result": secret,
                },
                "run-unfinished": {
                    "node": "run",
                    "id": "run-unfinished",
                    "session_id": "session-a",
                    "state": "completed",
                    "started": 6.0,
                },
            },
        },
        "edges": {},
        "telemetry": {
            "run-ok": {
                "node": "run_telemetry",
                "run_id": "run-ok",
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 3,
                    "cache_read_input_tokens": 4,
                    "output_tokens": 5,
                    "output_tokens_details": {"thinking_tokens": 2},
                },
                "provider_cost_usd": 0.25,
                "duration_ms": 1000.0,
                "message": secret,
            }
        },
    }


def attributes(span):
    values = {}
    for item in span["attributes"]:
        value = next(iter(item["value"].values()))
        values[item["key"]] = value
    return values


class ObservationOtelTests(unittest.TestCase):
    def test_builds_deterministic_otlp_request_and_reports_omissions(self):
        first = observation_otel.build_export(snapshot())
        second = observation_otel.build_export(snapshot())
        self.assertEqual(first, second)

        resource_spans = first["document"]["resourceSpans"]
        self.assertEqual(len(resource_spans), 1)
        scope_spans = resource_spans[0]["scopeSpans"]
        self.assertEqual(scope_spans[0]["scope"]["name"], "claude-control.observation")
        spans = scope_spans[0]["spans"]
        self.assertEqual([span["name"] for span in spans], ["invoke_agent executor"] * 2)
        self.assertEqual(first["metadata"]["exported_run_count"], 2)
        self.assertEqual(first["metadata"]["omitted_run_count"], 2)
        self.assertEqual(
            [item["cause"] for item in first["metadata"]["omitted_runs"]],
            ["non_terminal", "missing_timestamps"],
        )

    def test_span_ids_timestamps_status_and_semantic_attributes(self):
        spans = observation_otel.build_export(snapshot())["document"]["resourceSpans"][0][
            "scopeSpans"
        ][0]["spans"]
        failed, success = spans
        self.assertRegex(success["traceId"], re.compile(r"^[0-9a-f]{32}$"))
        self.assertRegex(success["spanId"], re.compile(r"^[0-9a-f]{16}$"))
        self.assertEqual(success["startTimeUnixNano"], "1000000000")
        self.assertEqual(success["endTimeUnixNano"], "2000000000")
        self.assertEqual(success["kind"], "SPAN_KIND_INTERNAL")
        self.assertEqual(success["status"], {"code": "STATUS_CODE_OK"})
        self.assertEqual(failed["status"], {"code": "STATUS_CODE_ERROR"})

        success_attrs = attributes(success)
        failed_attrs = attributes(failed)
        self.assertEqual(success_attrs["gen_ai.operation.name"], "invoke_agent")
        self.assertEqual(success_attrs["gen_ai.provider.name"], "anthropic")
        self.assertEqual(success_attrs["gen_ai.request.model"], "opus[1m]")
        self.assertEqual(success_attrs["gen_ai.response.model"], "claude-opus-5")
        self.assertEqual(success_attrs["openinference.span.kind"], "AGENT")
        self.assertEqual(success_attrs["llm.token_count.prompt"], "17")
        self.assertEqual(success_attrs["llm.token_count.completion"], "5")
        self.assertEqual(success_attrs["llm.token_count.total"], "22")
        self.assertEqual(success_attrs["llm.token_count.reasoning"], "2")
        self.assertEqual(success_attrs["llm.cost.total"], 0.25)
        self.assertEqual(failed_attrs["error.type"], "claude_control.run.failed")

    def test_serialized_export_contains_no_content_or_private_fields(self):
        rendered = json.dumps(observation_otel.build_export(snapshot()), sort_keys=True)
        self.assertNotIn("DO-NOT-EXPORT-4217", rendered)
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
            "gen_ai.input.messages",
            "gen_ai.output.messages",
            "input.value",
            "output.value",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_document_helper_returns_only_standard_request_shape(self):
        document = observation_otel.otlp_document(snapshot())
        self.assertEqual(set(document), {"resourceSpans"})


if __name__ == "__main__":
    unittest.main()
