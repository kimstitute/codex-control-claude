"""Linux-runnable tests for the Windows WSL2+Bubblewrap bridge, with fake WSL/supervisor."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import windows_process  # noqa: E402
from claude_control import windows_wsl_sandbox as wsl  # noqa: E402
from claude_control.store import ControlError  # noqa: E402


class FakeHelperClient:
    def __init__(self, exit_code=0, result=None, uncertain=False):
        self.exit_code = exit_code
        self.result = result
        self.uncertain = uncertain
        self.stop_calls = []
        self.create_calls = 0
        self.aborted = False
        self.resumed = False
        self.last_create = None

    def __call__(self, process):
        return self

    def hello(self):
        return {}

    def create(self, **payload):
        self.create_calls += 1
        self.last_create = payload
        if self.uncertain:
            raise windows_process.HelperError("helper_eof", "closed stdout", uncertain=True)
        if self.result is not None:
            Path(payload["stdout_path"]).write_bytes(
                json.dumps(self.result, separators=(",", ":")).encode() + b"\n"
            )
        return {"pid": 4242, "creation_filetime": "133700000000000000"}

    def resume(self):
        self.resumed = True
        return {}

    def stop(self, grace_ms=3000):
        self.stop_calls.append(grace_ms)

    def abort(self):
        self.aborted = True

    def next_event(self, timeout):
        return {"exit_code": self.exit_code}

    def close(self):
        pass


def _helper_info():
    return windows_process.HelperInfo(
        path=Path("/fake/ccc-win-supervisor.exe"), sha256="0" * 64,
        target="x86_64-pc-windows-msvc", source="test",
    )


def _result_body(outcome="ok", stdout="", stderr="", exit_code=0):
    return {
        "outcome": outcome,
        "exit_code": exit_code,
        "duration": 0.1,
        "truncated": False,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
    }


class PackTarTests(unittest.TestCase):
    def test_deterministic_regardless_of_creation_order(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            (Path(a) / "b.txt").write_text("2")
            (Path(a) / "a.txt").write_text("1")
            (Path(b) / "a.txt").write_text("1")
            (Path(b) / "b.txt").write_text("2")
            for path in (Path(a) / "a.txt", Path(b) / "a.txt"):
                path.chmod(0o644)
            for path in (Path(a) / "b.txt", Path(b) / "b.txt"):
                path.chmod(0o644)
            self.assertEqual(wsl._pack_tar(a), wsl._pack_tar(b))

    def test_rejects_unsupported_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.sh"
            path.write_text("#!/bin/sh")
            path.chmod(0o600)
            with self.assertRaises(ControlError) as caught:
                wsl._pack_tar(directory)
            self.assertEqual(caught.exception.code, "workspace_integrity")


class ExecuteDispatchTests(unittest.TestCase):
    def _run(self, client, **kwargs):
        with (
            mock.patch.object(windows_process, "resolve_helper", return_value=_helper_info()),
            mock.patch.object(wsl, "_locate_wsl", return_value="/fake/wsl.exe"),
            mock.patch.object(wsl.subprocess, "Popen", return_value=mock.Mock()),
            mock.patch.object(windows_process, "HelperClient", client),
        ):
            with tempfile.TemporaryDirectory() as directory:
                (Path(directory) / "f.txt").write_text("x")
                Path(directory, "f.txt").chmod(0o644)
                return wsl.execute(directory, ["/usr/bin/true"], 5, **kwargs)

    def test_happy_path_returns_helper_result(self):
        result_body = _result_body(stdout="hi")
        client = FakeHelperClient(exit_code=0, result=result_body)
        result = self._run(client)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["stdout"], "hi")
        self.assertEqual(client.create_calls, 1)

    def test_uncertain_create_yields_unknown_and_is_never_retried(self):
        client = FakeHelperClient(uncertain=True)
        result = self._run(client)
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(client.create_calls, 1)

    def test_cancellation_sends_stop(self):
        result_body = _result_body()
        client = FakeHelperClient(exit_code=0, result=result_body)
        self._run(client, cancelled=lambda: True)
        self.assertGreaterEqual(len(client.stop_calls), 1)

    def test_rejected_outcome_from_helper_is_mapped_to_unknown(self):
        result_body = _result_body(
            outcome="rejected", stderr="digest_mismatch", exit_code=None
        )
        client = FakeHelperClient(exit_code=None, result=result_body)
        result = self._run(client)
        self.assertEqual(result["outcome"], "unknown")

    def test_result_digest_mismatch_is_unknown(self):
        result_body = _result_body(stdout="trusted")
        result_body["stdout_sha256"] = "0" * 64
        result = self._run(FakeHelperClient(result=result_body))
        self.assertEqual(result["outcome"], "unknown")

    def test_provider_environment_is_not_forwarded_to_wsl(self):
        client = FakeHelperClient(result=_result_body())
        with mock.patch.dict("os.environ", {"SYNTHETIC_PROVIDER_TOKEN": "secret"}):
            self._run(client)
        self.assertNotIn("SYNTHETIC_PROVIDER_TOKEN", client.last_create["env"])

    def test_managed_identity_is_durable_before_resume(self):
        class FakeStore:
            recorded = False

            def db(self, write=False):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, _sql, _values):
                self.recorded = True
                return SimpleNamespace(rowcount=1)

        fake_store = FakeStore()

        class OrderingClient(FakeHelperClient):
            def resume(self):
                self.resumed = True
                if not fake_store.recorded:
                    raise AssertionError("workspace command identity was not committed")
                return {}

        client = OrderingClient(result=_result_body())
        with mock.patch("claude_control.store.Store", return_value=fake_store):
            result = self._run(
                client,
                managed={"state_dir": "C:/state", "run_id": "run", "seq": 0},
            )
        self.assertEqual(result["outcome"], "ok")
        self.assertTrue(client.resumed)


class ProbeTests(unittest.TestCase):
    def test_reports_distro_backend_and_reason(self):
        with mock.patch.dict("os.environ", {"CLAUDE_CONTROL_WSL_DISTRO": "Ubuntu"}):
            with mock.patch.object(
                wsl, "execute",
                return_value={"outcome": "ok", "stdout": "", "stderr": "", "exit_code": 0,
                               "duration": 0.1, "truncated": False,
                               "stdout_sha256": "0" * 64, "stderr_sha256": "0" * 64},
            ):
                report = wsl.probe()
            self.assertTrue(report["ready"])
            self.assertEqual(report["backend"], "wsl2-bubblewrap")
            self.assertEqual(report["distro"], "Ubuntu")
            self.assertIsNone(report["reason"])

    def test_windows_probe_creates_and_cleans_empty_mode_metadata(self):
        observed = []

        def execute(directory, _argv, _timeout):
            sidecar = Path(directory).with_name(Path(directory).name + ".modes.json")
            observed.append(sidecar)
            self.assertEqual(json.loads(sidecar.read_text()), {})
            return _result_body()

        with mock.patch.object(wsl.workspace_files, "_ON_WINDOWS", True), mock.patch.object(
            wsl, "execute", side_effect=execute
        ):
            report = wsl.probe()

        self.assertTrue(report["ready"])
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())

    def test_windows_runtime_environment_has_no_case_insensitive_duplicates(self):
        environment = wsl._windows_environment(
            {
                "SystemRoot": r"C:\Windows",
                "SYSTEMROOT": r"C:\Windows",
                "PATH": r"C:\Windows\System32",
            }
        )
        folded = [name.casefold() for name in environment]
        self.assertEqual(len(folded), len(set(folded)))
        self.assertIn("SystemRoot", environment)
        self.assertNotIn("SYSTEMROOT", environment)


class HelperSourceBoundaryTests(unittest.TestCase):
    """The helper source is plain Linux Python and runs unmodified in this test."""

    def _invoke(self, stdin, env):
        return subprocess.run(
            [sys.executable, "-c", wsl.HELPER_SOURCE],
            input=stdin, capture_output=True, env=env, timeout=10,
        )

    def test_rejects_malformed_manifest(self):
        completed = self._invoke(b"not json\n", {"HOME": "/tmp"})
        body = json.loads(completed.stdout.decode())
        self.assertEqual(body["outcome"], "rejected")

    def test_rejects_trailing_transport_bytes(self):
        manifest = json.dumps(
            {
                "argv": ["/usr/bin/true"],
                "timeout": 5,
                "tar_sha256": hashlib.sha256(b"").hexdigest(),
                "tar_size": 0,
                "files": {},
                "output_limit": 1024,
            }
        ).encode() + b"\ntrailing"
        completed = self._invoke(manifest, {})
        body = json.loads(completed.stdout.decode())
        self.assertEqual(body["outcome"], "rejected")
        self.assertEqual(body["stderr"], "trailing_input")


if __name__ == "__main__":
    unittest.main()
