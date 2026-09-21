"""Fail before launch on unsupported CLI settings, preserving execution identity."""

import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))
from claude_control import execution_settings, runner
from claude_control.store import ControlError, Store, write_json
from test_controller import FAKE, ControllerTestCase


class EffortCapabilityTests(ControllerTestCase):
    def backend(self, supported_calls):
        binary = self.root / "capability-fixture.py"
        binary.write_text(
            "#!" + sys.executable + "\n"
            "import sys, pathlib, runpy\n"
            "p = pathlib.Path(__file__)\n"
            "if '--version' in sys.argv:\n"
            " print('fixture-1'); sys.exit(0)\n"
            "if '--help' in sys.argv:\n"
            " counter = p.with_suffix('.count')\n"
            " n = int(counter.read_text()) + 1 if counter.exists() else 1\n"
            " counter.write_text(str(n))\n"
            f" print('--effort' if n <= {supported_calls} else '--model'); sys.exit(0)\n"
            "p.with_suffix('.launched').write_text('unexpected model invocation')\n"
            f"runpy.run_path({str(FAKE)!r}, run_name='__main__')\n"
        )
        binary.chmod(0o700)
        config = json.loads((self.state / "config.json").read_text())
        write_json(self.state / "config.json", {**config, "claude_bin": str(binary)})
        return Store(self.state), binary

    def options(self):
        return dict(
            prompt='{"text":"OK"}',
            request_id="effort-probe",
            timeout=5,
            name="effort-probe",
            model="sonnet",
            role="executor",
            project=str(self.project),
            effort="medium",
        )

    def test_unsupported_admission_creates_no_run_or_session(self):
        store, binary = self.backend(0)
        with self.assertRaises(ControlError) as raised:
            store.reserve(**self.options())
        self.assertEqual(raised.exception.code, "effort_unsupported")
        self.assertEqual(store.list_all()["runs"], [])
        self.assertEqual(store.list_all()["sessions"], [])
        self.assertFalse(binary.with_suffix(".launched").exists())

    def test_worker_rechecks_capability_and_releases_slot(self):
        self.reject_after_admission(1)

    def test_raw_replay_does_not_depend_on_current_cli_capability(self):
        store, binary = self.backend(1)
        run_id, _ = store.reserve(**self.options())
        # The next help probe would fail. Replay must retrieve the original reservation.
        reopened = Store(self.state)
        self.assertEqual(reopened.reserve(**self.options()), (run_id, False))
        with self.assertRaises(ControlError) as raised:
            reopened.reserve(**{**self.options(), "prompt": "different"})
        self.assertEqual(raised.exception.code, "request_conflict")
        self.assertEqual(binary.with_suffix(".count").read_text(), "1")

    def test_exec_wrapper_rejects_changed_binary_and_releases_slot(self):
        store, binary = self.backend(2)
        run_id, _ = store.reserve(**self.options())
        popen = runner.subprocess.Popen

        def changed_backend(*args, **kwargs):
            if "_exec" in args[0]:
                binary.write_text(binary.read_text() + "\n# replaced before exec\n")
            return popen(*args, **kwargs)

        with (
            mock.patch.object(runner.signal, "signal"),
            mock.patch.object(runner.subprocess, "Popen", side_effect=changed_backend),
        ):
            runner.run_worker(self.state, run_id)
        result = store.get_run(run_id)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "effort_unsupported")
        self.assertIsNone(result["child_pid"])
        self.assertFalse(binary.with_suffix(".launched").exists())

    def test_one_second_run_does_not_probe_after_worker_spawn(self):
        store, binary = self.backend(2)
        run_id, _ = store.reserve(**{**self.options(), "timeout": 1})
        runner.launch_worker(store, run_id)
        result = self.cli("wait", "--run", run_id, "--seconds", "8")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["effort"], "medium")
        self.assertEqual(binary.with_suffix(".count").read_text(), "2")

    def reject_after_admission(self, supported_calls):
        store, binary = self.backend(supported_calls)
        run_id, created = store.reserve(**self.options())
        self.assertTrue(created)
        runner.launch_worker(store, run_id)
        result = self.cli("wait", "--run", run_id, "--seconds", "8")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "effort_unsupported")
        self.assertIsNone(result["child_pid"])
        self.assertFalse(binary.with_suffix(".launched").exists())
        self.assertEqual(store.session(result["session_id"])["blocked"], 0)
        self.assertFalse(
            any(
                row["status"] in ("pending", "claimed", "launching", "running", "unknown")
                for row in store.list_all()["runs"]
            )
        )

    def test_cached_help_is_scoped_to_binary_and_reported_version(self):
        execution_settings._help_support.cache_clear()
        calls = []
        version = ["fixture-1"]

        def call(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(
                returncode=0, stdout=version[0] if argv[-1] == "--version" else "--effort"
            )

        with mock.patch.object(execution_settings.subprocess, "run", side_effect=call):
            for _ in range(2):
                self.assertTrue(execution_settings.probe_effort(str(FAKE), self.state)[1])
            self.assertEqual(sum(argv[-1] == "--help" for argv in calls), 1)
            version[0] = "fixture-2"
            self.assertTrue(execution_settings.probe_effort(str(FAKE), self.state)[1])
            self.assertEqual(sum(argv[-1] == "--help" for argv in calls), 2)
            self.assertTrue(execution_settings.probe_effort(str(FAKE), self.state, fresh=True)[1])
            self.assertEqual(sum(argv[-1] == "--help" for argv in calls), 3)
        execution_settings._help_support.cache_clear()
