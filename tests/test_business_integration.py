"""Public business/lifecycle integration with an independent in-memory adapter.

No AutoForm imports, UI calls, or previous project's test doubles are used.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from af_r13_bridge.api import Bridge
from af_r13_bridge.lifecycle import Lifecycle, canonical_path, digest
from af_r13_bridge.models import Design
from af_r13_bridge.workflow import run_once


class BusinessStore:
    def __init__(self, directory, **_options):
        self.directory = Path(directory)
        self.baseline = Design('user', 'User original', 'C:/user/original.afd', None)
        self.documents = {'user': self.baseline}
        self.values = {}
        self.sequence = 0
        self.calls = []
        self.fail_operation = None
        self.proof_fault = None
        self.change_on_reopen = False
        self.close_noop = False
        self.stopped = False

    def inventory(self):
        return list(self.documents.values())

    def _new(self, path=None):
        self.sequence += 1
        token = f'owned-{self.sequence}'
        document = Design(token, Path(path).stem if path else 'Empty', str(path) if path else None,
                          None, {'runtime_id': [42, self.sequence]})
        self.documents[token] = document
        self.values[token] = {'parts': 0, 'material': None, 'thickness_mm': None}
        return document

    def create(self):
        self.calls.append(('create',))
        return self._new()

    def save(self, document, path):
        self.calls.append(('save', document.token, str(path)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.values[document.token]), encoding='utf-8')
        updated = replace(self.documents[document.token], name=path.stem, path=str(path),
                          metadata={**document.metadata, 'save_verified': True})
        self.documents[document.token] = updated
        return updated

    def close(self, document):
        self.calls.append(('close', document.token))
        if not self.close_noop:
            del self.documents[document.token]
            del self.values[document.token]

    def open(self, path):
        self.calls.append(('open', str(path)))
        document = self._new(path)
        self.values[document.token] = json.loads(path.read_text(encoding='utf-8'))
        if self.change_on_reopen:
            self.values[document.token]['thickness_mm'] = 9.0
        return document

    def readback(self, document):
        return deepcopy(self.values[document.token])

    def business(self, document, operation, arguments):
        self.calls.append(('business', operation, document.token))
        current = self.documents[document.token]
        if operation != 'read_business':
            self.documents[document.token] = replace(current, metadata={'runtime_id': current.metadata['runtime_id']})
        if operation == 'import_part':
            self.values[document.token]['parts'] = 1
        if operation == 'set_material':
            self.values[document.token].update(material=Path(arguments['path']).stem,
                                                thickness_mm=arguments.get('thickness_mm', 4.0))
        if operation == 'read_business':
            return {'ok': True, 'values': deepcopy(self.values[document.token])}
        if operation == 'set_direction':
            return {'ok': True, 'after': {'angles_deg': {'X': -180.0, "Y'": 0.0, "Z''": 0.0}}}
        if operation == 'start_simulation':
            target = Path(arguments['path'])
            target.write_text(json.dumps(self.values[document.token]), encoding='utf-8')
            rid = current.metadata['runtime_id']
            proof = {'ok': True, 'path': str(target), 'path_readback': str(target),
                     'before_rid': rid, 'after_rid': rid, 'sha256': digest(target)}
            if self.proof_fault == 'rid':
                proof['after_rid'] = [42, 999]
            elif self.proof_fault == 'hash':
                proof['sha256'] = '0' * 64
            elif self.proof_fault == 'path_readback':
                proof['path_readback'] = str(target.parent / 'different.afd')
            self.documents[document.token] = replace(current, path=str(target), name=target.stem,
                metadata={'runtime_id': rid, 'save_verified': True, 'save_transaction': proof})
            return {'ok': True, 'save_transaction': proof, 'springback': True}
        return {'ok': operation != self.fail_operation,
                'error': 'injected_business_failure' if operation == self.fail_operation else None}

    def metrics(self):
        return {'working_set_bytes': None, 'private_bytes': None}

    def shutdown(self):
        self.stopped = True


class BusinessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name).resolve()
        self.adapter = BusinessStore(self.directory)
        self.bridge = Bridge(self.adapter, self.directory)
        self.assertTrue(self.bridge.call('attach')['ok'])

    def tearDown(self):
        self.temp.cleanup()

    def create(self):
        result = self.bridge.call('create_design')
        self.assertTrue(result['ok'])
        return result['document_id']

    def test_baseline_business_request_never_reaches_adapter(self):
        result = self.bridge.call('set_material', {'document_id': 'user', 'path': 'steel.mtb'})
        self.assertFalse(result['ok'])
        self.assertEqual('invalid_document_handle', result['error']['code'])
        self.assertEqual([], self.adapter.calls)
        self.assertEqual(self.adapter.baseline, self.adapter.documents['user'])

    def test_start_save_registers_only_exact_owned_transaction(self):
        token = self.create()
        path = self.directory / 'business.afd'
        result = self.bridge.call('start_simulation', {'document_id': token, 'path': str(path)})
        self.assertTrue(result['ok'])
        saved = self.bridge.lifecycle.saved[canonical_path(path)]
        self.assertEqual(digest(path), saved['sha256'])
        self.assertEqual(str(path), self.bridge._handles[token].path)
        self.assertEqual(self.adapter.baseline, self.adapter.documents['user'])

    def test_start_save_rejects_changed_identity_hash_or_file_dialog_readback(self):
        for fault in ('rid', 'hash', 'path_readback'):
            with self.subTest(fault=fault):
                directory = self.directory / fault
                directory.mkdir()
                adapter = BusinessStore(directory)
                adapter.proof_fault = fault
                bridge = Bridge(adapter, directory)
                bridge.call('attach')
                token = bridge.call('create_design')['document_id']
                result = bridge.call('start_simulation', {'document_id': token, 'path': str(directory / 'work.afd')})
                self.assertFalse(result['ok'])
                self.assertEqual('business_save_unverified', result['error']['code'])
                self.assertEqual({}, bridge.lifecycle.saved)

    def test_failed_business_batch_preserves_content_and_closes_owned_only(self):
        # Batch attaches its own immutable baseline, so use a fresh Bridge.
        self.adapter.fail_operation = 'import_part'
        bridge = Bridge(self.adapter, self.directory)
        result = bridge.batch([
            {'operation': 'attach'},
            {'operation': 'create_design', 'as': 'work'},
            {'operation': 'import_part', 'request': {'document_id': '@work', 'path': 'part.step'}},
            {'operation': 'set_material', 'request': {'document_id': '@work', 'path': 'steel.mtb'}},
        ])
        self.assertFalse(result['ok'])
        self.assertTrue(result['data']['cleanup']['cleanup_ok'])
        self.assertTrue(self.adapter.stopped)
        self.assertEqual({'user': self.adapter.baseline}, self.adapter.documents)
        self.assertFalse(any(call[:2] == ('business', 'set_material') for call in self.adapter.calls))
        diagnostic = list((self.directory / 'diagnostics').glob('*.afd'))
        self.assertEqual(1, len(diagnostic))
        self.assertEqual(1, json.loads(diagnostic[0].read_text())['parts'])

    def test_native_solve_cannot_use_disk_state_after_new_unsaved_business_change(self):
        token = self.create()
        path = self.directory / 'business.afd'
        self.assertTrue(self.bridge.call('start_simulation', {'document_id': token, 'path': str(path)})['ok'])
        self.assertTrue(self.bridge.call('set_material', {'document_id': token, 'path': 'changed.mtb'})['ok'])
        with patch('af_r13_bridge.solver.solve', return_value={'ok': True}) as execute:
            result = self.bridge.call('solve', {'document_id': token})
        self.assertFalse(result['ok'])
        execute.assert_not_called()


class WorkflowPersistenceTests(unittest.TestCase):
    def _run(self, *, change_on_reopen=False, close_noop=False):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            part, material = directory / 'part.step', directory / 'steel.mtb'
            part.write_bytes(b'part input')
            material.write_bytes(b'material input')
            stores = []
            def factory(path, **options):
                adapter = BusinessStore(path, **options)
                adapter.change_on_reopen = change_on_reopen
                adapter.close_noop = close_noop
                stores.append(adapter)
                return adapter
            with patch('af_r13_bridge.solver.solve', return_value={'ok': True, 'springback': True,
                       'geometry_input_validation': {'ok': True}, 'program_end_codes': [0]}):
                result = run_once(directory / 'run', part, material, adapter_factory=factory)
            return result, stores[0]

    def test_nonempty_same_file_reopen_and_final_close_pass_with_baseline_retained(self):
        result, adapter = self._run()
        self.assertTrue(result['e2e_ok'], result.get('error'))
        self.assertEqual(1, result['persistence']['before']['parts'])
        self.assertEqual(result['persistence']['before'], result['persistence']['after'])
        opened = [call[1] for call in adapter.calls if call[0] == 'open']
        self.assertEqual([result['final_file']['path']], opened)
        self.assertEqual(2, len([call for call in adapter.calls if call[0] == 'close']))
        self.assertEqual({'user': adapter.baseline}, adapter.documents)
        self.assertTrue(adapter.stopped)

    def test_changed_business_value_on_same_file_reopen_cannot_pass(self):
        result, adapter = self._run(change_on_reopen=True)
        self.assertFalse(result['e2e_ok'])
        self.assertEqual('persistence_mismatch', result['error']['code'])
        self.assertTrue(result['cleanup']['ok'])
        self.assertEqual({'user': adapter.baseline}, adapter.documents)

    def test_close_without_tab_disappearance_cannot_pass(self):
        result, adapter = self._run(close_noop=True)
        self.assertFalse(result['e2e_ok'])
        self.assertFalse(result['cleanup']['ok'])
        self.assertEqual(adapter.baseline, adapter.documents['user'])
        self.assertTrue(adapter.stopped)

    def test_final_evidence_write_failure_rejects_success_after_cleanup(self):
        original_event = Lifecycle._event

        def fail_disconnect_event(lifecycle, operation, **data):
            # Fail the real evidence writer only after closure and shutdown,
            # so there is no later operation whose _ready() can catch the error.
            if operation == 'disconnect':
                with patch.object(Path, 'open', side_effect=OSError('injected final evidence write failure')):
                    return original_event(lifecycle, operation, **data)
            return original_event(lifecycle, operation, **data)

        with patch.object(Lifecycle, '_event', new=fail_disconnect_event):
            result, adapter = self._run()
        self.assertTrue(result['cleanup']['ok'])
        self.assertTrue(result['disconnect']['ok'])
        self.assertEqual({'user': adapter.baseline}, adapter.documents)
        self.assertTrue(adapter.stopped)
        self.assertFalse(result['e2e_ok'])
        self.assertFalse(result['ok'])
        self.assertEqual('evidence_write_failed', result['error']['code'])
        self.assertEqual('disconnect', result['evidence_errors'][0]['operation'])
        self.assertIn('injected final evidence write failure', result['evidence_errors'][0]['message'])


if __name__ == '__main__':
    unittest.main()
