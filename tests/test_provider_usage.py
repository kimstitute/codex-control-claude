"""Provider quota collectors never expose credentials and preserve reported units."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import monitor, provider_usage  # noqa: E402


class ProviderUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        provider_usage._CACHE.clear()

    def test_claude_reads_cached_windows_without_account_identity(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / ".claude").mkdir()
            (home / ".claude/.credentials.json").write_text('{"oauth":"secret"}')
            (home / ".claude.json").write_text(
                json.dumps(
                    {
                        "oauthAccount": {"email": "private@example.test"},
                        "cachedUsageUtilization": {
                            "accountUuid": "private-id",
                            "fetchedAtMs": time.time() * 1000,
                            "utilization": {
                                "limits": [
                                    {
                                        "kind": "weekly_scoped",
                                        "percent": 25,
                                        "resets_at": "2030-01-01T00:00:00Z",
                                        "scope": {"model": {"display_name": "Fable"}},
                                    }
                                ]
                            },
                        },
                    }
                )
            )
            with (
                mock.patch.dict(os.environ, {"HOME": root}, clear=False),
                mock.patch(
                    "claude_control.provider_usage.shutil.which", return_value="/bin/claude"
                ),
            ):
                result = provider_usage._claude(1)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["windows"][0]["used_percent"], 25)
        self.assertEqual(result["windows"][0]["remaining_percent"], 75)
        self.assertEqual(result["windows"][0]["model"], "Fable")
        self.assertNotIn("private", json.dumps(result))

    def test_gemini_preserves_request_unit_and_exact_amounts(self) -> None:
        future = (time.time() + 3600) * 1000
        responses = [
            {"cloudaicompanionProject": {"id": "hidden-project"}},
            {
                "buckets": [
                    {
                        "modelId": "gemini-test",
                        "tokenType": "REQUESTS",
                        "remainingFraction": 0.75,
                        "remainingAmount": "750",
                        "resetTime": "2030-01-01T00:00:00Z",
                    }
                ]
            },
        ]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / ".gemini"
            path.mkdir()
            (path / "oauth_creds.json").write_text(
                json.dumps({"access_token": "top-secret", "expiry_date": future})
            )
            with (
                mock.patch.dict(os.environ, {"HOME": root}, clear=False),
                mock.patch(
                    "claude_control.provider_usage.shutil.which", return_value="/bin/gemini"
                ),
                mock.patch(
                    "claude_control.provider_usage._post_json", side_effect=responses
                ) as post,
            ):
                result = provider_usage._gemini(1)

        window = result["windows"][0]
        self.assertEqual((window["used"], window["remaining"], window["limit"]), (250, 750, 1000))
        self.assertEqual(window["unit"], "requests")
        self.assertNotIn("top-secret", json.dumps(result))
        self.assertEqual(post.call_count, 2)

    def test_cursor_uses_reported_percentage_without_inventing_tokens(self) -> None:
        responses = [
            {
                "billingCycleEnd": "2030-01-01T00:00:00Z",
                "planUsage": {"totalPercentUsed": "12.5"},
                "spendLimitUsage": {"individualUsed": "125", "individualLimit": "5000"},
            },
            {"planInfo": {"planName": "Team"}},
        ]
        with (
            mock.patch("claude_control.provider_usage._cursor_auth", return_value="secret"),
            mock.patch("claude_control.provider_usage.shutil.which", return_value="/bin/agent"),
            mock.patch("claude_control.provider_usage._post_json", side_effect=responses),
        ):
            result = provider_usage._cursor(1)

        self.assertEqual(result["windows"][0]["used_percent"], 12.5)
        self.assertEqual(result["windows"][0]["remaining_percent"], 87.5)
        self.assertFalse(result["windows"][0]["exact_amounts"])
        self.assertIsNone(result["tokens"]["remaining"])
        self.assertEqual(result["spending"]["used_cents"], 125)

    def test_codex_reads_all_quota_buckets_and_account_tokens(self) -> None:
        class FakeRpc:
            def __init__(self, _binary, _timeout):
                self.calls = []

            def request(self, identifier, method, params):
                self.calls.append((identifier, method, params))
                if method == "initialize":
                    return {"userAgent": "test"}
                if method == "account/rateLimits/read":
                    return {
                        "rateLimitsByLimitId": {
                            "codex": {
                                "limitId": "codex",
                                "primary": {
                                    "usedPercent": 20,
                                    "windowDurationMins": 300,
                                    "resetsAt": 2_000_000_000,
                                },
                                "secondary": {
                                    "usedPercent": 40,
                                    "windowDurationMins": 10080,
                                    "resetsAt": 2_000_000_100,
                                },
                            },
                            "model-special": {
                                "limitName": "Special Model",
                                "primary": {
                                    "usedPercent": 10,
                                    "windowDurationMins": 300,
                                    "resetsAt": 2_000_000_000,
                                },
                            },
                        }
                    }
                return {
                    "summary": {"lifetimeTokens": 1234, "peakDailyTokens": 400},
                    "dailyUsageBuckets": [{"startDate": "2030-01-01", "tokens": 55}],
                }

            def send(self, value):
                self.calls.append(value)

            def close(self):
                pass

        with (
            mock.patch("claude_control.provider_usage.shutil.which", return_value="/bin/codex"),
            mock.patch("claude_control.provider_usage._CodexRpc", FakeRpc),
        ):
            result = provider_usage._codex(1)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["windows"]), 3)
        self.assertEqual(result["tokens"]["lifetime_used"], 1234)
        self.assertEqual(result["tokens"]["today_used"], 55)
        self.assertIsNone(result["tokens"]["remaining"])

    def test_limit_renderer_distinguishes_percent_from_exact_requests(self) -> None:
        data = {
            "provider_usage": {
                "providers": [
                    {
                        "provider": "gemini",
                        "status": "ok",
                        "stale": False,
                        "windows": [
                            provider_usage._window(
                                "gemini-test",
                                remaining_percent=75,
                                unit="requests",
                                used=250,
                                remaining=750,
                                limit=1000,
                            )
                        ],
                        "tokens": {},
                        "spending": {},
                    }
                ]
            }
        }

        rendered = "\n".join(monitor._limit_lines(data))

        self.assertIn("250 requests / 750 req", rendered)
        self.assertIn("100% means fully remaining", rendered)


if __name__ == "__main__":
    unittest.main()
