"""Exercise actual native guard wiring with a fake user32, never a desktop."""
import ctypes
import importlib.util
from pathlib import Path
import unittest
import json
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import Mock,patch

import af_r13_bridge
from af_r13_bridge.models import BridgeError
from af_r13_bridge.window_policy import expected_profile


def isolated_native():
    path=Path(af_r13_bridge.__file__).parent/'adapters'/'native.py'
    spec=importlib.util.spec_from_file_location('af_r13_bridge.adapters.native_increment_tests',path)
    module=importlib.util.module_from_spec(spec)
    with patch.object(ctypes,'WinDLL',return_value=Mock(),create=True):
        spec.loader.exec_module(module)
    return module


class NativeGuardWiringTests(unittest.TestCase):
    def setUp(self):
        self.n=isolated_native()
        self.profile=expected_profile(1,9)
        self.n.configure_input(self.profile)
        self.n.window_pid=Mock(return_value=9)
        self.n.geometry=Mock(return_value={**self.profile,'minimized':False})
        self.n.windows=Mock(return_value=[dict(hwnd=1,pid=9,owner=0,class_name='Main')])
        self.n.gui_state=Mock(return_value=dict(capture=0,focus=1,thread_id=7))
        self.n.root_window=Mock(side_effect=lambda hwnd:hwnd)
        self.n.u.IsIconic.return_value=False
        self.n.u.IsWindowEnabled.return_value=True
        self.n.u.GetForegroundWindow.return_value=1
        self.n.foreground=Mock()

    def test_capture_failure_prevents_foreground_or_input(self):
        self.n.gui_state.return_value['capture']=88
        with self.assertRaises(BridgeError) as caught:
            self.n.guard_point(1,9,[10,10,20,20])
        self.assertEqual(caught.exception.code,'MOUSE_CAPTURE_REDIRECTED')
        self.n.foreground.assert_not_called()
        self.n.u.WindowFromPoint.assert_not_called()

    def test_context_is_rechecked_after_activation(self):
        def activate(*args):
            self.n.windows.return_value.append(dict(hwnd=2,pid=9,owner=0,class_name='UnknownPopup'))
        self.n.foreground.side_effect=activate
        with self.assertRaises(BridgeError) as caught:
            self.n.guard_input(1,9,activate=True)
        self.assertEqual(caught.exception.code,'UNEXPECTED_OWNED_WINDOW')
        self.assertEqual(self.n.windows.call_count,2)

    def test_rejected_window_details_survive_business_string_only_errors(self):
        popup=dict(hwnd=2,pid=9,owner=0,class_name='UnclassifiedTransient')
        self.n.windows.return_value.append(popup)
        stderr=StringIO()
        with redirect_stderr(stderr),self.assertRaises(BridgeError):
            self.n.guard_input(1,9)
        evidence=json.loads(stderr.getvalue())
        self.assertEqual(evidence['error']['details']['windows'],[popup])

    def test_resize_rejected_before_activation_can_restore_it(self):
        self.n.geometry.return_value['client_rect']=[35,50,1835,1010]
        with self.assertRaises(BridgeError):
            self.n.guard_input(1,9,activate=True)
        self.n.foreground.assert_not_called()

    def test_gui_query_error_is_never_treated_as_no_capture(self):
        self.n=isolated_native()
        self.n.u.GetWindowThreadProcessId.return_value=0
        with self.assertRaises(BridgeError) as caught:
            self.n.gui_state(1)
        self.assertEqual(caught.exception.code,'GUI_STATE_UNAVAILABLE')

    def test_disabled_target_cannot_receive_input(self):
        self.n.u.IsWindowEnabled.return_value=False
        with self.assertRaises(BridgeError):
            self.n.guard_input(1,9,activate=True)
        self.n.foreground.assert_not_called()

    def test_material_context_is_cleared_after_error_and_not_nested(self):
        with self.assertRaises(ValueError):
            with self.n.material_preview_context(2,{'hwnd':6}):
                self.assertEqual(self.n._material_preview[0],2)
                with self.assertRaises(BridgeError):
                    with self.n.material_preview_context(3,{'hwnd':7}):
                        pass
                raise ValueError('file input failed')
        self.assertIsNone(self.n._material_preview)


if __name__=='__main__':
    unittest.main()
