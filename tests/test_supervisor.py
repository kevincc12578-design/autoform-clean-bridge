"""Offline failure-path tests. Never launch the real UI worker or AutoForm."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from af_r13_bridge.evidence import Evidence, hash_file
from af_r13_bridge.supervisor import Supervisor, WorkerError, WriterLease


class FakeProcess:
    pid = 12345
    args = ['fake-worker']

    def __init__(self, failure=None):
        self.failure = failure
        self.returncode = None
        self.killed = False
        self.waited_after_kill = False

    def wait(self, timeout=None):
        if self.killed:
            self.waited_after_kill = True
            self.returncode = -1
            return -1
        if self.failure is not None:
            raise self.failure
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -1


class FakeJob:
    creation_flags = 0

    def __init__(self, evidence, call_id):
        self.closed = False
        self.process = None

    def assign_and_resume(self, process):
        self.process = process

    def refresh(self):
        return [self.process.pid] if self.process and self.process.poll() is None else []

    def terminate_and_confirm(self):
        if self.process and self.process.poll() is None:
            self.process.kill()
        self.closed = True


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        job_patch = patch('af_r13_bridge.supervisor.WindowsWorkerJob', FakeJob)
        job_patch.start()
        self.addCleanup(job_patch.stop)

    def test_timeout_reaps_owned_worker_and_retains_request(self):
        worker = FakeProcess(subprocess.TimeoutExpired("fake", 0.1))
        supervisor = Supervisor(self.directory)
        with patch("af_r13_bridge.supervisor.subprocess.Popen", return_value=worker):
            with self.assertRaises(WorkerError) as raised:
                supervisor.run_worker({"action": "observe"}, timeout=0.1)
        self.assertEqual(raised.exception.code, "worker_timeout")
        self.assertTrue(worker.killed)
        self.assertTrue(worker.waited_after_kill)
        self.assertIsNone(supervisor.active_pid)
        self.assertEqual(len(list(self.directory.glob("*.request.json"))), 1)
        events = [json.loads(line) for line in (self.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(events[-1]["worker_reaped"])

    def test_keyboard_interrupt_reaps_before_propagating(self):
        worker = FakeProcess(KeyboardInterrupt())
        supervisor = Supervisor(self.directory)
        with patch("af_r13_bridge.supervisor.subprocess.Popen", return_value=worker):
            with self.assertRaises(KeyboardInterrupt):
                supervisor.run_worker({"action": "observe"})
        self.assertTrue(worker.waited_after_kill)
        self.assertIsNone(supervisor.active_pid)

    def test_shutdown_rejects_future_workers_without_starting_any(self):
        supervisor = Supervisor(self.directory)
        supervisor.shutdown()
        supervisor.shutdown()
        with patch("af_r13_bridge.supervisor.subprocess.Popen") as start:
            with self.assertRaises(WorkerError) as raised:
                supervisor.run_worker({})
        self.assertEqual(raised.exception.code, "supervisor_closed")
        start.assert_not_called()

    def test_success_logs_exact_command_and_preserves_unicode(self):
        worker = FakeProcess()

        def start(command, **kwargs):
            request = json.loads(Path(command[command.index("--request") + 1]).read_text(encoding="utf-8"))
            response_path = Path(command[command.index("--response") + 1])
            response_path.write_text(json.dumps({"ok": True, "name": request["name"]}), encoding="utf-8")
            self.assertEqual(command[1:4], ["-I", "-m", "af_r13_bridge.adapters.uia_worker"])
            self.assertNotIn("PYTHONPATH", kwargs["env"])
            return worker

        with patch("af_r13_bridge.supervisor.subprocess.Popen", side_effect=start):
            result = Supervisor(self.directory).run_worker({"name": "工程核对"})
        self.assertEqual(result, {"ok": True, "name": "工程核对"})
        self.assertFalse(worker.killed)

    def test_same_pid_lease_rejects_another_writer_then_releases(self):
        with WriterLease(2147000001):
            with self.assertRaises(WorkerError) as raised:
                WriterLease(2147000001).acquire()
            self.assertEqual(raised.exception.code, "writer_busy")
        with WriterLease(2147000001):
            pass

    def test_lease_excludes_a_separate_process_and_releases_for_it(self):
        code = """from af_r13_bridge.supervisor import WriterLease, WorkerError
try:
    with WriterLease(2147000002):
        print('acquired')
except WorkerError as exc:
    print(exc.code)
"""
        command = [sys.executable, "-I", "-c", code]
        with WriterLease(2147000002):
            held = subprocess.run(command, capture_output=True, text=True, timeout=10)
        released = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(held.returncode, 0, held.stderr)
        self.assertEqual(held.stdout.strip(), "writer_busy")
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertEqual(released.stdout.strip(), "acquired")

    def test_failed_reap_retains_worker_and_blocks_new_work(self):
        class Unreapable(FakeProcess):
            def kill(self):
                raise OSError("simulated refusal")

        worker = Unreapable(subprocess.TimeoutExpired("fake", 0.1))
        supervisor = Supervisor(self.directory)
        with patch("af_r13_bridge.supervisor.subprocess.Popen", return_value=worker):
            with self.assertRaises(WorkerError) as raised:
                supervisor.run_worker({}, timeout=0.1)
            self.assertEqual(raised.exception.code, "worker_reap_failed")
            self.assertEqual(supervisor.active_pid, worker.pid)
            with self.assertRaises(WorkerError) as blocked:
                supervisor.run_worker({})
            self.assertEqual(blocked.exception.code, "supervisor_closed")

    def test_snapshot_hashes_source_and_unique_files_do_not_overwrite(self):
        evidence = Evidence(self.directory)
        first = evidence.write_json("阶段", {"x": 1})
        second = evidence.write_json("阶段", {"x": 2})
        self.assertNotEqual(first, second)
        record = evidence.snapshot("readback", first)
        self.assertEqual(record["sha256"], hash_file(first))
        self.assertEqual(json.loads(first.read_text(encoding="utf-8")), {"x": 1})


if __name__ == "__main__":
    unittest.main()
