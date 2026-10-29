"""Ownership-first lifecycle; no Windows imports or UI side effects at import.

The adapter performs bounded actions and waits for the actual application to
settle. This layer independently checks the resulting inventory and identities.
It never closes an unregistered design and never selects a discard action.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from .models import BridgeError, Design, Result


class Adapter(Protocol):
    def inventory(self) -> list[Design]: ...
    def create(self) -> Design: ...
    def save(self, design: Design, path: Path) -> Design: ...
    def close(self, design: Design) -> None: ...
    def open(self, path: Path) -> Design: ...
    def readback(self, design: Design) -> dict[str, Any]: ...
    def metrics(self) -> dict[str, Any]: ...
    def shutdown(self) -> None: ...


def canonical_path(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


class Lifecycle:
    """One run owns at most one design; user baseline designs remain untouched.

    All write methods require a live, current ``Design`` returned by this object.
    The UI adapter must reject unrecognized modal dialogs and unknown targets.
    ``disconnect`` stops this bridge's workers; it never exits AutoForm.
    """

    def __init__(self, adapter: Adapter, run_dir: str | Path):
        self.adapter = adapter
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid.uuid4().hex
        self.baseline: dict[str, Design] | None = None
        self.owned: dict[str, Design] = {}
        self.retired: set[str] = set()
        self.saved: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.evidence_errors: list[dict[str, Any]] = []
        self.resource_samples: list[dict[str, Any]] = []
        self.blocked = False
        self.disconnected = False
        self._started = time.monotonic()

    def _event(self, operation: str, **data: Any) -> None:
        event = {
            "run_id": self.run_id,
            "sequence": len(self.events) + 1,
            "monotonic_ms": round((time.monotonic() - self._started) * 1000, 3),
            "operation": operation,
            **data,
        }
        self.events.append(event)
        try:
            with (self.run_dir / "lifecycle-events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as error:
            # Losing evidence is a failed run, but must never skip worker shutdown.
            self.evidence_errors.append({"operation": operation, "message": str(error)})
            self.blocked = True

    def _inventory(self) -> dict[str, Design]:
        items = self.adapter.inventory()
        found = {design.token: design for design in items}
        if len(found) != len(items) or any(not token for token in found):
            raise BridgeError("ambiguous_inventory", "Design tokens must be unique and nonempty.")
        return found

    def _assert_baseline(self, current: dict[str, Design]) -> None:
        if self.baseline is None:
            raise BridgeError("not_attached", "Attach and snapshot the user baseline first.")
        missing = set(self.baseline) - set(current)
        if missing:
            self.blocked = True
            raise BridgeError("baseline_changed", "An original user design disappeared.", missing=sorted(missing))
        for token, original in self.baseline.items():
            actual = current[token]
            if original.path != actual.path or original.name != actual.name or original.dirty != actual.dirty:
                self.blocked = True
                raise BridgeError("baseline_changed", "A user design changed during this run.", token=token)

    def _ready(self) -> None:
        if self.disconnected:
            raise BridgeError("disconnected", "The bridge has disconnected.")
        if self.blocked:
            raise BridgeError("cleanup_required", "The previous operation failed; no new work may be created.")
        if self.baseline is None:
            raise BridgeError("not_attached", "Attach before changing designs.")

    def attach(self) -> list[Design]:
        if self.disconnected:
            raise BridgeError("disconnected", "Cannot reattach a disconnected lifecycle.")
        if self.baseline is not None:
            raise BridgeError("already_attached", "The baseline is immutable for this lifecycle.")
        self.baseline = self._inventory()
        self._event("attach", baseline=[asdict(item) for item in self.baseline.values()])
        self._sample("start", self.baseline)
        return list(self.baseline.values())

    def _sample(self, stage: str, inventory: dict[str, Design] | None = None) -> None:
        current = self._inventory() if inventory is None else inventory
        try:
            measured = self.adapter.metrics()
        except Exception as error:
            measured = {"working_set_bytes": None, "private_bytes": None, "error": str(error)}
        sample = {**measured, "stage": stage, "design_count": len(current), "owned_count": len(set(current) & set(self.owned))}
        self.resource_samples.append(sample)
        self._event("resource_sample", sample=sample)

    def _require_owned(self, design: Design) -> Design:
        if self.baseline is None or design.token in self.baseline or design.token not in self.owned:
            raise BridgeError("not_owned", "Only a currently registered run-owned design may be changed.", token=design.token)
        if design.token in self.retired or design != self.owned[design.token]:
            raise BridgeError("stale_handle", "This design handle is no longer current.", token=design.token)
        current = self._inventory()
        self._assert_baseline(current)
        if design.token not in current:
            self.blocked = True
            raise BridgeError("design_missing", "The owned design disappeared before the operation.", token=design.token)
        actual = current[design.token]
        if design.path and (not actual.path or canonical_path(actual.path) != canonical_path(design.path)):
            self.blocked = True
            raise BridgeError("identity_changed", "The live design path no longer matches its owned handle.")
        return actual

    def _adopt_delta(self, before: dict[str, Design], returned: Design, operation: str) -> Design:
        after = self._inventory()
        self._assert_baseline(after)
        new_tokens = set(after) - set(before)
        removed = set(before) - set(after)
        if removed or new_tokens != {returned.token} or returned.token in self.retired:
            self.blocked = True
            self._event("ownership_ambiguous", new_tokens=sorted(new_tokens), removed=sorted(removed), returned=asdict(returned))
            raise BridgeError("ownership_ambiguous", "Exactly one new, never-reused design identity is required.")
        actual = after[returned.token]
        self.owned[actual.token] = actual
        self._event(operation, document_id=actual.token, design=asdict(actual), before_tokens=sorted(before), after_tokens=sorted(after))
        self._sample(operation, after)
        return actual

    def create_design(self) -> Design:
        self._ready()
        if self.owned:
            raise BridgeError("owned_limit", "Close the current owned design before creating another.")
        before = self._inventory()
        self._assert_baseline(before)
        self._assert_known_inventory(before)
        try:
            returned = self.adapter.create()
            return self._adopt_delta(before, returned, "create_design")
        except (Exception, KeyboardInterrupt) as error:
            self._record_uncertain_mutation("create_design", before, error)
            raise

    def _assert_known_inventory(self, current: dict[str, Design]) -> None:
        unknown = set(current) - set(self.baseline or {}) - set(self.owned)
        if unknown:
            self.blocked = True
            raise BridgeError("unregistered_designs", "Unregistered designs require explicit ownership resolution before more work.", tokens=sorted(unknown))

    def _record_uncertain_mutation(self, operation: str, before: dict[str, Design], error: BaseException) -> None:
        """An exception is not proof that UI input had no effect.

        Even one new tab is not automatically claimed: the adapter has not
        supplied a successful identity binding. Preserve it and stop writes.
        """
        self.blocked = True
        try:
            current = self._inventory()
            added = [asdict(current[token]) for token in sorted(set(current) - set(before))]
            removed = sorted(set(before) - set(current))
        except (Exception, KeyboardInterrupt) as inspection_error:
            self._event("uncertain_mutation", attempted_operation=operation, error=self._error(error), inventory_error=self._error(inspection_error), unknown_documents_remaining=None)
        else:
            self._event("uncertain_mutation", attempted_operation=operation, error=self._error(error), unregistered_additions=added, removed_tokens=removed, current_designs=[asdict(item) for item in current.values()], unknown_documents_remaining=len(added))

    def _target(self, path: str | Path, *, must_exist: bool) -> Path:
        target = Path(path).resolve()
        if not target.is_relative_to(self.run_dir) or target == self.run_dir:
            raise BridgeError("path_not_owned", "Working files must stay inside this run directory.")
        if target.suffix.lower() != ".afd":
            raise BridgeError("invalid_extension", "A lifecycle work file must have an .afd extension.")
        if must_exist and not target.is_file():
            raise BridgeError("file_missing", "The recorded working file is missing.")
        return target

    def save_design(self, design: Design, path: str | Path) -> Design:
        actual = self._require_owned(design)
        target = self._target(path, must_exist=False)
        same_owned_file = actual.path is not None and canonical_path(actual.path) == canonical_path(target)
        previously_saved = canonical_path(target) in self.saved
        if target.exists() and not (same_owned_file and previously_saved):
            raise BridgeError("overwrite_refused", "An existing unregistered file must not be overwritten.")
        target.parent.mkdir(parents=True, exist_ok=True)
        before = self._inventory()
        was_blocked = self.blocked
        self.blocked = True
        try:
            returned = self.adapter.save(actual, target)
        except (Exception, KeyboardInterrupt) as error:
            self._record_uncertain_mutation("save_design", before, error)
            raise
        current = self._inventory()
        self._assert_baseline(current)
        live = current.get(actual.token)
        if returned.token != actual.token or live is None:
            raise BridgeError("save_identity_changed", "Saving changed or lost the owned design identity.")
        if not live.path or canonical_path(live.path) != canonical_path(target):
            raise BridgeError("save_path_unverified", "The current design's actual saved path is unverified.")
        if not returned.path or canonical_path(returned.path) != canonical_path(target):
            raise BridgeError("save_path_unverified", "The save operation did not confirm the requested path.")
        if live.dirty is True or returned.dirty is True:
            raise BridgeError("save_incomplete", "The design still reports unsaved changes.")
        if live.dirty is None and not (live.metadata.get("save_verified") is True or returned.metadata.get("save_verified") is True):
            raise BridgeError("save_state_unknown", "Unknown dirty state needs independent save-completion evidence.")
        if not target.is_file() or target.stat().st_size == 0:
            raise BridgeError("save_file_missing", "The application's saved file is missing or empty.")
        # Keep adapter's confirmed metadata without replacing the live identity.
        verified = Design(live.token, live.name, live.path, live.dirty, {**live.metadata, **returned.metadata})
        self.owned[verified.token] = verified
        file_record = {"path": str(target), "sha256": digest(target), "size": target.stat().st_size}
        self.saved[canonical_path(target)] = file_record
        self._event("save_design", document_id=verified.token, file=file_record, dirty=verified.dirty, save_verified=True)
        self.blocked = was_blocked or bool(self.evidence_errors)
        return verified

    def close_design(self, design: Design) -> None:
        actual = self._require_owned(design)
        if not actual.path or canonical_path(actual.path) not in self.saved:
            raise BridgeError("unsaved_close_refused", "Save and verify this owned design before closing it.")
        if actual.dirty is True:
            raise BridgeError("dirty_close_refused", "Changes appeared after saving; save again before closing.")
        if actual.dirty is None and not (actual.metadata.get("save_verified") is True or design.metadata.get("save_verified") is True):
            raise BridgeError("unknown_close_state", "Closing an unknown save state is not permitted.")
        before = self._inventory()
        was_blocked = self.blocked
        self.blocked = True
        try:
            self.adapter.close(actual)
        except (Exception, KeyboardInterrupt) as error:
            self._record_uncertain_mutation("close_design", before, error)
            raise
        after = self._inventory()
        self._assert_baseline(after)
        if design.token in after:
            self.blocked = True
            raise BridgeError("close_unverified", "The owned design is still present after close.")
        if set(before) - set(after) != {design.token} or set(after) - set(before):
            self.blocked = True
            raise BridgeError("close_inventory_changed", "Closing the owned design changed other design identities.")
        del self.owned[design.token]
        self.retired.add(design.token)
        self._event("close_design", document_id=design.token, close_verified=True, remaining_tokens=sorted(after))
        self._sample("after_close", after)
        self.blocked = was_blocked or bool(self.evidence_errors)

    def register_business_save(self, design: Design, path: str | Path) -> Design:
        """Register only a Start save transaction independently checked by adapter.

        This narrow path does not allow ordinary save to overwrite existing files.
        The normal save operation remains required before workflow final closure.
        """
        actual = self._require_owned(design)
        target = self._target(path, must_exist=True)
        proof = actual.metadata.get('save_transaction',{})
        rid = actual.metadata.get('runtime_id')
        if (not proof.get('ok') or proof.get('before_rid')!=rid or proof.get('after_rid')!=rid
                or not actual.path or canonical_path(actual.path)!=canonical_path(target)
                or canonical_path(proof.get('path',''))!=canonical_path(target)
                or proof.get('path_readback')!=str(target) or proof.get('sha256')!=digest(target)
                or not actual.metadata.get('save_verified')):
            raise BridgeError('business_save_unverified','GUI Start save evidence did not pass independent lifecycle checks')
        self.saved[canonical_path(target)]={'path':str(target),'sha256':digest(target),'size':target.stat().st_size}
        self.owned[actual.token]=actual
        self._event('business_save_registered',document_id=actual.token,file=self.saved[canonical_path(target)],transaction=proof)
        return actual

    def reopen_design(self, path: str | Path) -> Design:
        self._ready()
        if self.owned:
            raise BridgeError("close_required", "The original owned design must close before reopening the file.")
        target = self._target(path, must_exist=True)
        record = self.saved.get(canonical_path(target))
        if record is None:
            raise BridgeError("file_not_registered", "Only a file saved by this lifecycle may be reopened.")
        if digest(target) != record["sha256"]:
            raise BridgeError("file_changed", "The working file changed after the verified save.")
        before = self._inventory()
        self._assert_baseline(before)
        self._assert_known_inventory(before)
        try:
            returned = self.adapter.open(target)
            opened = self._adopt_delta(before, returned, "reopen_design")
        except (Exception, KeyboardInterrupt) as error:
            self._record_uncertain_mutation("reopen_design", before, error)
            raise
        if not opened.path or canonical_path(opened.path) != canonical_path(target):
            self.blocked = True
            raise BridgeError("reopen_path_mismatch", "The reopened design is not the recorded saved file.")
        self._event("same_file_opened", document_id=opened.token, path=str(target), sha256=record["sha256"])
        return opened

    def readback(self, design: Design) -> dict[str, Any]:
        actual = self._require_owned(design)
        values = self.adapter.readback(actual)
        if not isinstance(values, dict) or not values or all(value is None for value in values.values()):
            raise BridgeError("empty_readback", "Persistence verification requires observed semantic values.")
        self._event("readback", document_id=design.token, values=values)
        return values

    def cleanup(self) -> dict[str, Any]:
        """Save uncertain run-owned content to a fresh diagnostic file, then close.

        No attempt is made to claim or close ambiguous/unregistered UI additions.
        An unknown modal remains a failure for the adapter to report and preserve.
        """
        errors: list[dict[str, Any]] = []
        for token, design in list(self.owned.items()):
            try:
                actual = self._require_owned(design)
                known_saved = actual.path and canonical_path(actual.path) in self.saved
                if actual.dirty is not False or not known_saved:
                    diagnostic = self.run_dir / "diagnostics" / f"preserved-{uuid.uuid4().hex}.afd"
                    design = self.save_design(design, diagnostic)
                self.close_design(design)
            except (Exception, KeyboardInterrupt) as error:
                errors.append(self._error(error))
        try:
            current = self._inventory()
            self._assert_baseline(current)
            remaining = len(set(self.owned) & set(current))
            unregistered = sorted(set(current) - set(self.baseline or {}) - set(self.owned))
            unknown_remaining = len(unregistered)
            if self.owned:
                # A registered but unexpectedly missing tab is not verified cleanup.
                errors.append({"code": "unconfirmed_ownership", "tokens": sorted(self.owned)})
            if unregistered:
                errors.append({"code": "unregistered_designs", "tokens": unregistered, "designs": [asdict(current[token]) for token in unregistered]})
            self._sample("end", current)
        except (Exception, KeyboardInterrupt) as error:
            remaining = None
            unknown_remaining = None
            errors.append(self._error(error))
        cleanup_ok = not errors and remaining == 0
        if not cleanup_ok:
            self.blocked = True
        outcome = {"cleanup_ok": cleanup_ok, "owned_documents_remaining": remaining, "unknown_documents_remaining": unknown_remaining, "errors": errors}
        self._event("cleanup", **outcome)
        return outcome

    @staticmethod
    def _error(error: BaseException) -> dict[str, Any]:
        if isinstance(error, BridgeError):
            return error.to_dict()
        if isinstance(error, KeyboardInterrupt):
            return {"code": "cancelled", "message": "Lifecycle cancelled; cleanup was attempted."}
        return {"code": "adapter_error", "type": type(error).__name__, "message": str(error)}

    def disconnect(self) -> None:
        """Stop bridge workers only; never close designs or exit the application."""
        if not self.disconnected:
            self.adapter.shutdown()
            self.disconnected = True
            self._event("disconnect", application_exit_requested=False)

    def run(self, filename: str = "lifecycle.afd") -> dict[str, Any]:
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        gates: dict[str, Any] = {
            "save_verified": False,
            "close_verified": False,
            "reopen_same_file_verified": False,
            "readback_verified": False,
            "final_close_verified": False,
            "cleanup_ok": False,
            "owned_documents_remaining": None,
            "unknown_documents_remaining": None,
            "baseline_preserved": False,
            "workers_stopped": False,
        }
        error: dict[str, Any] | None = None
        final_path: str | None = None
        before_values: dict[str, Any] | None = None
        after_values: dict[str, Any] | None = None
        try:
            if self.baseline is None:
                self.attach()
            self._ready()
            target = self._target(self.run_dir / filename, must_exist=False)
            if target.exists():
                raise BridgeError("overwrite_refused", "Lifecycle output already exists; choose a fresh run directory.")
            design = self.create_design()
            before_values = self.readback(design)
            design = self.save_design(design, target)
            final_path = str(target)
            gates["save_verified"] = True
            self.close_design(design)
            gates["close_verified"] = True
            reopened = self.reopen_design(target)
            gates["reopen_same_file_verified"] = True
            after_values = self.readback(reopened)
            if before_values != after_values:
                raise BridgeError("persistence_mismatch", "Semantic readback differs after reopening the same file.", before=before_values, after=after_values)
            gates["readback_verified"] = True
            # Save again through the application's normal save path. This also
            # handles editors that mark a document dirty merely after opening it.
            reopened = self.save_design(reopened, target)
            self.close_design(reopened)
            gates["final_close_verified"] = True
        except (Exception, KeyboardInterrupt) as caught:
            self.blocked = True
            error = self._error(caught)
            self._event("failure", error=error)
        finally:
            try:
                cleaned = self.cleanup()
                gates["cleanup_ok"] = cleaned["cleanup_ok"]
                gates["owned_documents_remaining"] = cleaned["owned_documents_remaining"]
                gates["unknown_documents_remaining"] = cleaned["unknown_documents_remaining"]
                if cleaned["errors"] and error is None:
                    error = {"code": "cleanup_failed", "details": cleaned["errors"]}
                try:
                    self._assert_baseline(self._inventory())
                    gates["baseline_preserved"] = True
                except (Exception, KeyboardInterrupt) as caught:
                    error = error or self._error(caught)
            except (Exception, KeyboardInterrupt) as caught:
                error = error or self._error(caught)
                self.blocked = True
            finally:
                try:
                    self.disconnect()
                    gates["workers_stopped"] = True
                except (Exception, KeyboardInterrupt) as caught:
                    error = error or self._error(caught)
        if self.evidence_errors:
            error = error or {"code": "evidence_write_failed", "details": self.evidence_errors}
        count_gates = {"owned_documents_remaining", "unknown_documents_remaining"}
        lifecycle_ok = error is None and all(value is True for key, value in gates.items() if key not in count_gates) and all(gates[key] == 0 for key in count_gates)
        data = {
            "run_id": self.run_id,
            "lifecycle_ok": lifecycle_ok,
            "e2e_ok": False,
            "solver_verified": False,
            "final_file": final_path,
            "before_readback": before_values,
            "after_readback": after_values,
            "resources": self.resource_samples,
            "resource_peak_scope": "Maximum of recorded lifecycle checkpoints, not continuous memory monitoring.",
            "peak_design_count": max((item["design_count"] for item in self.resource_samples), default=None),
            "peak_owned_count": max((item["owned_count"] for item in self.resource_samples), default=None),
            **gates,
        }
        result = Result(
            ok=lifecycle_ok,
            operation="lifecycle",
            request_id=request_id,
            data=data,
            verification_scope="Owned small-design lifecycle and semantic persistence only; no forming or solver acceptance.",
            postconditions=gates,
            evidence=[str(self.run_dir / "lifecycle-events.jsonl")],
            error=error,
            elapsed_ms=round((time.monotonic() - started) * 1000, 3),
        ).to_dict()
        try:
            (self.run_dir / "lifecycle-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as caught:
            result["ok"] = False
            result["data"]["lifecycle_ok"] = False
            result["error"] = result["error"] or {"code": "result_write_failed", "message": str(caught)}
        return result
