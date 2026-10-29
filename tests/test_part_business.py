"""Part business predicates: pure data tests, no AutoForm or desktop calls."""
import math
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from af_r13_bridge.operations import part
from af_r13_bridge.operations.part import (
    angles_from_rows, bending_result_evidence, material_matches,
    part_count_from_rows, persisted_direction_matches, reversal_confirmed,
    imported_part_evidence, step_part_identity,
)


def angle_rows(x="0 °", y="0 °", z="0 °"):
    return [{"type": "SpinnerControl", "name": name, "value": value,
             "rect": [0, 10 * index, 100, 10 * index + 8]}
            for index, (name, value) in enumerate((("X", x), ("Y′", y), ("Z″", z)))]


def bending_rows(weight="2.5 kg", force="10 kN"):
    return [
        {"type": "TextControl", "class_name": "AFLabel", "name": "零件重量: " + weight},
        {"type": "TextControl", "class_name": "AFLabel", "name": "冲力: " + force},
        {"type": "CheckBoxControl", "class_name": "AFProgressiveDisclosureBtn", "name": "评估结果"},
        {"type": "CheckBoxControl", "class_name": "AFProgressiveDisclosureBtn", "name": "评估力"},
        {"type": "CheckBoxControl", "class_name": "QToolButton", "name": "展开的料片"},
        {"type": "PaneControl", "class_name": "AutoForm::FeasibilityBendingView", "rect": [0, 0, 100, 100]},
    ]


class PartBusinessPredicates(unittest.TestCase):
    def test_readback_opens_import_when_part_stage_remembers_forming_check(self):
        session = SimpleNamespace()
        active = {"page": "part.formcheck"}
        import_page, material_page, tip_page = object(), object(), object()
        pages = {"AutoForm::PartImportPageView": ("part.import", import_page),
                 "AutoForm::MaterialPageView": ("part.material", material_page),
                 part.TIP_PAGE: ("part.tip", tip_page)}

        def open_page(_session, key):
            # Opening just the parent stage retains its active subpage.
            if key != "part":
                active["page"] = key
            return {"ok": True, "page": key}

        def visible_page(_session, class_name):
            key, page = pages.get(class_name, (None, None))
            return page if active["page"] == key else None

        def count(_session):
            if active["page"] == "part.import":
                return {"ok": True, "count": 1}
            return {"ok": False, "count": None}

        def tree(page):
            return [("抱箍-基体-法兰1", object()), ("未使用的面片", object())] if page is import_page else []

        angles = {"X": -180.0, "Y'": 0.0, "Z''": 0.0}
        with patch.object(part.ui, "open_page", side_effect=open_page) as navigation, \
             patch.object(part, "_page", side_effect=visible_page), \
             patch.object(part, "_part_count", side_effect=count), \
             patch.object(part, "_tree_items", side_effect=tree), \
             patch.object(part, "_rows", return_value=[{"name": "S355MC_4936_4.00/4.000 mm"}]), \
             patch.object(part, "_unique", return_value=object()), \
             patch.object(part, "_click", return_value={"ok": True}), \
             patch.object(part, "read_direction_angles", return_value={"ok": True, "angles_deg": angles}):
            result = part.read_part_state(session, "S355MC_4936_4.00", expected_angles=angles)
        self.assertTrue(result["ok"], result)
        self.assertEqual(1, result["values"]["part_count"])
        self.assertEqual(["抱箍-基体-法兰1"], result["values"]["part_names"])
        self.assertEqual("part.import", navigation.call_args_list[0].args[1])

    def test_readback_failed_import_navigation_does_not_invent_count(self):
        with patch.object(part.ui, "open_page", return_value={"ok": False}), \
             patch.object(part, "_part_count") as count, patch.object(part, "_tree_items") as tree:
            result = part.read_part_state(object(), "S355MC_4936_4.00")
        self.assertFalse(result["ok"])
        self.assertEqual("PART_READBACK_PAGE_FAILED", result["error"])
        self.assertEqual({}, result["values"])
        count.assert_not_called()
        tree.assert_not_called()

    def material_session(self):
        return SimpleNamespace(pid=7, hwnd=11, verify_document=Mock(return_value=True))

    def material_main(self):
        return SimpleNamespace(hwnd=11, pid=7, owner=0, enabled=True)

    def test_material_cleanup_rejects_different_picker_path_without_input(self):
        session = self.material_session()
        picker = SimpleNamespace(hwnd=12, pid=7, owner=11, title="材料文件", class_name="#32770")
        context = {"entry_clicked": True, "path": "D:/run/current.mtb", "editor_rid": [1],
                   "picker": vars(picker)}
        edit = Mock()
        edit.GetValuePattern.return_value.Value = "D:/someone-else/material.mtb"
        with patch.object(part.ui, "windows", return_value=[self.material_main(), picker]), \
             patch.object(part.ui, "control_from_handle"), patch.object(part, "_by_id", return_value=edit), \
             patch.object(part, "_click") as click, patch.object(part, "_page") as page:
            result = part._cleanup_material_failure(session, context)
        self.assertFalse(result["ok"])
        click.assert_not_called()
        page.assert_not_called()

    def test_material_cleanup_waits_for_picker_and_preview_before_editor(self):
        session = self.material_session()
        main = self.material_main()
        picker = SimpleNamespace(hwnd=12, pid=7, owner=11, title="材料文件", class_name="#32770")
        preview = SimpleNamespace(hwnd=13)
        context = {"entry_clicked": True, "path": "D:/run/current.mtb", "editor_rid": [1],
                   "picker": vars(picker)}
        edit, cancel = Mock(), Mock(ControlTypeName="ButtonControl", IsEnabled=True)
        edit.GetValuePattern.return_value.Value = context["path"]
        with patch.object(part.ui, "windows", side_effect=[[main, picker, preview], [main], [main]]), \
             patch.object(part.ui, "control_from_handle"), patch.object(part, "_by_id", side_effect=[edit, cancel]), \
             patch.object(part, "_click", return_value={"ok": True}), patch.object(part, "_page", return_value=None):
            result = part._cleanup_material_failure(session, context)
        self.assertTrue(result["ok"])
        self.assertEqual([item["action"] for item in result["steps"]], ["cancel_material_picker", "picker_and_preview_closed"])
        self.assertFalse(result["document_closed"])

    def test_material_cleanup_invokes_only_matching_owned_editor(self):
        session = self.material_session()
        editor, cancel = Mock(), Mock(IsEnabled=True)
        editor.GetRuntimeId.return_value = [42, 10]
        context = {"entry_clicked": True, "editor_rid": [42, 10]}
        with patch.object(part.ui, "windows", return_value=[self.material_main()]), \
             patch.object(part, "_page", side_effect=[editor, None]), \
             patch.object(part, "_unique", return_value=cancel), patch.object(part, "_visible", return_value=True), \
             patch.object(part.ui, "_prepare", return_value={"ok": True}), \
             patch.object(part.ui, "_check_point", return_value={"ok": True}), \
             patch.object(part.ui, "rect", return_value=(0, 0, 30, 20)):
            result = part._cleanup_material_failure(session, context)
        self.assertTrue(result["ok"])
        cancel.GetInvokePattern.return_value.Invoke.assert_called_once()
        editor.GetRuntimeId.return_value = [99]
        with patch.object(part.ui, "windows", return_value=[self.material_main()]), \
             patch.object(part, "_page", return_value=editor), patch.object(part, "_unique") as unique:
            result = part._cleanup_material_failure(session, context)
        self.assertFalse(result["ok"])
        unique.assert_not_called()

    def test_material_exception_survives_cleanup_failure(self):
        with patch.object(part, "_apply_material", side_effect=RuntimeError("File dialog did not close after submit")), \
             patch.object(part, "_cleanup_material_failure", return_value={"ok": False, "error": "cleanup also failed"}):
            result = part.apply_material(self.material_session(), "D:/run/current.mtb")
        self.assertFalse(result["ok"])
        self.assertEqual(result["exception"]["message"], "File dialog did not close after submit")
        self.assertEqual(result["material_cleanup"]["error"], "cleanup also failed")

    def imported_rows(self, part_name="抱箍-基体-法兰1"):
        return [{"type": "TextControl", "name": "1 零件(s)", "enabled": True},
                {"type": "TreeItemControl", "name": part_name, "enabled": True},
                {"type": "TreeItemControl", "name": "提取", "enabled": True},
                {"type": "TreeItemControl", "name": "未使用的面片", "enabled": True}]

    def test_step_identity_decodes_actual_named_product_and_solid(self):
        text = r"#338 = MANIFOLD_SOLID_BREP ( '\X2\57fa4f53\X0\-\X2\6cd55170\X0\1', #129 );" + "\n" + r"#539 = PRODUCT ( '\X2\62b17b8d\X0\', '', '', (#978));"
        result = step_part_identity(text)
        self.assertTrue(result["ok"])
        self.assertEqual(result["expected_part_names"], ["抱箍-基体-法兰1"])
        self.assertFalse(step_part_identity(text + text)["ok"])

    def test_named_import_requires_count_identity_extract_and_no_modal(self):
        expected = ["抱箍-基体-法兰1"]
        self.assertTrue(imported_part_evidence(self.imported_rows(), expected, main_enabled=True)["ok"])
        self.assertFalse(imported_part_evidence(self.imported_rows("其他零件"), expected, main_enabled=True)["ok"])
        self.assertFalse(imported_part_evidence(self.imported_rows()[:2], expected, main_enabled=True)["ok"])
        self.assertFalse(imported_part_evidence(self.imported_rows(), expected, main_enabled=False)["ok"])
        self.assertFalse(imported_part_evidence(self.imported_rows(), expected, main_enabled=True, secondary_windows=[55])["ok"])

    def test_import_without_confirmation_requires_stable_named_geometry(self):
        session = SimpleNamespace(pid=1, hwnd=2)
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(part.ui, "windows", return_value=[SimpleNamespace(hwnd=2, visible=True, enabled=True)]), \
             patch.object(part, "_page", return_value=object()), \
             patch.object(part, "_rows", return_value=self.imported_rows()), \
             patch.object(part, "_import_confirmation") as confirmation, \
             patch.object(part.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(part.time, "sleep", side_effect=sleep):
            result = part._wait_import_completion(session, ["抱箍-基体-法兰1"], timeout=3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["completion_mode"], "named_part_without_confirmation")
        self.assertGreaterEqual(result["stable_seconds"], .4)
        confirmation.assert_not_called()

    def test_degree_fields_exclude_translation_mm(self):
        rows = angle_rows("−180 °", "1 deg", "2 °")
        rows.append({"type": "SpinnerControl", "name": "X", "value": "52 mm"})
        result = angles_from_rows(rows)
        self.assertTrue(result["ok"])
        self.assertEqual(result["angles_deg"], {"X": -180, "Y'": 1, "Z''": 2})

    def test_conflicting_angle_fields_are_ambiguous(self):
        rows = angle_rows()
        rows.append({"type": "SpinnerControl", "name": "X", "value": "10 °"})
        result = angles_from_rows(rows)
        self.assertFalse(result["ok"])
        self.assertEqual(result["ambiguous"], ["X"])

    def test_reversal_requires_actual_command_and_angle_change(self):
        before = {"ok": True, "angles_deg": {"X": 0, "Y'": 0, "Z''": 0}}
        after = {"ok": True, "angles_deg": {"X": -180, "Y'": 0, "Z''": 0}, "direction_label": "反向"}
        self.assertTrue(reversal_confirmed(before, after))
        self.assertFalse(reversal_confirmed(before, {**after, "direction_label": "自动设置"}))
        self.assertFalse(reversal_confirmed(before, {**after, "angles_deg": before["angles_deg"]}))

    def test_persistence_compares_wrapped_angles_not_last_command_label(self):
        observed = {"ok": True, "angles_deg": {"X": 180, "Y'": 360, "Z''": 0}, "direction_label": "设置"}
        self.assertTrue(persisted_direction_matches(observed, {"X": -180, "Y'": 0, "Z''": 0})["ok"])
        self.assertFalse(persisted_direction_matches(observed, {"X": math.nan, "Y'": 0, "Z''": 0})["ok"])
        self.assertFalse(persisted_direction_matches(observed, None)["ok"])

    def test_part_count_absence_and_duplicates_are_unknown(self):
        self.assertIsNone(part_count_from_rows([])["count"])
        row = {"type": "TextControl", "name": "0 零件(s)", "rect": [0, 0, 100, 20], "enabled": True}
        self.assertEqual(part_count_from_rows([row])["count"], 0)
        self.assertIsNone(part_count_from_rows([row, dict(row)])["count"])

    def test_hidden_old_workspace_cannot_supply_part_count(self):
        row = {"type": "TextControl", "name": "1 零件(s)", "rect": [-500, 0, -100, 20], "enabled": True}
        self.assertIsNone(part_count_from_rows([row], [0, 0, 1920, 1080])["count"])

    def test_material_requires_observed_matching_thickness(self):
        self.assertFalse(material_matches([], "S355MC_4936_4.00", 4)["ok"])
        row = {"name": "S355MC_4936_4.00/4.000 mm"}
        self.assertTrue(material_matches([row], "S355MC_4936_4.00", 4)["ok"])
        self.assertFalse(material_matches([row], "S355MC_4936_4.00", 3)["ok"])
        self.assertFalse(material_matches([row], "OtherMaterial", 4)["ok"])

    def test_bending_requires_units_positive_results_and_context(self):
        self.assertTrue(bending_result_evidence(bending_rows())["ok"])
        for weight in ("0 kg", "-2 kg", "NaN kg", "2.5", "2.5 mm"):
            with self.subTest(weight=weight):
                self.assertFalse(bending_result_evidence(bending_rows(weight=weight))["ok"])
        self.assertFalse(bending_result_evidence(bending_rows()[:-1])["ok"])

    def test_bending_placeholder_and_duplicate_are_not_completion(self):
        result = bending_result_evidence(bending_rows("- kg", "- kN"))
        self.assertTrue(result["both_placeholders"])
        self.assertFalse(result["ok"])
        rows = bending_rows()
        rows.append(dict(rows[0]))
        self.assertFalse(bending_result_evidence(rows)["ok"])


if __name__ == "__main__":
    unittest.main()
