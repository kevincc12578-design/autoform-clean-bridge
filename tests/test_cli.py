"""CLI/API boundary tests. Factories are replaced before any Windows import."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from af_r13_bridge import __main__ as cli
from af_r13_bridge.api import Bridge
from test_lifecycle import MemoryAdapter


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "new-run"
        self.adapter = MemoryAdapter()
        self.audit = {"ok": True, "findings": [], "verification_scope": "offline fixture", "project_root": "独立新库"}
        self.factory_patch = patch.object(cli, "_adapter_factory", return_value=self.adapter)
        self.factory = self.factory_patch.start()
        self.report_patch = patch.object(cli, "isolation_report", return_value=self.audit)
        self.report = self.report_patch.start()
        self.root_patch = patch.object(cli, "_project_root", return_value=self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.report_patch.stop()
        self.factory_patch.stop()
        self.temp.cleanup()

    def run_cli(self, arguments):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = cli.main(arguments)
        return code, json.loads(stream.getvalue())

    def request(self, body):
        path = self.root / "request.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        return str(path)

    def test_no_live_flag_cannot_create_adapter_or_output(self):
        code, result = self.run_cli(["lifecycle", "--out", str(self.output)])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "live_opt_in_required")
        self.factory.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_request_json_cannot_grant_live_authorization(self):
        path = self.request({"out": str(self.output), "live": True})
        code, result = self.run_cli(["call", "lifecycle", "--request", path])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "live_opt_in_required")
        self.factory.assert_not_called()

    def test_standalone_mutation_requires_same_session_batch(self):
        path = self.request({"out": str(self.output)})
        code, result = self.run_cli(["call", "create_design", "--live", "--request", path])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "session_required")
        self.factory.assert_not_called()

    def test_unimplemented_business_method_reports_implementation_gap(self):
        path = self.request({})
        code, result = self.run_cli(["call", "optimize_springback", "--request", path])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "not_implemented")
        self.factory.assert_not_called()

    def test_existing_output_is_never_overwritten(self):
        self.output.mkdir()
        marker = self.output / "result.json"
        marker.write_text("previous evidence", encoding="utf-8")
        code, result = self.run_cli(["lifecycle", "--live", "--out", str(self.output)])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "output_exists")
        self.assertEqual(marker.read_text(), "previous evidence")
        self.factory.assert_not_called()

    def test_failed_isolation_blocks_startup_and_writes_failure(self):
        self.report.return_value = {"ok": False, "findings": [{"code": "reference_module_name"}]}
        code, result = self.run_cli(["lifecycle", "--live", "--out", str(self.output)])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "isolation_failed")
        self.factory.assert_not_called()
        self.assertTrue((self.output / "result.json").is_file())

    def test_constructor_failure_still_writes_json_result(self):
        self.factory.side_effect = RuntimeError("R13 startup did not complete")
        code, result = self.run_cli(["lifecycle", "--live", "--out", str(self.output)])
        self.assertEqual(code, 1)
        self.assertIn("startup", result["error"]["message"])
        saved = json.loads((self.output / "result.json").read_text(encoding="utf-8"))
        self.assertFalse(saved["ok"])
        self.assertTrue((self.output / "isolation-after.json").is_file())

    def test_lifecycle_success_has_same_file_and_no_e2e_claim(self):
        code, result = self.run_cli(["lifecycle", "--live", "--out", str(self.output)])
        self.assertEqual(code, 0)
        self.assertTrue(result["data"]["lifecycle_ok"])
        self.assertFalse(result["data"]["e2e_ok"])
        self.assertEqual(result["data"]["owned_documents_remaining"], 0)
        self.assertTrue(self.adapter.stopped)
        self.assertEqual(self.report.call_count, 2)
        self.assertEqual(json.loads((self.output / "result.json").read_text(encoding="utf-8"))["request_id"], result["request_id"])

    def test_final_loaded_module_audit_can_invalidate_success(self):
        self.report.side_effect = [self.audit, {"ok": False, "findings": [{"code": "reference_loaded_module"}]}]
        code, result = self.run_cli(["lifecycle", "--live", "--out", str(self.output)])
        self.assertEqual(code, 1)
        self.assertFalse(result["data"]["lifecycle_ok"])
        self.assertTrue(self.adapter.stopped)
        self.assertEqual(result["error"]["code"], "isolation_failed")

    def test_call_lifecycle_uses_the_same_api_and_passes_launch_options(self):
        path = self.request({"out": str(self.output), "filename": "small.afd"})
        code, result = self.run_cli(["call", "lifecycle", "--live", "--request", path, "--launch", "--license-server", "local-fixture"])
        self.assertEqual(code, 0)
        self.assertEqual(result["operation"], "lifecycle")
        self.assertTrue((self.output / "small.afd").is_file())
        self.factory.assert_called_once_with(self.output, launch=True, license_server="local-fixture", allow_selected_license=False)

    def test_batch_document_alias_keeps_current_handle_after_save(self):
        path = self.request({"out": str(self.output), "operations": [
            {"operation": "attach"},
            {"operation": "create_design", "as": "work"},
            {"operation": "save_design", "request": {"document_id": "@work", "path": "batch.afd"}},
            {"operation": "close_design", "request": {"document_id": "@work"}},
            {"operation": "reopen_design", "as": "reopened", "request": {"path": "batch.afd"}},
            {"operation": "readback", "request": {"document_id": "@reopened"}},
        ]})
        code, result = self.run_cli(["batch", "--live", "--request", path])
        self.assertEqual(code, 0)
        self.assertTrue(result["data"]["cleanup"]["cleanup_ok"])
        self.assertEqual(set(self.adapter.documents), {"user:1"})
        self.assertTrue(self.adapter.stopped)
        self.assertNotEqual(result["data"]["results"][1]["document_id"], result["data"]["results"][4]["document_id"])

    def test_batch_failure_stops_sequence_preserves_draft_and_disconnects(self):
        path = self.request({"out": str(self.output), "operations": [
            {"operation": "attach"},
            {"operation": "create_design", "as": "work"},
            {"operation": "save_design", "request": {"document_id": "@missing", "path": "draft.afd"}},
            {"operation": "create_design"},
        ]})
        code, result = self.run_cli(["batch", "--live", "--request", path])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "unknown_document_reference")
        self.assertTrue(result["data"]["cleanup"]["cleanup_ok"])
        self.assertEqual([item[0] for item in self.adapter.actions].count("create"), 1)
        self.assertEqual(len(list((self.output / "diagnostics").glob("*.afd"))), 1)
        self.assertTrue(self.adapter.stopped)

    def test_audit_and_capabilities_are_offline(self):
        code, result = self.run_cli(["audit", "--out", str(self.output)])
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["project_root"], "独立新库")
        code, result = self.run_cli(["capabilities"])
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["solver"], "implemented; validation is run-scoped")
        self.assertIn('solve', result['data']['business_operations'])
        self.factory.assert_not_called()

    def test_discover_is_explicitly_read_only(self):
        with patch.object(cli, "_discover", return_value=[{"pid": 100, "windows": []}]) as discover:
            code, result = self.run_cli(["discover"])
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["processes"][0]["pid"], 100)
        discover.assert_called_once_with()
        self.factory.assert_not_called()

    def test_api_refuses_user_baseline_document_id(self):
        bridge = Bridge(self.adapter, self.output)
        self.assertTrue(bridge.call("attach")["ok"])
        result = bridge.call("close_design", {"document_id": "user:1"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_document_handle")
        self.assertIn("user:1", self.adapter.documents)
        bridge.call("disconnect")


if __name__ == "__main__":
    unittest.main()
