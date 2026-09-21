"""Real namespace boundaries; skipped when the outer host denies Bubblewrap."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))
from claude_control import workspace_sandbox as sandbox


def namespace_alive(namespace):
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if os.readlink(entry / "ns/pid") == namespace:
                    return True
            except OSError:
                pass
    return False


class SandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ready = sandbox.probe()
        if not ready["ready"]:
            raise unittest.SkipTest("Host denies Bubblewrap namespaces: " + str(ready["reason"]))

    def test_no_host_files_credentials_environment_network_or_gpu(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            secret = root / "host-only.txt"
            secret.write_text("synthetic-private-value")
            scratch = root / "scratch"
            scratch.mkdir()
            program = """
import json,os,socket
from pathlib import Path
s=socket.socket(); s.settimeout(.2)
try:
 s.connect(('1.1.1.1',443)); network=True
except OSError: network=False
print(json.dumps(dict(env=dict(os.environ),host_file=Path(%r).exists(),
 gpu=list(map(str,Path('/dev').glob('nvidia*'))),network=network,proc=list(Path('/proc').glob('[0-9]*')).__len__())))
""" % str(secret)
            with patch.dict(os.environ, {"SYNTHETIC_AUTH_TOKEN": "synthetic-private-value"}):
                result = sandbox.execute(scratch, ["/usr/bin/python3", "-c", program], 5)
            self.assertEqual(result["outcome"], "ok", result)
            data = json.loads(result["stdout"])
            self.assertFalse(data["host_file"])
            self.assertFalse(data["network"])
            self.assertEqual(data["gpu"], [])
            self.assertNotIn("SYNTHETIC_AUTH_TOKEN", data["env"])
            self.assertNotIn("HOME", data["env"])
            self.assertLess(data["proc"], 10)

    def test_only_disposable_tree_and_tmp_are_writable(self):
        with tempfile.TemporaryDirectory() as directory:
            result = sandbox.execute(
                directory,
                [
                    "/usr/bin/python3",
                    "-c",
                    "from pathlib import Path; Path('generated').write_text('ok'); Path('/tmp/test').write_text('ok');\ntry: Path('/usr/escape').write_text('bad')\nexcept OSError: print('denied')",
                ],
                5,
            )
            self.assertEqual(result["outcome"], "ok", result)
            self.assertEqual(result["stdout"].strip(), "denied")
            self.assertEqual((Path(directory) / "generated").read_text(), "ok")

    def test_output_and_wall_time_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            output = sandbox.execute(
                directory, ["/usr/bin/python3", "-c", "while True: print('x'*8192, flush=True)"], 5
            )
            self.assertEqual(output["outcome"], "output_limit")
            self.assertLessEqual(
                len(output["stdout"].encode()) + len(output["stderr"].encode()),
                sandbox.OUTPUT_BYTES,
            )
            self.assertTrue(output["truncated"])
            timed = sandbox.execute(
                directory, ["/usr/bin/python3", "-c", "import time; time.sleep(20)"], 1
            )
            self.assertEqual(timed["outcome"], "timeout")
            self.assertLess(timed["duration"], 4)

    def test_timeout_kills_a_descendant_with_a_new_session(self):
        program = "import os,subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'],start_new_session=True); print(os.readlink('/proc/self/ns/pid'),flush=True); time.sleep(20)"
        with tempfile.TemporaryDirectory() as directory:
            result = sandbox.execute(directory, ["/usr/bin/python3", "-c", program], 1)
        self.assertEqual(result["outcome"], "timeout")
        namespace = result["stdout"].strip()
        self.assertTrue(namespace.startswith("pid:["), result)
        deadline = time.monotonic() + 2
        while namespace_alive(namespace) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(namespace_alive(namespace))

    def test_cancellation_kills_the_namespace(self):
        begin = time.monotonic()
        with tempfile.TemporaryDirectory() as directory:
            result = sandbox.execute(
                directory,
                ["/usr/bin/python3", "-c", "import time;time.sleep(20)"],
                5,
                cancelled=lambda: time.monotonic() - begin > 0.2,
            )
        self.assertEqual(result["outcome"], "cancelled")
        self.assertLess(result["duration"], 3)

    def test_coordinator_death_kills_even_detached_children(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            program = "import os,subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'],start_new_session=True); open('namespace','w').write(os.readlink('/proc/self/ns/pid')); time.sleep(20)"
            scripts = str(Path(sandbox.__file__).resolve().parents[1])
            supervisor = (
                "import sys;sys.path.insert(0,%r);from claude_control.workspace_sandbox import execute;execute(%r,%r,10)"
                % (scripts, str(directory), ["/usr/bin/python3", "-c", program])
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", supervisor],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 5
                while not (directory / "namespace").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((directory / "namespace").exists())
                namespace = (directory / "namespace").read_text()
                proc.kill()
                proc.wait(timeout=3)
                deadline = time.monotonic() + 3
                while namespace_alive(namespace) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(namespace_alive(namespace))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=3)
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
