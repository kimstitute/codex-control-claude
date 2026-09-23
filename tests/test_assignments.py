"""End-to-end contract tests for structured Claude assignments."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import orchestration
from claude_control.assignments import CONTRACT, MAX_BYTES, ROLE_PRESETS, render_assignment
from claude_control.model_settings import CONTRACT as MODEL_SETTINGS_CONTRACT
from claude_control.model_settings import ROLES
from claude_control.store import ControlError, Store
from test_controller import FAKE, ControllerTestCase


class AssignmentCommandTests(ControllerTestCase):
    """Exercise assignment behavior through the public controller commands."""

    def assignment(self, *, task_id="task-a", name="assignment-a", role="executor", **changes):
        payload = {
            "id": task_id,
            "name": name,
            "role": role,
            "project": str(self.project),
            "objective": "Produce a bounded response.",
            "context": "",
            "scope": ["Use only the assignment text."],
            "acceptance_criteria": ["Return the required report object."],
            "deliverable": "A concise result.",
        }
        payload.update(changes)
        return payload

    def assignment_file(self, payload: object) -> Path:
        path = self.root / f"assignment-{len(list(self.root.glob('assignment-*')))}.json"
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def delegate(self, payload: object, request_id: str, *, expected: int | None = 0) -> dict:
        return self.cli(
            "delegate",
            "--assignment-file",
            str(self.assignment_file(payload)),
            "--request-id",
            request_id,
            expected=expected,
        )

    def completed_assignment(self, payload: dict, request_id: str) -> dict:
        created = self.delegate(payload, request_id)
        completed = self.wait_terminal(created["id"])
        self.assertEqual(completed["status"], "completed")
        return created

    def fixture_context(self, *, response: str | None = None, behavior: str | None = None) -> str:
        fixture = {}
        if response is not None:
            fixture["response"] = response
        if behavior is not None:
            fixture["behavior"] = behavior
        return json.dumps({"__fake_assignment__": fixture})

    def valid_report(self, task_id: str, role: str, **changes: object) -> dict:
        report = {
            "task_id": task_id,
            "role": role,
            "status": "complete",
            "summary": "Fixture summary.",
            "deliverable": "Fixture deliverable.",
            "evidence": [
                {"claim": "Provided input", "basis": "supplied_context", "reference": "context"}
            ],
            "limitations": [],
            "handoff": "",
        }
        report.update(changes)
        return report

    def test_roles_defaults_and_explicit_override_are_visible_in_runs(self) -> None:
        roles = self.cli("roles")
        advertised = {role["name"]: role for role in roles["roles"]}
        self.assertEqual(roles["protocol"], CONTRACT)
        self.assertEqual(set(advertised), set(ROLE_PRESETS))
        self.assertEqual(advertised["executor"]["model"], "sonnet")
        self.assertEqual(advertised["critic"]["model"], "fable")

        for role, expected_model, override in (
            ("executor", "sonnet", None),
            ("critic", "fable", None),
            ("executor", "fable", "fable"),
        ):
            payload = self.assignment(
                task_id=f"models-{role}-{override or 'default'}",
                name=f"models-{role}-{override or 'default'}",
                role=role,
                **({"model": override} if override else {}),
            )
            created = self.completed_assignment(payload, payload["id"])
            session = Store(self.state).session(created["session_id"])
            self.assertEqual(session["model"], expected_model)
            self.assertEqual(session["role"], role)

    def test_role_model_settings_apply_to_new_work_and_explicit_assignment_wins(self) -> None:
        configured = {
            "contract": MODEL_SETTINGS_CONTRACT,
            "roles": {role: {"model": "claude-opus-5", "effort": "high"} for role in ROLES},
        }
        configured["roles"]["researcher"] = {"model": "claude-haiku-4-5-20251001"}
        path = self.root / "role-models.json"
        path.write_text(json.dumps(configured), encoding="utf-8")

        result = self.cli("models", "configure", "--file", str(path))
        self.assertEqual(
            (result["source"], result["roles"]["executor"]),
            (
                "configured",
                {"model": "claude-opus-5", "effort": "high"},
            ),
        )
        self.assertEqual(self.cli("models", "show"), result)

        defaulted = self.completed_assignment(
            self.assignment(task_id="configured-default", name="configured-default"),
            "configured-default",
        )
        defaulted_session = Store(self.state).session(defaulted["session_id"])
        self.assertEqual(
            (defaulted_session["model"], defaulted_session["effort"]),
            ("claude-opus-5", "high"),
        )

        overridden = self.completed_assignment(
            self.assignment(
                task_id="configured-override",
                name="configured-override",
                model="haiku",
                effort="low",
            ),
            "configured-override",
        )
        overridden_session = Store(self.state).session(overridden["session_id"])
        self.assertEqual(
            (overridden_session["model"], overridden_session["effort"]), ("haiku", "low")
        )

        reset = self.cli("models", "reset")
        self.assertEqual(
            (reset["source"], reset["roles"]["executor"]),
            (
                "built_in",
                {"model": "sonnet"},
            ),
        )

    def test_delegate_sends_canonical_role_prompt_on_stdin_and_preserves_locked_argv(self) -> None:
        payload = self.assignment(task_id="stdin-envelope", name="stdin-envelope")
        created = self.completed_assignment(payload, "stdin-envelope-request")

        fixture_path = self.project / ".fake-claude" / f"{created['backend_id']}.invocation.json"
        invocation = json.loads(fixture_path.read_text(encoding="utf-8"))
        prompt = invocation["prompt"]
        self.assertEqual(prompt["protocol"], CONTRACT)
        self.assertEqual(prompt["assignment"]["id"], "stdin-envelope")
        self.assertEqual(prompt["role"]["instructions"], ROLE_PRESETS["executor"]["instructions"])

        recorded = json.loads(
            (self.state / "runs" / created["id"] / "invocation.json").read_text(encoding="utf-8")
        )["argv"]
        self.assertEqual(recorded[0], str(Path(FAKE).resolve()))
        self.assertEqual(recorded[1:3], ["--setting-sources", ""])
        self.assertEqual(recorded[3:5], ["--model", "sonnet"])
        self.assertIn("--safe-mode", recorded)
        self.assertIn("--strict-mcp-config", recorded)
        self.assertIn("--disable-slash-commands", recorded)
        self.assertEqual(recorded[recorded.index("--mcp-config") + 1], '{"mcpServers":{}}')
        self.assertEqual(recorded[recorded.index("--tools") + 1], "")
        self.assertEqual(recorded[recorded.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--permission-prompts", recorded)
        self.assertEqual(recorded[recorded.index("--session-id") + 1], created["backend_id"])
        self.assertEqual(recorded[-4:], ["--output-format", "stream-json", "--verbose", "-p"])

    def test_assignment_parser_rejects_duplicate_unknown_nonfinite_and_null_fields(self) -> None:
        payload = self.assignment()
        duplicate = '{"id":"other-id",' + json.dumps(payload)[1:]
        invalids = [
            duplicate,
            self.assignment(extra="forbidden"),
            self.assignment(timeout=math.nan),
            self.assignment(timeout=True),
            self.assignment(timeout=None),
            self.assignment(model=None),
            self.assignment(scope="not-a-list"),
        ]
        for index, invalid in enumerate(invalids):
            with self.subTest(index=index):
                rejected = self.delegate(invalid, f"invalid-{index}", expected=2)
                self.assertIn(rejected["error"], {"invalid_json", "invalid_assignment"})

        oversized = self.assignment(context="x" * MAX_BYTES)
        rejected = self.delegate(oversized, "invalid-oversized", expected=2)
        self.assertEqual(rejected["error"], "invalid_assignment")

    def test_request_id_dedup_conflicts_when_assignment_intent_changes(self) -> None:
        initial = self.assignment(task_id="dedup-task", name="dedup-session", timeout=31)
        first = self.delegate(initial, "dedup-request")
        repeat = self.delegate(initial, "dedup-request")
        self.assertEqual(repeat["id"], first["id"])
        self.assertTrue(repeat["deduplicated"])

        for changed in (
            self.assignment(task_id="dedup-task", name="dedup-session", timeout=32),
            self.assignment(task_id="dedup-task", name="dedup-session", context="changed"),
        ):
            conflict = self.delegate(changed, "dedup-request", expected=2)
            self.assertEqual(conflict["error"], "request_conflict")
        self.wait_terminal(first["id"])
        inspected = self.cli("report", "--run", first["id"])
        self.assertEqual(inspected["contract_status"], "supported")
        self.assertEqual(inspected["format_status"], "valid")

    def test_changed_role_preset_changes_reservation_fingerprint(self) -> None:
        assignment = self.assignment(task_id="preset-task", name="preset-session")
        store = Store(self.state)
        prompt = render_assignment({**assignment, "model": "sonnet", "timeout": 300.0})
        run_id, created = store.reserve(
            prompt=prompt,
            request_id="preset-request",
            timeout=300.0,
            name=assignment["name"],
            model="sonnet",
            role="executor",
            project=str(self.project),
        )
        self.assertTrue(created)
        with mock.patch.dict(ROLE_PRESETS["executor"], {"instructions": "changed"}):
            changed = render_assignment({**assignment, "model": "sonnet", "timeout": 300.0})
            with self.assertRaisesRegex(ControlError, "different input"):
                store.reserve(
                    prompt=changed,
                    request_id="preset-request",
                    timeout=300.0,
                    name=assignment["name"],
                    model="sonnet",
                    role="executor",
                    project=str(self.project),
                )
        self.cli("stop", "--run", run_id)

    def test_report_verifies_snapshot_and_is_independent_of_current_catalog(self) -> None:
        payload = self.assignment(task_id="history-task", name="history-session")
        created = self.completed_assignment(payload, "history-request")
        valid = self.cli("report", "--run", created["id"])
        self.assertEqual((valid["contract_status"], valid["format_status"]), ("supported", "valid"))
        self.assertEqual(valid["agent_status"], "complete")
        self.assertEqual(valid["acceptance"], "unreviewed")

        with mock.patch.dict(ROLE_PRESETS["executor"], {"instructions": "future catalog"}):
            historical = orchestration.report(Store(self.state), created["id"])
        self.assertEqual(
            (historical["contract_status"], historical["format_status"]), ("supported", "valid")
        )

        prompt = self.state / "runs" / created["id"] / "prompt.txt"
        tampered_snapshot = json.loads(prompt.read_text(encoding="utf-8"))
        tampered_snapshot["assignment"]["objective"] = "Changed after reservation."
        prompt.write_text(json.dumps(tampered_snapshot, separators=(",", ":")), encoding="utf-8")
        tampered = self.cli("report", "--run", created["id"])
        self.assertEqual(tampered["contract_status"], "invalid")
        self.assertEqual(tampered["format_status"], "unavailable")

    def test_report_contract_rejects_wrong_identity_blocked_empty_evidence_and_trailing_prose(
        self,
    ) -> None:
        cases = {
            "malformed": "{not-json",
            "wrong-task": self.valid_report("other-task", "executor"),
            "wrong-role": self.valid_report("wrong-role", "critic"),
            "blocked-empty-limitations": self.valid_report(
                "blocked-empty-limitations", "executor", status="blocked", limitations=[]
            ),
            "empty-evidence": self.valid_report(
                "empty-evidence", "executor", evidence=[], limitations=[]
            ),
            "trailing": json.dumps(self.valid_report("trailing", "executor")) + "\ntrailing prose",
        }
        for name, report in cases.items():
            response = report if isinstance(report, str) else json.dumps(report)
            payload = self.assignment(
                task_id=name,
                name=f"report-{name}",
                context=self.fixture_context(response=response),
            )
            created = self.completed_assignment(payload, f"report-{name}")
            inspected = self.cli("report", "--run", created["id"])
            self.assertEqual(inspected["contract_status"], "supported")
            self.assertEqual(inspected["format_status"], "invalid")

        fenced = self.valid_report("fenced", "executor")
        payload = self.assignment(
            task_id="fenced",
            name="report-fenced",
            context=self.fixture_context(response=f"```json\n{json.dumps(fenced)}\n```"),
        )
        created = self.completed_assignment(payload, "report-fenced")
        self.assertEqual(self.cli("report", "--run", created["id"])["format_status"], "valid")

    def test_ordinary_start_and_followup_reports_are_unsupported(self) -> None:
        started = self.start("ordinary-report", {"text": "first"}, "ordinary-start")
        self.assertEqual(self.wait_terminal(started["id"])["status"], "completed")
        first_report = self.cli("report", "--run", started["id"])
        self.assertEqual(first_report["contract_status"], "unsupported")

        followed = self.followup(started["session_id"], {"text": "second"}, "ordinary-follow")
        self.assertEqual(self.wait_terminal(followed["id"])["status"], "completed")
        followup_report = self.cli("report", "--run", followed["id"])
        self.assertEqual(followup_report["contract_status"], "unsupported")

    def test_restart_and_followup_do_not_reinterpret_structured_prompts(self) -> None:
        created = self.completed_assignment(
            self.assignment(task_id="original-contract", name="original-contract"),
            "original-contract",
        )
        original_prompt = self.state / "runs" / created["id"] / "prompt.txt"
        for command in ("restart", "followup"):
            arguments = [
                command,
                "--session",
                created["session_id"],
                "--prompt-file",
                str(original_prompt),
                "--request-id",
                "structured-looking-" + command,
                "--timeout",
                "10",
            ]
            if command == "restart":
                arguments.append("--acknowledge-context")
            continued = self.cli(*arguments)
            self.assertEqual(self.wait_terminal(continued["id"])["status"], "completed")
            inspected = self.cli("report", "--run", continued["id"])
            self.assertEqual(inspected["contract_status"], "unsupported")
            (self.state / "runs" / continued["id"] / "prompt.txt").unlink()
            missing = self.cli("report", "--run", continued["id"])
            self.assertEqual(missing["contract_status"], "unsupported")
        original = self.cli("report", "--run", created["id"])
        self.assertEqual(original["format_status"], "valid")

    def test_pending_and_blocked_reports_are_not_acceptance(self) -> None:
        sleeping = self.delegate(
            self.assignment(context=self.fixture_context(behavior="sleep")), "pending-report"
        )
        pending = self.cli("report", "--run", sleeping["id"])
        self.assertEqual(pending["format_status"], "pending")
        self.assertIsNone(pending["agent_status"])
        self.cli("stop", "--run", sleeping["id"])
        self.wait_terminal(sleeping["id"])
        blocked = self.valid_report(
            "blocked-report", "critic", status="blocked", evidence=[], limitations=["Missing data."]
        )
        created = self.completed_assignment(
            self.assignment(
                task_id="blocked-report",
                name="blocked-report",
                role="critic",
                context=self.fixture_context(response=json.dumps(blocked)),
            ),
            "blocked-report",
        )
        inspected = self.cli("report", "--run", created["id"])
        self.assertEqual(inspected["execution_status"], "completed")
        self.assertEqual(inspected["format_status"], "valid")
        self.assertEqual(inspected["agent_status"], "blocked")
        self.assertEqual(inspected["acceptance"], "unreviewed")

    def test_observe_reports_terminal_deadline_and_attention_without_retry(self) -> None:
        first = self.completed_assignment(
            self.assignment(task_id="observe-a", name="observe-a"), "observe-a"
        )
        second = self.completed_assignment(
            self.assignment(task_id="observe-b", name="observe-b"), "observe-b"
        )
        terminal = self.cli(
            "observe", "--run", first["id"], "--run", second["id"], "--seconds", "0"
        )
        self.assertTrue(terminal["execution_done"])
        self.assertEqual(terminal["counts"], {"completed": 2})
        self.assertEqual(terminal["wait_reason"], "terminal")

        sleeping = self.delegate(
            self.assignment(
                task_id="observe-deadline",
                name="observe-deadline",
                context=self.fixture_context(behavior="sleep"),
            ),
            "observe-deadline",
        )
        deadline = self.cli("observe", "--run", sleeping["id"], "--seconds", "0")
        self.assertFalse(deadline["execution_done"])
        self.assertEqual(deadline["wait_reason"], "deadline")
        self.cli("stop", "--run", sleeping["id"])
        self.wait_terminal(sleeping["id"])

        failed = self.delegate(
            self.assignment(
                task_id="observe-failed",
                name="observe-failed",
                context=self.fixture_context(behavior="exit_fail"),
            ),
            "observe-failed",
        )
        self.assertEqual(self.wait_terminal(failed["id"])["status"], "failed")
        before = len(self.cli("list")["runs"])
        attention = self.cli("observe", "--run", failed["id"], "--seconds", "1")
        self.assertTrue(attention["execution_done"])
        self.assertEqual(attention["needs_attention"], [failed["id"]])
        self.assertEqual(len(self.cli("list")["runs"]), before)

        with Store(self.state).db(write=True) as db:
            db.execute(
                "UPDATE runs SET status='unknown',reason='test_unknown' WHERE id=?", (second["id"],)
            )
        unknown = self.cli("observe", "--run", second["id"], "--seconds", "1")
        self.assertFalse(unknown["execution_done"])
        self.assertEqual(unknown["needs_attention"], [second["id"]])
        self.assertEqual(unknown["runs"][0]["status"], "unknown")
