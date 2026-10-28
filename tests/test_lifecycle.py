"""Independent offline failure-injection tests; no live UI adapter is imported."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

from af_r13_bridge.lifecycle import Lifecycle
from af_r13_bridge.models import BridgeError, Design


class MemoryAdapter:
    """A deliberately small document store, independent of AutoForm internals."""

    def __init__(self):
        self.documents = {"user:1": Design("user:1", "User design", "C:/user/original.afd", True)}
        self.values = {"user:1": {"part_count": 5}}
        self.generation = 0
        self.actions = []
        self.stopped = False
        self.close_noop = False
        self.save_unknown = False
        self.change_on_reopen = False
        self.extra_created = False
        self.fail_readback_once = False
        self.cancel_readback = False
        self.reuse_token = False
        self.fail_after_create = False
        self.fail_after_open = False

    def inventory(self):
        return list(self.documents.values())

    def _new(self, path=None, dirty=True):
        self.generation += 1
        token = f"owned:{self.generation}" if not self.reuse_token else "owned:reused"
        design = Design(token, Path(path).stem if path else "Untitled", str(path) if path else None, dirty)
        self.documents[token] = design
        self.values[token] = {"part_count": 0, "project_kind": "forming"}
        return design

    def create(self):
        self.actions.append(("create", None))
        design = self._new()
        if self.extra_created:
            self._new()
        if self.fail_after_create:
            raise TimeoutError("Creation observation timed out after UI input")
        return design

    def save(self, design, path):
        self.actions.append(("save", design.token, str(path)))
        path.write_text(json.dumps(self.values[design.token]), encoding="utf-8")
        dirty = None if self.save_unknown else False
        updated = replace(design, path=str(path), name=path.stem, dirty=dirty)
        self.documents[design.token] = updated
        return updated

    def close(self, design):
        self.actions.append(("close", design.token))
        if not self.close_noop:
            del self.documents[design.token]
            del self.values[design.token]

    def open(self, path):
        self.actions.append(("open", str(path)))
        design = self._new(path, False)
        self.values[design.token] = json.loads(path.read_text(encoding="utf-8"))
        if self.change_on_reopen:
            self.values[design.token]["part_count"] = 1
        if self.fail_after_open:
            raise TimeoutError("Open observation timed out after UI input")
        return design

    def readback(self, design):
        if self.cancel_readback:
            self.cancel_readback = False
            raise KeyboardInterrupt()
        if self.fail_readback_once:
            self.fail_readback_once = False
            raise TimeoutError("Readback probe did not return within its deadline")
        return dict(self.values[design.token])

    def metrics(self):
        return {"working_set_bytes": 1000 + 100 * len(self.documents), "private_bytes": 2000 + 100 * len(self.documents)}

    def shutdown(self):
        self.actions.append(("shutdown", None))
        self.stopped = True


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.adapter = MemoryAdapter()
        self.lifecycle = Lifecycle(self.adapter, self.directory)

    def tearDown(self):
        self.temp.cleanup()

    def assert_code(self, code, function, *args):
        with self.assertRaises(BridgeError) as raised:
            function(*args)
        self.assertEqual(raised.exception.code, code)

    def test_same_file_close_reopen_close_keeps_dirty_user_design(self):
        original = self.adapter.documents["user:1"]
        result = self.lifecycle.run()
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["e2e_ok"])
        self.assertEqual(result["data"]["owned_documents_remaining"], 0)
        self.assertEqual(result["data"]["peak_owned_count"], 1)
        self.assertEqual(self.adapter.documents, {"user:1": original})
        self.assertEqual([item[0] for item in self.adapter.actions], ["create", "save", "close", "open", "save", "close", "shutdown"])
        saved = self.adapter.actions[1][2]
        self.assertEqual(self.adapter.actions[3][1], saved)
        self.assertEqual(self.adapter.actions[4][2], saved)
        self.assertTrue(self.adapter.stopped)

    def test_user_baseline_cannot_be_saved_or_closed(self):
        user = self.lifecycle.attach()[0]
        self.assert_code("not_owned", self.lifecycle.save_design, user, self.directory / "user.afd")
        self.assert_code("not_owned", self.lifecycle.close_design, user)
        self.assertEqual(self.adapter.actions, [])

    def test_closed_handle_never_targets_a_new_document(self):
        self.lifecycle.attach()
        old = self.lifecycle.create_design()
        old = self.lifecycle.save_design(old, self.directory / "work.afd")
        self.lifecycle.close_design(old)
        reopened = self.lifecycle.reopen_design(self.directory / "work.afd")
        self.assertNotEqual(reopened.token, old.token)
        self.assert_code("not_owned", self.lifecycle.close_design, old)
        self.assertIn(reopened.token, self.adapter.documents)

    def test_noop_close_fails_and_blocks_further_creation(self):
        self.adapter.close_noop = True
        result = self.lifecycle.run()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "close_unverified")
        self.assertFalse(result["data"]["cleanup_ok"])
        self.assertEqual(result["data"]["owned_documents_remaining"], 1)
        self.assertNotIn("open", [item[0] for item in self.adapter.actions])
        self.assertTrue(self.adapter.stopped)
        self.assert_code("disconnected", self.lifecycle.create_design)

    def test_file_exists_without_save_state_is_not_success(self):
        self.adapter.save_unknown = True
        result = self.lifecycle.run()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "save_state_unknown")
        self.assertFalse(result["data"]["save_verified"])
        self.assertFalse(result["data"]["cleanup_ok"])
        self.assertNotIn("close", [item[0] for item in self.adapter.actions])

    def test_semantic_persistence_mismatch_cannot_pass_lifecycle(self):
        self.adapter.change_on_reopen = True
        result = self.lifecycle.run()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "persistence_mismatch")
        self.assertTrue(result["data"]["cleanup_ok"])
        self.assertFalse(result["data"]["readback_verified"])
        self.assertEqual(result["data"]["owned_documents_remaining"], 0)

    def test_timeout_preserves_unsaved_work_to_new_diagnostic_then_closes(self):
        self.adapter.fail_readback_once = True
        result = self.lifecycle.run()
        self.assertFalse(result["ok"])
        self.assertTrue(result["data"]["cleanup_ok"])
        diagnostics = list((self.directory / "diagnostics").glob("*.afd"))
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(json.loads(diagnostics[0].read_text())["part_count"], 0)
        self.assertEqual(set(self.adapter.documents), {"user:1"})
        self.assertTrue(self.adapter.stopped)

    def test_cancel_also_preserves_work_and_stops_only_bridge_workers(self):
        self.adapter.cancel_readback = True
        result = self.lifecycle.run()
        self.assertEqual(result["error"]["code"], "cancelled")
        self.assertTrue(result["data"]["cleanup_ok"])
        self.assertTrue(result["data"]["workers_stopped"])
        self.assertIn("user:1", self.adapter.documents)

    def test_multiple_new_tabs_are_ambiguous_and_never_auto_claimed(self):
        self.adapter.extra_created = True
        result = self.lifecycle.run()
        self.assertEqual(result["error"]["code"], "ownership_ambiguous")
        self.assertFalse(result["data"]["cleanup_ok"])
        self.assertEqual(len(self.adapter.documents), 3)
        self.assertNotIn("close", [item[0] for item in self.adapter.actions])

    def test_reused_runtime_token_is_rejected(self):
        self.adapter.reuse_token = True
        result = self.lifecycle.run()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "ownership_ambiguous")
        self.assertFalse(result["data"]["cleanup_ok"])

    def test_output_is_never_overwritten_and_parent_path_is_rejected(self):
        target = self.directory / "lifecycle.afd"
        target.write_bytes(b"already exists")
        result = self.lifecycle.run()
        self.assertFalse(result["ok"])
        self.assertEqual(target.read_bytes(), b"already exists")
        self.assertNotIn("create", [item[0] for item in self.adapter.actions])

    def test_path_escape_refused_before_save_action(self):
        self.lifecycle.attach()
        design = self.lifecycle.create_design()
        self.assert_code("path_not_owned", self.lifecycle.save_design, design, self.directory.parent / "outside.afd")
        self.assertEqual([item[0] for item in self.adapter.actions], ["create"])

    def test_external_file_mutation_prevents_reopen(self):
        self.lifecycle.attach()
        design = self.lifecycle.create_design()
        path = self.directory / "work.afd"
        design = self.lifecycle.save_design(design, path)
        self.lifecycle.close_design(design)
        path.write_bytes(b"changed externally")
        self.assert_code("file_changed", self.lifecycle.reopen_design, path)
        self.assertNotIn("open", [item[0] for item in self.adapter.actions])

    def test_dirty_changes_after_save_prevent_close(self):
        self.lifecycle.attach()
        design = self.lifecycle.create_design()
        design = self.lifecycle.save_design(design, self.directory / "work.afd")
        self.adapter.documents[design.token] = replace(design, dirty=True)
        self.assert_code("dirty_close_refused", self.lifecycle.close_design, design)
        self.assertIn(design.token, self.adapter.documents)

    def test_unchanged_file_size_and_timestamp_do_not_invalidate_save(self):
        self.lifecycle.attach()
        design = self.lifecycle.create_design()
        path = self.directory / "work.afd"
        design = self.lifecycle.save_design(design, path)
        stamp = path.stat().st_mtime_ns
        original_save = self.adapter.save

        def unchanged_save(item, target):
            self.adapter.actions.append(("save", item.token, str(target)))
            return item

        self.adapter.save = unchanged_save
        design = self.lifecycle.save_design(design, path)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.lifecycle.close_design(design)
        self.adapter.save = original_save

    def test_unknown_memory_values_remain_unknown(self):
        self.adapter.metrics = lambda: {"working_set_bytes": None, "private_bytes": None}
        result = self.lifecycle.run()
        self.assertTrue(result["ok"])
        self.assertTrue(all(sample["working_set_bytes"] is None for sample in result["data"]["resources"]))

    def test_create_then_timeout_blocks_public_api_and_does_not_claim_tab(self):
        self.adapter.fail_after_create = True
        self.lifecycle.attach()
        with self.assertRaises(TimeoutError):
            self.lifecycle.create_design()
        self.assertEqual(self.lifecycle.owned, {})
        self.assert_code("cleanup_required", self.lifecycle.create_design)
        cleaned = self.lifecycle.cleanup()
        self.assertFalse(cleaned["cleanup_ok"])
        self.assertEqual(cleaned["unknown_documents_remaining"], 1)
        self.assertEqual(len(self.adapter.documents), 2)
        self.assertNotIn("close", [item[0] for item in self.adapter.actions])

    def test_open_then_timeout_preserves_unknown_tab_and_prevents_next_round(self):
        self.adapter.fail_after_open = True
        result = self.lifecycle.run()
        self.assertFalse(result["ok"])
        self.assertFalse(result["data"]["cleanup_ok"])
        self.assertEqual(result["data"]["unknown_documents_remaining"], 1)
        self.assertEqual(result["data"]["owned_documents_remaining"], 0)
        self.assertEqual(len(self.adapter.documents), 2)
        self.assertEqual([item[0] for item in self.adapter.actions].count("close"), 1)
        self.assertTrue(self.adapter.stopped)

    def test_event_log_failure_still_stops_workers_and_cannot_pass(self):
        original = Path.open

        def fail_event_log(path, *args, **kwargs):
            if path.name == "lifecycle-events.jsonl":
                raise OSError("Simulated full evidence volume")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", fail_event_log):
            result = self.lifecycle.run()
        self.assertFalse(result["ok"])
        self.assertTrue(self.adapter.stopped)
        self.assertTrue(result["data"]["workers_stopped"])
        self.assertTrue(self.lifecycle.evidence_errors)

    def test_unexpected_cleanup_exception_does_not_skip_worker_shutdown(self):
        def failed_cleanup():
            raise RuntimeError("Unexpected cleanup exception")

        self.lifecycle.cleanup = failed_cleanup
        result = self.lifecycle.run()
        self.assertFalse(result["ok"])
        self.assertFalse(result["data"]["cleanup_ok"])
        self.assertTrue(result["data"]["workers_stopped"])
        self.assertTrue(self.adapter.stopped)


if __name__ == "__main__":
    unittest.main()
