"""Linux-runnable tests for the native Windows helper contract."""

import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import runner, windows_process  # noqa: E402
from claude_control.store import Store  # noqa: E402


class FakeProcess:
    def __init__(self, frames):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(b"".join(json.dumps(frame).encode() + b"\n" for frame in frames))
        self.killed = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class HelperResolutionTests(unittest.TestCase):
    def test_explicit_helper_requires_and_verifies_hash(self):
        with tempfile.TemporaryDirectory() as root:
            binary = Path(root) / "helper.exe"
            binary.write_bytes(b"verified helper")
            digest = hashlib.sha256(binary.read_bytes()).hexdigest()
            info = windows_process.resolve_helper(
                {
                    windows_process.HELPER_PATH_ENV: str(binary),
                    windows_process.HELPER_SHA256_ENV: digest,
                },
                machine="AMD64",
            )
            self.assertEqual(info.sha256, digest)
            self.assertEqual(info.target, "x86_64-pc-windows-msvc")
            with self.assertRaises(windows_process.HelperError):
                windows_process.resolve_helper(
                    {
                        windows_process.HELPER_PATH_ENV: str(binary),
                        windows_process.HELPER_SHA256_ENV: "0" * 64,
                    },
                    machine="AMD64",
                )

    def test_bundled_manifest_pins_name_hash_and_size(self):
        with tempfile.TemporaryDirectory() as root:
            binary_dir = Path(root) / "bin"
            binary_dir.mkdir()
            name = "ccc-win-supervisor-aarch64-pc-windows-msvc.exe"
            binary = binary_dir / name
            binary.write_bytes(b"arm helper")
            manifest = {
                "protocol": 1,
                "artifacts": [
                    {
                        "name": name,
                        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                        "size": binary.stat().st_size,
                    }
                ],
            }
            (binary_dir / "windows-helper-manifest.json").write_text(json.dumps(manifest))
            info = windows_process.resolve_helper({}, plugin_root=root, machine="ARM64")
            self.assertEqual(info.path, binary)
            self.assertEqual(info.source, "bundled")


class ProtocolTests(unittest.TestCase):
    def test_hello_create_and_deferred_exit_are_strictly_framed(self):
        process = FakeProcess(
            [
                {"id": 1, "ok": True, "event": "hello", "protocol": 1, "version": "0.1.0"},
                {"event": "exited", "exit_code": 0, "reason": None},
                {
                    "id": 2,
                    "ok": True,
                    "event": "created",
                    "pid": 123,
                    "creation_filetime": "456",
                    "broke_away": True,
                },
            ]
        )
        client = windows_process.HelperClient(process)
        self.assertEqual(client.hello()["version"], "0.1.0")
        created = client.create(
            run_id="12345678-1234-1234-1234-123456789abc",
            job_name="Local\\ccc-test",
            argv=["claude.exe"],
            cwd="C:\\repo",
            env={},
            stdin_path="in",
            stdout_path="out",
            stderr_path="err",
        )
        self.assertEqual(created["pid"], 123)
        self.assertEqual(client.next_event(0.1)["exit_code"], 0)
        sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual([item["op"] for item in sent], ["hello", "create"])

    def test_duplicate_key_and_nonfinite_json_are_rejected(self):
        for raw in (b'{"id":1,"id":2}\n', b'{"value":NaN}\n'):
            with self.subTest(raw=raw), self.assertRaises(windows_process.HelperError):
                windows_process._read_frame(io.BytesIO(raw))

    def test_oversized_frame_is_drained_before_rejection(self):
        stream = io.BytesIO(
            b"x" * (windows_process.MAX_FRAME_BYTES + 8) + b'\n{"ok":true}\n'
        )
        with self.assertRaises(windows_process.HelperError):
            windows_process._read_frame(stream)
        self.assertEqual(windows_process._read_frame(stream), {"ok": True})

    def test_create_transport_loss_is_marked_uncertain(self):
        process = FakeProcess(
            [{"id": 1, "ok": True, "event": "hello", "protocol": 1, "version": "0.1.0"}]
        )
        client = windows_process.HelperClient(process)
        client.hello()
        with self.assertRaises(windows_process.HelperError) as raised:
            client.create(
                run_id="12345678-1234-1234-1234-123456789abc",
                job_name="Local\\ccc-test",
                argv=["claude.exe"],
                cwd="C:\\repo",
                env={},
                stdin_path="in",
                stdout_path="out",
                stderr_path="err",
            )
        self.assertTrue(raised.exception.uncertain)


class WindowsRunOrderingTests(unittest.TestCase):
    def _run(self, client):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        project = root / "project"
        project.mkdir()
        state = root / "state"
        Store.initialize(state, sys.executable, [project])
        store = Store(state)
        run_id, _ = store.reserve(
            prompt="hello",
            request_id="windows-order",
            timeout=30,
            name="windows-order",
            model="sonnet",
            role="executor",
            project=project,
        )
        with store.db(write=True) as db:
            db.execute(
                "UPDATE runs SET status='launching',started=1,platform='windows',"
                "process_backend='windows-job-object' WHERE id=?",
                (run_id,),
            )
        row = store.get_run(run_id)
        (store.run_dir(run_id) / "events.jsonl").touch()
        (store.run_dir(run_id) / "stderr.txt").touch()
        client.store = store
        client.run_id = run_id
        helper = windows_process.HelperInfo(
            path=Path("C:/helper.exe"),
            sha256="a" * 64,
            target="x86_64-pc-windows-msvc",
            source="test",
        )
        process = mock.Mock()
        with (
            mock.patch.object(windows_process, "resolve_helper", return_value=helper),
            mock.patch.object(windows_process.subprocess, "Popen", return_value=process),
            mock.patch.object(windows_process, "HelperClient", return_value=client),
        ):
            result = windows_process.run_windows_child(
                store,
                row,
                str(project),
                ["claude.exe"],
                store.run_dir(run_id),
                {},
                [False],
                1024,
            )
        return store, run_id, result

    def test_child_identity_is_committed_before_resume(self):
        class Client:
            def hello(self):
                return None

            def create(self, **_payload):
                return {"pid": 44, "creation_filetime": "99", "broke_away": True}

            def resume(self):
                row = self.store.get_run(self.run_id)
                assert row["status"] == "launching"
                assert json.loads(row["child_identity_json"])["creation_filetime"] == "99"

            def next_event(self, _timeout):
                return {"event": "exited", "exit_code": 0, "reason": None}

            def close(self):
                pass

        store, run_id, result = self._run(Client())
        row = store.get_run(run_id)
        self.assertEqual(result, {"exit_code": 0, "reason": None, "uncertain": False})
        self.assertEqual(row["status"], "running")
        self.assertEqual((row["child_pid"], row["child_start"]), (44, "99"))

    def test_transport_loss_after_created_identity_is_uncertain_and_not_retried(self):
        class Client:
            create_calls = 0

            def hello(self):
                return None

            def create(self, **_payload):
                self.create_calls += 1
                return {"pid": 55, "creation_filetime": "101", "broke_away": False}

            def resume(self):
                raise windows_process.HelperError("helper_eof", "lost")

            def abort(self):
                raise windows_process.HelperError("helper_eof", "lost")

            def close(self):
                pass

        client = Client()
        store, run_id, result = self._run(client)
        self.assertEqual(client.create_calls, 1)
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["reason"], "helper_eof")
        self.assertEqual(store.get_run(run_id)["child_pid"], 55)

    def test_transport_loss_during_create_is_uncertain_and_not_retried(self):
        class Client:
            create_calls = 0

            def hello(self):
                return None

            def create(self, **_payload):
                self.create_calls += 1
                raise windows_process.HelperError(
                    "helper_eof", "lost create reply", uncertain=True
                )

            def abort(self):
                raise windows_process.HelperError("helper_eof", "lost")

            def close(self):
                pass

        client = Client()
        store, run_id, result = self._run(client)
        self.assertEqual(client.create_calls, 1)
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["reason"], "helper_eof")
        self.assertIsNone(store.get_run(run_id)["child_pid"])


class WindowsWorkerLaunchTests(unittest.TestCase):
    def test_worker_uses_windows_creation_flags(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "project"
            project.mkdir()
            store = Store.initialize(Path(root) / "state", sys.executable, [project])
            store = Store(store["state_dir"])
            run_id, _ = store.reserve(
                prompt="hello",
                request_id="windows-worker-launch",
                timeout=30,
                name="windows-worker-launch",
                model="sonnet",
                role="executor",
                project=project,
            )
            native_path = type(Path("/"))
            with (
                mock.patch.object(runner.os, "name", "nt"),
                mock.patch.object(runner, "Path", native_path),
                mock.patch.object(runner.subprocess, "Popen") as popen,
            ):
                runner.launch_worker(store, run_id)

            self.assertEqual(
                popen.call_args.kwargs["creationflags"],
                0x08000000 | 0x00000200,
            )
            self.assertNotIn("start_new_session", popen.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
