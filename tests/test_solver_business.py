"""Pure evidence tests and owned Python process tests; never launch AutoForm."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from af_r13_bridge import solver
from af_r13_bridge.models import BridgeError


SUCCESS = '''ct: Start of process step "C-20 Springback"
nl: Springback solution converged in 7 iterations.
ct: Simulation successfully finished.
Program END [1234 0]
'''
GEOMETRY = '''Initializing operation "C-20"
Blank material data
New number of elements: 42
Tool center initial coordinates of "凸模"
Tool center initial coordinates of "凹模"
Program END [1234 0]
'''


class SolverEvidenceTests(unittest.TestCase):
    def test_success_requires_convergence_success_and_end_in_one_segment(self):
        result = solver.solver_completion_evidence(SUCCESS)
        self.assertTrue(result['ok'])
        self.assertEqual([0], result['program_end_codes'])
        self.assertFalse(result['numeric_springback_field_read'])
        split = SUCCESS.replace('ct: Simulation successfully finished.',
                                'Program END [2222 0]\nct: Simulation successfully finished.')
        self.assertFalse(solver.solver_completion_evidence(split)['ok'])

    def test_planned_springback_does_not_prove_executed_solution(self):
        text = 'Process step .... : "Springback"\nct: Simulation successfully finished.\nProgram END [11 0]\n'
        result = solver.solver_completion_evidence(text)
        self.assertFalse(result['ok'])
        self.assertIn('springback_convergence', result['missing'])

    def test_bad_or_unparsed_end_after_success_rejects_entire_run(self):
        for tail in ('Program END [4321 -3]\n', 'Program END [unparsed]\n'):
            with self.subTest(tail=tail):
                self.assertFalse(solver.solver_completion_evidence(SUCCESS + tail)['ok'])

    def test_fatal_error_rejects_success_markers(self):
        self.assertFalse(solver.solver_completion_evidence(SUCCESS + 'ct: FATAL ERROR: failed\n')['ok'])

    def test_geometry_requires_both_tools_and_positive_mesh(self):
        self.assertTrue(solver.native_geometry_evidence(GEOMETRY)['ok'])
        for text in (GEOMETRY.replace('elements: 42', 'elements: 0'),
                     GEOMETRY.replace('Tool center initial coordinates of "凹模"', ''),
                     GEOMETRY.replace('"C-20"', '"C-10"')):
            self.assertFalse(solver.native_geometry_evidence(text)['ok'])

    def test_cancellation_requires_matching_job_and_running_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            state = {'kind': 'af_r13_bridge.native_solver', 'work_dir': str(directory),
                     'job_id': 'owned-job', 'status': 'running'}
            (directory / 'native-job.json').write_text(json.dumps(state), encoding='utf-8')
            with self.assertRaises(BridgeError):
                solver.cancel_job(directory, 'different-job')
            self.assertFalse((directory / 'cancel-request.json').exists())
            result = solver.cancel_job(directory, 'owned-job')
            self.assertTrue(result['cancel_requested'])
            self.assertFalse(result['cancelled'])
            state['status'] = 'completed'
            (directory / 'native-job.json').write_text(json.dumps(state), encoding='utf-8')
            self.assertFalse(solver.cancel_job(directory, 'owned-job')['ok'])


class GuiJobEvidenceTests(unittest.TestCase):
    def setUp(self):
        from af_r13_bridge.operations import simulation
        self.simulation = simulation
        self.target = Path('D:/work/baogu-12abcdef.afd')
        self.job = {'name': 'SingleSim_A1', 'id': 'current-id'}

    def test_started_job_requires_full_target_and_current_save(self):
        log = ('Started job: AFFormingJob -jobId SingleSim_A1#current-id -jn '
               f'{self.target} -ls 2375@127.0.0.1\n#FILE_LOG#Save:{self.target}\n')
        self.assertTrue(self.simulation.current_job_evidence(log, self.target)['ok'])
        self.assertFalse(self.simulation.current_job_evidence(log.split('#FILE_LOG#Save:')[0], self.target)['ok'])
        self.assertFalse(self.simulation.current_job_evidence(log, Path('D:/elsewhere') / self.target.name)['ok'])

    def test_service_completed_alone_cannot_prove_bound_job_completion(self):
        self.assertFalse(self.simulation.job_status_evidence('sigChanged Completed current-id', self.job, self.target)['completed'])
        xml = ('<singleSimJobStatus><jobId_R11><name>SingleSim_A1</name><id>current-id</id></jobId_R11>'
               f'<fileName>{self.target}</fileName><status>Calculated</status><failing>false</failing>'
               '<canceling>false</canceling><check>false</check></singleSimJobStatus>')
        log = f'Message: Input file: {self.target}\n{xml}\nsigChanged Completed current-id\n'
        self.assertTrue(self.simulation.job_status_evidence(log, self.job, self.target)['completed'])
        self.assertFalse(self.simulation.job_status_evidence(log + '<singleSimJobStatus>', self.job, self.target)['completed'])
        self.assertFalse(self.simulation.job_status_evidence(log.replace('current-id', 'old-id'), self.job, self.target)['completed'])

    def test_native_gui_log_binding_rejects_unchanged_or_unbound_output(self):
        import hashlib
        text = 'Sending job SingleSim_A1\n' + SUCCESS
        result = self.simulation.bind_native_log(text, self.job, self.target)
        self.assertTrue(result['ok'])
        self.assertTrue(result['completion']['ok'])
        self.assertFalse(self.simulation.bind_native_log(text, self.job, self.target,
                          hashlib.sha256(text.encode()).hexdigest())['ok'])
        self.assertFalse(self.simulation.bind_native_log(SUCCESS, self.job, self.target)['ok'])

    def test_native_gui_log_common_basename_cannot_bind_job(self):
        common = Path('D:/work/project.afd')
        text = 'post: Postfile "project.afd" opened.\n' + SUCCESS
        self.assertFalse(self.simulation.bind_native_log(text, self.job, common)['ok'])


class FinalSaveBytesTests(unittest.TestCase):
    def setUp(self):
        from af_r13_bridge.operations import simulation
        self.simulation = simulation
        self.temp = tempfile.TemporaryDirectory()
        self.target = Path(self.temp.name).resolve() / 'owned.afd'
        self.target.write_bytes(b'complete model bytes')

    def tearDown(self):
        self.temp.cleanup()

    def test_positive_file_size_before_job_completion_does_not_hash_or_pass(self):
        from types import SimpleNamespace
        session = SimpleNamespace(design_runtime_id=[42, 1],
                                  title=lambda: f'AutoForm Forming R13 - {self.target.name} (test)')
        dialog = {'ok': True, 'path_readback': str(self.target)}
        with patch.object(self.simulation, '_design_tabs', return_value=[[42, 1]]), \
                patch.object(self.simulation, 'hash_file') as read:
            result = self.simulation._save_transaction(session, self.target, [[42, 1]], dialog)
        self.assertFalse(result['ok'])
        self.assertTrue(result['identity_verified'])
        self.assertFalse(result['file_bytes_verified'])
        self.assertIsNone(result['sha256'])
        read.assert_not_called()

    def test_transient_permission_denial_requires_two_later_stable_hashes(self):
        expected = self.simulation.hash_file(self.target)
        with patch.object(self.simulation, 'hash_file',
                          side_effect=[PermissionError(13, 'file still shared exclusively'), expected, expected]) as read:
            result = self.simulation._stable_file_bytes(self.target, timeout=.2, interval=.001)
        self.assertTrue(result['ok'])
        self.assertEqual(3, read.call_count)
        self.assertEqual(expected, result['sha256'])
        self.assertEqual(2, len(result['samples']))
        self.assertEqual('PermissionError', result['transient_observations'][0]['error'])

    def test_different_hashes_cannot_be_accepted_as_stable(self):
        with patch.object(self.simulation, 'hash_file', side_effect=['first', 'second', 'second']) as read:
            result = self.simulation._stable_file_bytes(self.target, timeout=.2, interval=.001)
        self.assertTrue(result['ok'])
        self.assertEqual(3, read.call_count)
        self.assertEqual('second', result['sha256'])
        self.assertEqual(['second', 'second'], [value['sha256'] for value in result['samples']])

    def test_modification_during_hash_is_rejected_before_resampling(self):
        real_hash = self.simulation.hash_file
        first = True
        def changing(path):
            nonlocal first
            value = real_hash(path)
            if first:
                first = False
                path.write_bytes(b'new completed model with a different length')
            return value
        with patch.object(self.simulation, 'hash_file', side_effect=changing):
            result = self.simulation._stable_file_bytes(self.target, timeout=.2, interval=.001)
        self.assertTrue(result['ok'])
        self.assertEqual(real_hash(self.target), result['sha256'])
        self.assertIn('FILE_CHANGED_DURING_HASH', [value['error'] for value in result['transient_observations']])

    def test_persistent_lock_times_out_without_saved_byte_claim(self):
        with patch.object(self.simulation, 'hash_file', side_effect=PermissionError(13, 'still locked')):
            result = self.simulation._stable_file_bytes(self.target, timeout=.015, interval=.002)
        self.assertFalse(result['ok'])
        self.assertTrue(result['timed_out'])
        self.assertIsNone(result['sha256'])


@unittest.skipUnless(os.name == 'nt', 'Windows process containment is required')
class NativeProcessTests(unittest.TestCase):
    def _run_python(self, code, *, timeout=3, cancel_event=None):
        real_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            install = directory / 'bin'
            install.mkdir()
            (install / 'AFFormingJob.exe').write_bytes(b'test-placeholder-never-executed')
            project = directory / 'test.afd'
            project.write_bytes(b'test-afd-never-passed-to-autoform')
            launched = []
            def launch(_command, **kwargs):
                process = real_popen([sys.executable, '-I', '-c', code], **kwargs)
                launched.append(process)
                return process
            with patch.object(solver, 'INSTALL_BIN', install), patch.object(solver.subprocess, 'Popen', launch):
                value = solver.run_job(project, work_dir=directory, timeout=timeout, cancel_event=cancel_event)
            self.assertEqual(1, len(launched))
            self.assertIsNotNone(launched[0].poll())
            self.assertTrue(value['workers_stopped'])
            self.assertFalse(value['application_terminated'])
            self.assertEqual(value['status'], solver.status_job(directory)['status'])
            return value

    def test_native_runner_confirms_exit_and_complete_end_set(self):
        value = self._run_python("print('Program END [1234 0]', flush=True)")
        self.assertTrue(value['ok'])
        self.assertEqual('completed', value['status'])

    def test_native_runner_times_out_its_owned_process(self):
        value = self._run_python('import time; time.sleep(30)', timeout=.25)
        self.assertFalse(value['ok'])
        self.assertTrue(value['timed_out'])
        self.assertEqual('timed_out', value['status'])

    def test_native_runner_observes_cooperative_cancel(self):
        cancel = threading.Event()
        cancel.set()
        value = self._run_python('import time; time.sleep(30)', cancel_event=cancel)
        self.assertTrue(value['cancelled'])
        self.assertEqual('cancelled', value['status'])


if __name__ == '__main__':
    unittest.main()
