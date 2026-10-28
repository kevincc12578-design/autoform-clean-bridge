"""Real Windows process-tree containment, using only sleeping Python scripts.

No AutoForm executable, UIA worker, screenshot, mouse or keyboard is invoked.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import psutil

from af_r13_bridge.supervisor import Supervisor, WorkerError


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object integration test')
class WorkerProcessTreeTests(unittest.TestCase):
    def setUp(self):
        retained = os.environ.get('AF_R13_TEST_EVIDENCE_ROOT')
        if retained:
            self.directory = Path(retained).resolve() / f'{self._testMethodName}-{uuid.uuid4().hex}'
            self.directory.mkdir(parents=True, exist_ok=False)
        else:
            self.temp = tempfile.TemporaryDirectory()
            self.addCleanup(self.temp.cleanup)
            self.directory = Path(self.temp.name)

    def members(self):
        journal = self.directory / 'events.jsonl'
        events = [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()]
        return [e for e in events if e['event'] == 'worker_job_member'], events

    def assert_members_gone(self):
        members, events = self.members()
        self.assertGreaterEqual(len(members), 2, 'This venv should exhibit redirector and interpreter processes')
        terminated=[]
        for member in members:
            try:
                current = psutil.Process(member['pid'])
                if current.create_time()==member['create_time']:
                    # Windows can retain the terminated process object while
                    # another handle is open. Query termination, not PID absence.
                    try:
                        exit_code=current.wait(timeout=0)
                    except psutil.TimeoutExpired:
                        self.fail(f'A recorded worker is still executing: {member}')
                    terminated.append({**member,'exit_code':exit_code,'termination_verified':True})
            except psutil.NoSuchProcess:
                pass
        (self.directory/'termination-checks.json').write_text(json.dumps(terminated,indent=2),encoding='utf-8')
        closed = [e for e in events if e['event'] == 'worker_job_closed']
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['active_processes_remaining'], 0)
        return members

    def sleeper_command(self, _request, _response):
        return [sys.executable, '-I', '-c', 'import time; time.sleep(30)']

    def test_real_timeout_reaps_venv_redirector_and_interpreter(self):
        supervisor = Supervisor(self.directory, command_factory=self.sleeper_command)
        with self.assertRaises(WorkerError) as caught:
            supervisor.run_worker({'test': 'sleep-only'}, timeout=.7)
        self.assertEqual(caught.exception.code, 'worker_timeout')
        self.assertIsNone(supervisor.active_pid)
        self.assert_members_gone()
        supervisor.shutdown()

    def test_real_keyboard_interrupt_reaps_tree_before_propagating(self):
        supervisor = Supervisor(self.directory, command_factory=self.sleeper_command)

        def interrupt_after_descendant(process, timeout):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                supervisor._job.refresh()
                if len(supervisor._job.active_pids()) >= 2:
                    raise KeyboardInterrupt()
                time.sleep(.02)
            self.fail('Redirector descendant was not observed')

        with patch.object(supervisor, '_wait_for_worker', side_effect=interrupt_after_descendant):
            with self.assertRaises(KeyboardInterrupt):
                supervisor.run_worker({'test': 'cancel-sleep-only'}, timeout=5)
        self.assertIsNone(supervisor.active_pid)
        self.assert_members_gone()
        supervisor.shutdown()

    def test_normal_root_exit_reaps_a_lingering_descendant(self):
        child_code = 'import time; time.sleep(30)'

        def command(_request, response):
            code = (
                'import subprocess,sys,time; from pathlib import Path; '
                f'subprocess.Popen([sys.executable,"-I","-c",{child_code!r}]); '
                'time.sleep(.25); '
                f'Path({str(response)!r}).write_text(\'{{"ok":true}}\',encoding="utf-8")'
            )
            return [sys.executable, '-I', '-c', code]

        supervisor = Supervisor(self.directory, command_factory=command)
        result = supervisor.run_worker({'test': 'normal-root-with-sleeping-child'}, timeout=5)
        self.assertEqual(result, {'ok': True})
        members = self.assert_members_gone()
        self.assertGreaterEqual(len(members), 3)
        supervisor.shutdown()

    def test_job_does_not_contain_unrelated_process(self):
        unrelated = subprocess.Popen(self.sleeper_command(None, None), creationflags=subprocess.CREATE_NO_WINDOW)
        unrelated_identity = psutil.Process(unrelated.pid).create_time()
        try:
            supervisor = Supervisor(self.directory, command_factory=self.sleeper_command)
            with self.assertRaises(WorkerError):
                supervisor.run_worker({'test': 'separate-process'}, timeout=.5)
            self.assert_members_gone()
            self.assertIsNone(unrelated.poll())
            self.assertEqual(psutil.Process(unrelated.pid).create_time(), unrelated_identity)
            supervisor.shutdown()
        finally:
            # Cleanup only this test's independent sleeper and its captured children.
            root = psutil.Process(unrelated.pid)
            descendants = root.children(recursive=True)
            for child in reversed(descendants):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            unrelated.kill()
            unrelated.wait(timeout=5)
            psutil.wait_procs(descendants, timeout=5)


if __name__ == '__main__':
    unittest.main()
