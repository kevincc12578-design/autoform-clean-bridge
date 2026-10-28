"""Pure selection/readback checks with native modules replaced before import.

No WindowsAdapter is constructed and no process, HWND or desktop is accessed.
"""

import importlib
import sys
import types
import unittest
from unittest.mock import Mock, patch

from af_r13_bridge.models import BridgeError

_native = types.ModuleType("af_r13_bridge.adapters.native")
_session = types.ModuleType("af_r13_bridge.adapters.session")


def _forbidden_connect(*args, **kwargs):
    raise AssertionError("Offline unit tests must never connect to R13")


_session.connect = _forbidden_connect
with patch.dict(sys.modules, {
    "af_r13_bridge.adapters.native": _native,
    "af_r13_bridge.adapters.session": _session,
}):
    _windows = importlib.import_module("af_r13_bridge.adapters.windows")


class WindowsSelectionTests(unittest.TestCase):
    def test_active_title_rejects_another_design_with_shared_prefix(self):
        self.assertTrue(_windows.active_title_matches('AutoForm Forming R13 - work.afd brand','work'))
        self.assertTrue(_windows.active_title_matches('AutoForm Forming R13 - Untitled brand','Untitled'))
        self.assertFalse(_windows.active_title_matches('AutoForm Forming R13 - work-extra.afd brand','work'))
        self.assertFalse(_windows.active_title_matches('AutoForm Forming R13 - work2.afd brand','work'))

    def controls(self):
        names = ["0 零件(s)", "0 模具(s)", "0 坐标系(s)", "0 曲线(s), 0 点(s)"]
        return [{"name": name, "kind": "TextControl", "enabled": True,
                 "rect": [10, 20 + index * 20, 200, 35 + index * 20]}
                for index, name in enumerate(names)]

    def test_explicit_visible_empty_counts_have_semantic_readback(self):
        result = _windows.empty_counts(self.controls(), [0, 0, 800, 600])
        self.assertEqual(result["entity_counts"], {
            "parts": 0, "tools": 0, "coordinate_systems": 0, "curves_points": [0, 0]})

    def test_missing_count_is_unknown_not_zero(self):
        with self.assertRaises(BridgeError) as caught:
            _windows.empty_counts(self.controls()[:-1], [0, 0, 800, 600])
        self.assertEqual(caught.exception.code, "readback_unavailable")

    def test_duplicate_visible_count_is_ambiguous(self):
        controls = self.controls()
        controls.append(dict(controls[0]))
        with self.assertRaises(BridgeError) as caught:
            _windows.empty_counts(controls, [0, 0, 800, 600])
        self.assertEqual(caught.exception.code, "readback_unavailable")

    def test_negative_coordinate_hidden_workspace_cannot_supply_count(self):
        controls = self.controls()
        controls[0]["rect"] = [-500, 20, -100, 40]
        with self.assertRaises(BridgeError):
            _windows.empty_counts(controls, [0, 0, 800, 600])

    def test_disabled_count_cannot_supply_readback(self):
        controls = self.controls()
        controls[1]["enabled"] = False
        with self.assertRaises(BridgeError):
            _windows.empty_counts(controls, [0, 0, 800, 600])

    def test_nonempty_design_does_not_pass_empty_scenario(self):
        controls = self.controls()
        controls[0]["name"] = "1 零件(s)"
        with self.assertRaises(BridgeError) as caught:
            _windows.empty_counts(controls, [0, 0, 800, 600])
        self.assertEqual(caught.exception.code, "scenario_not_verified")

    def test_unique_does_not_choose_first_duplicate(self):
        for controls in ([], [{"aid": "save"}, {"aid": "save"}]):
            with self.subTest(controls=controls), self.assertRaises(BridgeError):
                _windows.unique(controls, aid="save")

    def test_failed_worker_reap_keeps_writer_lease_and_can_retry(self):
        adapter=object.__new__(_windows.WindowsAdapter)
        adapter._closed=False
        adapter.supervisor=Mock()
        adapter.lease=Mock()
        adapter.supervisor.shutdown.side_effect=RuntimeError('worker still alive')
        with self.assertRaises(RuntimeError):
            adapter.shutdown()
        adapter.lease.release.assert_not_called()
        self.assertFalse(adapter._closed)
        adapter.supervisor.shutdown.side_effect=None
        adapter.shutdown()
        adapter.lease.release.assert_called_once()
        self.assertTrue(adapter._closed)


if __name__ == "__main__":
    unittest.main()
