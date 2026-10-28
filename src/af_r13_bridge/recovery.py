"""Explicit cleanup from the active ledger of the SAME R13 process.

Recovery never changes the original lifecycle result. Retired or unregistered
runtime IDs cannot grant ownership, even if the application later reuses them.
"""
from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

from .models import BridgeError


def _rid(design, key="runtime_id"):
    value = design.get("metadata", {}).get(key)
    if not isinstance(value, list) or not value or any(type(part) is not int for part in value):
        raise BridgeError("invalid_ledger", "Recorded UI identity is missing or invalid", field=key)
    return tuple(value)


def active_ledger(events):
    """Replay creates, verified saves, observations and closes in event order."""
    baseline, active, retired = None, {}, set()
    run_id = None
    for sequence, event in enumerate(events, 1):
        if event.get("sequence") != sequence or not event.get("run_id"):
            raise BridgeError("invalid_ledger", "Event sequence is incomplete or unordered")
        if run_id is None:
            run_id = event["run_id"]
        if event["run_id"] != run_id:
            raise BridgeError("invalid_ledger", "Multiple run identities in one ledger")
        operation = event.get("operation")
        if operation == "attach":
            if baseline is not None or sequence != 1:
                raise BridgeError("invalid_ledger", "Exactly one initial baseline is required")
            items = event["baseline"]
            baseline = {item["token"]: copy.deepcopy(item) for item in items}
            if len(baseline) != len(items):
                raise BridgeError("invalid_ledger", "Duplicate baseline identities")
        elif operation in {"create_design", "reopen_design"}:
            if baseline is None:
                raise BridgeError("invalid_ledger", "Ownership appeared before baseline")
            design = copy.deepcopy(event["design"])
            token = design["token"]
            if event.get("document_id") != token or token in baseline or token in active or token in retired:
                raise BridgeError("invalid_ledger", "Owned identity conflicts with prior ownership")
            _rid(design)
            active[token] = design
        elif operation == "save_design":
            token = event.get("document_id")
            if token not in active or event.get("save_verified") is not True:
                raise BridgeError("invalid_ledger", "Save does not refer to a live owned design")
            path = event["file"]["path"]
            active[token].update(path=path, name=Path(path).stem, dirty=event.get("dirty"))
        elif operation == "close_design":
            token = event.get("document_id")
            if token not in active or event.get("close_verified") is not True:
                raise BridgeError("invalid_ledger", "Close is not a verified active-owned close")
            del active[token]
            retired.add(token)
        elif operation == "uncertain_mutation":
            # Observation can refresh a known identity, never adopt a new tab.
            for token in event.get("removed_tokens", []):
                if token in active:
                    del active[token]
                    retired.add(token)
            for observed in event.get("current_designs", []):
                token = observed.get("token")
                if token in active:
                    if _rid(observed) != _rid(active[token]):
                        raise BridgeError("invalid_ledger", "A live token changed runtime identity")
                    active[token] = copy.deepcopy(observed)
    if baseline is None:
        raise BridgeError("invalid_ledger", "No initial baseline was recorded")
    for collection in (baseline, active):
        for design in collection.values():
            for key in ("runtime_id", "button_rid", "close_rid"):
                _rid(design, key)
    identities = [_rid(design) for design in [*baseline.values(), *active.values()]]
    if len(set(identities)) != len(identities):
        raise BridgeError("invalid_ledger", "Duplicate live runtime identities")
    return baseline, active, retired


def _error(error):
    if isinstance(error, BridgeError):
        return error.to_dict()
    return {"code": "cancelled" if isinstance(error, KeyboardInterrupt) else "recovery_error",
            "type": type(error).__name__, "message": str(error)}


def _inventory_matches(current, baseline, active):
    current_records = [design.to_dict() for design in current]
    by_rid = {_rid(design): design for design in current_records}
    if len(by_rid) != len(current_records):
        raise BridgeError("ambiguous_inventory", "Current runtime IDs are duplicated")
    expected = {_rid(design): design for design in [*baseline.values(), *active.values()]}
    if set(by_rid) != set(expected):
        raise BridgeError("unknown_ownership", "Current tabs differ from the active ownership ledger",
                          unexpected=[list(rid) for rid in set(by_rid) - set(expected)],
                          missing=[list(rid) for rid in set(expected) - set(by_rid)])
    baseline_rids = {_rid(design) for design in baseline.values()}
    for rid, original in expected.items():
        live = by_rid[rid]
        if original["name"] != live["name"] or any(
                _rid(original, key) != _rid(live, key) for key in ("button_rid", "close_rid")):
            raise BridgeError("baseline_changed" if rid in baseline_rids else "owned_identity_changed",
                              "Name or child runtime identity differs from the latest recorded identity")
        # Fresh attachment may not expose the source path. Never fabricate it;
        # recovery writes a fresh diagnostic and does not overwrite the original.
        if original.get("path") and live.get("path") and Path(original["path"]).resolve() != Path(live["path"]).resolve():
            raise BridgeError("identity_path_changed", "Observed document path differs from the ledger")
        if rid in baseline_rids and original.get("dirty") != live.get("dirty"):
            raise BridgeError("baseline_changed", "Baseline save state changed")
    return {_rid(design.to_dict()): design for design in current}


def recover_recorded_run(previous_run, output_dir, *, adapter_factory=None):
    previous, out = Path(previous_run).resolve(), Path(output_dir).resolve()
    if out == previous or out.is_relative_to(previous) or previous.is_relative_to(out):
        raise BridgeError("recovery_output_conflict", "Recovery evidence must use a fresh separate directory")
    out.mkdir(parents=True, exist_ok=False)
    result = {"ok": False, "cleanup_ok": False, "lifecycle_ok": False, "e2e_ok": False,
              "source_run": str(previous), "diagnostic_files": [], "owned_documents_remaining": None,
              "workers_stopped": False, "verification_scope": "Separate cleanup of active recorded ownership; original lifecycle result is unchanged.",
              "identity_scope": "Same process creation time/window, active ledger, live tab and child runtime IDs and names; unknown source paths remain unknown."}
    adapter = None
    try:
        session = json.loads((previous / "session.json").read_text(encoding="utf-8"))
        if not isinstance(session, dict) or any(key not in session for key in ("pid", "create_time", "hwnd")):
            raise BridgeError("invalid_session_evidence", "Original process/window identity is incomplete")
        events = [json.loads(line) for line in (previous / "lifecycle-events.jsonl").read_text(encoding="utf-8").splitlines()]
        baseline, active, retired = active_ledger(events)
        result["source_active_document_ids"] = list(active)
        result["source_retired_document_ids"] = sorted(retired)
        if adapter_factory is None:
            from .adapters.windows import WindowsAdapter
            adapter_factory = WindowsAdapter
        adapter = adapter_factory(out)
        if any(adapter.session[key] != session[key] for key in ("pid", "create_time", "hwnd")):
            raise BridgeError("stale_recovery", "Original R13 process/window no longer exists")
        _inventory_matches(adapter.inventory(), baseline, active)
        for original_token, record in list(active.items()):
            current = _inventory_matches(adapter.inventory(), baseline, active)
            design = current[_rid(record)]
            adapter.supervisor.evidence.append("ownership_recovered", source=str(previous),
                original=record, current=design.to_dict(), process_create_time=session["create_time"])
            adapter._owned_tokens.add(design.token)
            path = out / f"diagnostic-{uuid.uuid4().hex}.afd"
            saved = adapter.save(design, path)
            result["diagnostic_files"].append(str(path))
            active[original_token] = saved.to_dict()
            _inventory_matches(adapter.inventory(), baseline, active)
            adapter.close(saved)
            del active[original_token]
            _inventory_matches(adapter.inventory(), baseline, active)
        _inventory_matches(adapter.inventory(), baseline, {})
        result["owned_documents_remaining"] = 0
        result["cleanup_ok"] = True
    except (Exception, KeyboardInterrupt) as error:
        result["error"] = _error(error)
    finally:
        if adapter is not None:
            try:
                adapter.shutdown()
                result["workers_stopped"] = True
            except (Exception, KeyboardInterrupt) as error:
                result["shutdown_error"] = _error(error)
                result["cleanup_ok"] = False
        result["ok"] = result["cleanup_ok"] and result["workers_stopped"] and "error" not in result
        try:
            with (out / "recovery-result.json").open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
        except Exception as error:
            result["ok"] = False
            result["result_write_error"] = _error(error)
    return result
