"""Startup policy tests using independent fakes; never access a live window."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from af_r13_bridge.adapters import session
from af_r13_bridge.models import BridgeError


class StartupFake:
    pid = 2147000003

    def __init__(self, stage='main'):
        self.stage = stage
        self.created_at = 123.5
        self.requests = []
        self.held = False
        self.shutdown_called = False

    def windows(self):
        main = {'hwnd': 100, 'pid': self.pid, 'title': 'AutoForm Forming R13 - no design',
                'owner': 0, 'enabled': True}
        modal = {'hwnd': 200, 'pid': self.pid, 'title': self.stage,
                 'owner': 100 if self.stage == 'welcome' else 0, 'enabled': True}
        if self.stage == 'absent':
            return []
        if self.stage == 'main':
            return [main]
        if self.stage == 'welcome':
            return [main, modal]
        return [modal]

    def discover(self):
        return [] if self.stage == 'absent' else [{'pid': self.pid, 'create_time': self.created_at,
                                                   'r13': True, 'windows': self.windows()}]

    def run_worker(self, request, timeout):
        self.requests.append(request)
        if request['action'] == 'click':
            assert self.held, 'input must hold WriterLease'
            self.stage = 'welcome' if self.stage == 'license' else 'main'
            return {'ok': True, 'input_sent': True}
        root_class, button = {
            'license': (session._LICENSE_CLASS, session._LICENSE_BUTTON),
            'welcome': (session._WELCOME_CLASS, session._WELCOME_BUTTON),
        }.get(self.stage, ('UnknownErrorWindow', 'not-a-button'))
        return {'ok': True, 'controls': [
            {'class': root_class, 'rid': [42, 200]},
            {'aid': button, 'kind': 'ButtonControl', 'enabled': True,
             'offscreen': False, 'rid': [42, 200, 1]},
        ]}

    def lease(self, pid):
        fake = self

        class Lease:
            def __enter__(self):
                fake.held = True

            def __exit__(self, *_exc):
                fake.held = False

        return Lease()

    def shutdown(self):
        self.shutdown_called = True


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def run_fake(self, fake, **kwargs):
        executable = self.directory / 'existing.exe'
        executable.touch()
        application = Mock(pid=fake.pid)
        application.poll.return_value = None

        def launch(*args, **options):
            fake.stage = 'license'
            self.launch_options = options
            return application

        with patch.object(session.native, 'discover', side_effect=fake.discover), \
             patch.object(session.native, 'window_pid', return_value=fake.pid), \
             patch.object(session.native, 'EXE', executable), \
             patch.object(session.native.psutil, 'Process') as identity, \
             patch.object(session.subprocess, 'Popen', side_effect=launch) as start, \
             patch.object(session, 'Supervisor', return_value=fake), \
             patch.object(session, 'WriterLease', side_effect=fake.lease), \
             patch.object(session.time, 'sleep') as sleep:
            identity.return_value.create_time.return_value = fake.created_at
            self.start = start
            self.sleep = sleep
            return session.connect(self.directory / 'evidence', **kwargs)

    def test_attach_never_applies_license_even_if_option_true(self):
        fake = StartupFake('license')
        with self.assertRaises(BridgeError) as raised:
            self.run_fake(fake, launch=True, allow_selected_license=True)
        self.assertEqual(raised.exception.code, 'needs_user')
        self.assertFalse(any(request['action'] == 'click' for request in fake.requests))
        self.start.assert_not_called()
        self.assertTrue(fake.shutdown_called)

    def test_new_launch_defaults_to_readonly_license_prompt(self):
        fake = StartupFake('absent')
        with self.assertRaises(BridgeError):
            self.run_fake(fake, launch=True)
        self.assertEqual([request['action'] for request in fake.requests], ['dump'])

    def test_explicit_new_launch_applies_selection_then_closes_welcome(self):
        fake = StartupFake('absent')
        old_server = os.environ.get('AF_LICENSE_SERVER')
        result = self.run_fake(fake, launch=True, license_server='2375@127.0.0.1',
                               allow_selected_license=True)
        self.assertTrue(result['launched'])
        self.assertTrue(result['selected_license_applied'])
        self.assertTrue(result['welcome_closed'])
        self.assertEqual(sum(request['action'] == 'click' for request in fake.requests), 2)
        self.assertEqual(os.environ.get('AF_LICENSE_SERVER'), old_server)
        self.assertEqual(self.launch_options['env']['AF_LICENSE_SERVER'], '2375@127.0.0.1')
        self.assertTrue(fake.shutdown_called)

    def test_unknown_error_is_dumped_and_rejected_without_wait_or_input(self):
        fake = StartupFake('error')
        with self.assertRaises(BridgeError) as raised:
            self.run_fake(fake)
        self.assertEqual(raised.exception.code, 'needs_user')
        self.assertEqual([request['action'] for request in fake.requests], ['dump'])
        self.sleep.assert_not_called()
        self.assertTrue(raised.exception.details['windows'])

    def test_changed_process_creation_time_is_rejected_before_reading(self):
        fake = StartupFake()
        original = fake.discover
        calls = 0

        def changed():
            nonlocal calls
            calls += 1
            if calls > 1:
                fake.created_at = 999.0
            return original()

        fake.discover = changed
        with self.assertRaises(BridgeError) as raised:
            self.run_fake(fake)
        self.assertEqual(raised.exception.code, 'needs_user')
        self.assertEqual(fake.requests, [])


if __name__ == '__main__':
    unittest.main()
