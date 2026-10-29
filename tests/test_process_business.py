"""Offline business predicates; no R13 process or input is used."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from af_r13_bridge.operations import plan, process, visibility


class FakeControl:
    def __init__(self, name="", *, selected=None, focus=False, enabled=True, legacy=0):
        self.Name = name
        self.IsEnabled = enabled
        self.HasKeyboardFocus = focus
        self.selected = selected
        self.legacy = legacy

    def GetSelectionItemPattern(self):
        if self.selected is None:
            raise RuntimeError("not supported")
        return SimpleNamespace(IsSelected=self.selected)

    def GetTogglePattern(self):
        raise RuntimeError("not supported")

    def GetLegacyIAccessiblePattern(self):
        return SimpleNamespace(State=self.legacy)


class ProcessBusinessTests(unittest.TestCase):
    def test_calibration_is_packaged_and_relative_targets_are_inside_controls(self):
        records = plan._recorded()
        self.assertEqual(set(records), {61, 86, 101, 109, 115})
        for event, parent in ((61, None), (86, "AutoForm::ProductionLineDnDBayView"),
                              (101, None), (109, None), (115, None)):
            with self.subTest(event=event):
                self.assertTrue(all(0 <= value < 1 for value in plan._relative(event, parent_class=parent)))

    def test_u_glyph_requires_foreground_shape(self):
        with Image.open(plan.U_GLYPH_REFERENCE) as image:
            reference = image.convert("RGB")
        self.assertTrue(plan.assignment_glyph_evidence(reference)["ok"])
        for background in ((255, 255, 255), (80, 100, 120), (180, 180, 150)):
            with self.subTest(background=background):
                self.assertFalse(plan.assignment_glyph_evidence(Image.new("RGB", reference.size, background))["ok"])

    def test_eye_templates_are_hashed_and_blank_pixels_are_unknown(self):
        templates, metadata = visibility._references()
        for state in ("hidden", "visible"):
            source = metadata["states"][state]
            self.assertEqual(hashlib.sha256((visibility.ASSETS / source["asset"]).read_bytes()).hexdigest(),
                             source["asset_sha256"])
            for role in visibility.ROLES:
                with self.subTest(state=state, role=role):
                    self.assertEqual(visibility.classify_eye(templates[state][role], role)["state"], state)
        self.assertEqual(visibility.classify_eye(Image.new("RGB", (15, 15), "white"), "header")["state"], "unknown")

    def test_reference_reversal_is_rejected_before_any_navigation(self):
        with patch.object(process, "_open_reference_tab") as navigate:
            result = process.add_recorded_reference(object(), reverse=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "REFERENCE_REVERSE_NOT_IN_VERIFIED_ROUTE")
        navigate.assert_not_called()

    def test_missing_friction_readback_cannot_be_success(self):
        with patch.object(process, "select_press_line", return_value={"ok": True}), \
                patch.object(process, "_process_tab"), \
                patch.object(process, "_friction_state", return_value={"ok": True}), \
                patch.object(process, "_wait", return_value=None):
            result = process.set_recorded_friction(object())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "FRICTION_READBACK_NOT_CONFIRMED")

    def test_measurement_empty_status_cell_is_not_a_second_plan(self):
        rows = [FakeControl("方案1"), FakeControl(""), FakeControl("  ")]
        with patch.object(process, "_measurement_plan_table", return_value=object()), \
                patch.object(process, "_visible_controls", return_value=rows):
            self.assertEqual(process._measurement_plan_rows(object()), rows[:1])

    def test_explicit_unselected_source_overrides_keyboard_focus(self):
        source = FakeControl("料片轮廓线 - 成型检查部分", selected=False, focus=True)
        confirm = FakeControl("确定")
        with patch.object(plan, "_controls", return_value=[source]), patch.object(plan, "_one", return_value=confirm):
            result = plan._source_preselection_state(object())
        self.assertFalse(result["ok"])
        self.assertTrue(result["selection_read_directly"])
        self.assertFalse(result["selected"])

    def test_source_focus_fallback_does_not_claim_direct_selection(self):
        source = FakeControl("料片轮廓线 - 成型检查部分", focus=True, legacy=4)
        confirm = FakeControl("确定")
        with patch.object(plan, "_controls", return_value=[source]), patch.object(plan, "_one", return_value=confirm):
            result = plan._source_preselection_state(object())
        self.assertTrue(result["ok"])
        self.assertFalse(result["selection_read_directly"])
        self.assertIsNone(result["selected"])
        self.assertFalse(result["geometry_selection_verified"])

    def test_two_focused_sources_do_not_satisfy_default_submission(self):
        source = FakeControl("料片轮廓线 - 成型检查部分", focus=True, legacy=4)
        unrelated = FakeControl("其他曲线", focus=True, legacy=4)
        with patch.object(plan, "_controls", return_value=[source, unrelated]), \
                patch.object(plan, "_one", return_value=FakeControl("确定")):
            result = plan._source_preselection_state(object())
        self.assertFalse(result["ok"])

    def test_tool_explicit_unselected_is_not_accepted_as_focused_context(self):
        tool = FakeControl("凸模", selected=False, focus=True)
        with patch.object(plan, "_controls", return_value=[tool]):
            result = plan._tool_context_state(object(), "凸模")
        self.assertFalse(result["ok"])
        self.assertTrue(result["selection_read_directly"])

    def test_process_readback_separates_stable_values_from_observation_details(self):
        patches = [
            patch.object(plan, "read_recorded_plan_assignment", return_value={"ok": True, "operation": "C-20", "operation_assignment_verified": True, "screenshot": "transient.png"}),
            patch.object(plan, "read_recorded_blank_state", return_value={"ok": True, "submission_verified": True, "source_names": []}),
            patch.object(plan, "read_recorded_automatic_tools", return_value={"ok": True, "tools": [{"tool": "凸模", "model_mode": "自动", "automatic_contact": True, "tool_context": {"rect": [1, 2, 3, 4]}}]}),
            patch.object(process, "select_press_line", return_value={"ok": True}),
            patch.object(plan, "_select_operation_context", return_value={"ok": True}),
            patch.object(process, "_process_tab"), patch.object(process, "_active_process_page"),
            patch.object(process, "_friction_state", return_value={"ok": True, "selected": "库仑摩擦", "parameters": [{"name": "摩擦系数", "value": "0.15", "kind": "EditControl"}]}),
            patch.object(process, "read_recorded_reference", return_value={"ok": True, "committed_input_present": True}),
            patch.object(process, "_measurement_plan_rows", return_value=[FakeControl("方案1")]),
            patch.object(process, "_click"),
            patch.object(process, "_springback_state", return_value={"ok": True}),
        ]
        from contextlib import ExitStack
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            result = process.read_process_state(object())
        self.assertTrue(result["ok"])
        self.assertFalse(result["persistent_verified"])
        self.assertEqual(result["values"]["measurement_plans"], ["方案1"])
        self.assertEqual(result["values"]["automatic_tools"],
                         [{"tool": "凸模", "model_mode": "自动", "automatic_contact": True}])
        self.assertTrue(result["values"]["blank_contour_committed"])
        self.assertIsNone(result["values"]["reference_orientation"])
        stable = json.dumps(result["values"], ensure_ascii=False)
        self.assertNotIn("transient.png", stable)
        self.assertNotIn("rect", stable)
        self.assertIn("transient.png", json.dumps(result["checks"]))


if __name__ == "__main__":
    unittest.main()
