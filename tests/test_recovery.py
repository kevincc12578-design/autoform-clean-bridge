"""Recovery failure-injection tests using a document store, never live UI."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from dataclasses import replace

from af_r13_bridge.models import BridgeError, Design
from af_r13_bridge.recovery import active_ledger, recover_recorded_run


def document(token, name, identity):
    return Design(token, name, None, None, {
        "runtime_id": [42, identity], "button_rid": [42, identity + 1],
        "close_rid": [42, identity + 2]})


class Store:
    def __init__(self, documents):
        self.documents = {design.token: design for design in documents}
        self.session = {"pid": 100, "create_time": 123.5, "hwnd": 200}
        self._owned_tokens = set()
        self.actions = []
        self.shutdown_error = False
        self.close_noop = False
        self.supervisor = SimpleNamespace(evidence=SimpleNamespace(append=lambda *a, **k: None))

    def factory(self, out):
        self.actions.append("connect")
        return self

    def inventory(self):
        return list(self.documents.values())

    def save(self, design, path):
        self.actions.append(("save", design.token))
        path.write_bytes(b"independent diagnostic")
        saved = replace(design, name=path.stem, path=str(path))
        self.documents[design.token] = saved
        return saved

    def close(self, design):
        self.actions.append(("close", design.token))
        if not self.close_noop:
            del self.documents[design.token]

    def shutdown(self):
        self.actions.append("shutdown")
        if self.shutdown_error:
            raise RuntimeError("Worker termination unconfirmed")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous = self.root / "previous"
        self.previous.mkdir()
        self.output = self.root / "recovery"
        self.user = document("user", "Original user", 10)
        self.owned = document("owned", "Owned work", 20)
        self.store = Store([self.user, self.owned])
        self.events = []
        self.event("attach", baseline=[self.user.to_dict()])
        self.event("create_design", document_id="owned", design=self.owned.to_dict())

    def tearDown(self):
        self.temp.cleanup()

    def event(self, operation, **data):
        self.events.append({"run_id": "run-one", "sequence": len(self.events) + 1,
                            "operation": operation, **data})

    def run_recovery(self):
        (self.previous / "session.json").write_text(json.dumps(self.store.session), encoding="utf-8")
        (self.previous / "lifecycle-events.jsonl").write_text(
            "\n".join(json.dumps(event) for event in self.events), encoding="utf-8")
        (self.previous / "lifecycle-result.json").write_text('{"ok":false}', encoding="utf-8")
        return recover_recorded_run(self.previous, self.output, adapter_factory=self.store.factory)

    def test_active_owned_saved_closed_and_original_failure_unchanged(self):
        result = self.run_recovery()
        self.assertTrue(result["ok"])
        self.assertFalse(result["lifecycle_ok"])
        self.assertFalse(result["e2e_ok"])
        self.assertEqual(self.store.documents, {"user": self.user})
        self.assertEqual((self.previous / "lifecycle-result.json").read_text(), '{"ok":false}')

    def test_retired_runtime_id_reused_by_another_tab_is_never_owned(self):
        self.event("close_design", document_id="owned", close_verified=True)
        self.store.documents["owned"] = document("someone_else", "Owned work", 20)
        result = self.run_recovery()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "unknown_ownership")
        self.assertEqual(self.store.actions, ["connect", "shutdown"])

    def test_latest_reopen_is_active_but_first_closed_record_is_not(self):
        self.event("close_design", document_id="owned", close_verified=True)
        reopened = document("reopened", "Reopened", 30)
        self.event("reopen_design", document_id="reopened", design=reopened.to_dict())
        self.store.documents = {"user": self.user, "current": replace(reopened, token="current")}
        result = self.run_recovery()
        self.assertTrue(result["ok"])
        self.assertEqual(result["source_active_document_ids"], ["reopened"])
        self.assertEqual(result["source_retired_document_ids"], ["owned"])

    def test_baseline_rename_blocks_all_writes(self):
        self.store.documents["user"] = replace(self.user, name="User changed it")
        result = self.run_recovery()
        self.assertEqual(result["error"]["code"], "baseline_changed")
        self.assertEqual(self.store.actions, ["connect", "shutdown"])

    def test_unknown_addition_blocks_all_writes(self):
        self.store.documents["unknown"] = document("unknown", "Unregistered", 40)
        result = self.run_recovery()
        self.assertEqual(result["error"]["code"], "unknown_ownership")
        self.assertEqual(self.store.actions, ["connect", "shutdown"])

    def test_failed_save_snapshot_refreshes_known_name_without_adopting_new_token(self):
        latest = replace(self.owned, name="saved-but-operation-failed")
        self.event("uncertain_mutation", current_designs=[self.user.to_dict(), latest.to_dict()], removed_tokens=[])
        self.store.documents["owned"] = latest
        result = self.run_recovery()
        self.assertTrue(result["ok"])

    def test_uncertain_observed_disappearance_invalidates_old_identity(self):
        self.event("uncertain_mutation", current_designs=[self.user.to_dict()], removed_tokens=["owned"])
        result = self.run_recovery()
        self.assertEqual(result["error"]["code"], "unknown_ownership")
        self.assertEqual(self.store.actions, ["connect", "shutdown"])

    def test_changed_child_runtime_identity_blocks_reused_parent_id(self):
        metadata = {**self.owned.metadata, "close_rid": [42, 999]}
        self.store.documents["owned"] = replace(self.owned, metadata=metadata)
        result = self.run_recovery()
        self.assertEqual(result["error"]["code"], "owned_identity_changed")
        self.assertEqual(self.store.actions, ["connect", "shutdown"])

    def test_noop_close_fails_postcondition_and_still_shuts_down(self):
        self.store.close_noop = True
        result = self.run_recovery()
        self.assertFalse(result["cleanup_ok"])
        self.assertEqual(self.store.actions[-1], "shutdown")

    def test_shutdown_failure_still_writes_independent_failed_result(self):
        self.store.shutdown_error = True
        result = self.run_recovery()
        saved = json.loads((self.output / "recovery-result.json").read_text())
        self.assertFalse(result["ok"])
        self.assertFalse(saved["workers_stopped"])
        self.assertIn("shutdown_error", saved)

    def test_missing_source_evidence_does_not_construct_adapter(self):
        result = recover_recorded_run(self.previous, self.output, adapter_factory=self.store.factory)
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.actions, [])

    def test_sequence_gap_rejects_ledger_before_ui(self):
        self.events[-1]["sequence"] = 8
        result = self.run_recovery()
        self.assertEqual(result["error"]["code"], "invalid_ledger")
        self.assertEqual(self.store.actions, [])


if __name__ == "__main__":
    unittest.main()
