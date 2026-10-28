"""Stable Python API. Windows dependencies are never imported by this module."""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from .lifecycle import Adapter, Lifecycle
from .models import BridgeError, Design, Result


def capabilities() -> dict[str, Any]:
    """Implementation status is deliberately separate from live validation."""
    return {
        "session_and_lifecycle": "implemented_unverified",
        "empty_design_lifecycle": "verified_current_scenario",
        "validation_reference": {"date": "2026-10-28", "application": "R13.0.1 local Chinese UI",
                            "scenario": "One empty design; baseline had zero documents",
                            "evidence": "runs/lifecycle-20261028-03/result.json",
                            "elapsed_ms": 31645.719},
        "native_memory_fully_released": False,
        "lifecycle_scope": "One small owned design; save, close, same-file reopen, semantic readback, final close.",
        "business_bridge": "not_implemented",
        "baogu_workflow": "not_implemented",
        "solver": "not_implemented",
        "full_result_fields": "not_implemented",
        "recorder": "not_implemented",
        "live_evidence_required": True,
        "e2e_ok": False,
    }


def error_result(operation: str, error: BaseException, *, elapsed_ms: float = 0.0) -> dict[str, Any]:
    if isinstance(error, BridgeError):
        detail = error.to_dict()
    elif isinstance(error, KeyboardInterrupt):
        detail = {"code": "cancelled", "message": "Operation cancelled."}
    else:
        detail = {"code": getattr(error, "code", "operation_failed"), "type": type(error).__name__, "message": str(error)}
    return Result(False, operation, uuid.uuid4().hex, error=detail, elapsed_ms=elapsed_ms).to_dict()


class Bridge:
    """A single ownership session; never reconstruct ownership from file names.

    Keep this object alive while issuing individual operations. For command-line
    use, ``batch`` keeps all operations and document references in one process.
    """

    def __init__(self, adapter: Adapter, run_dir: str | Path):
        self.lifecycle = Lifecycle(adapter, run_dir)
        self._handles: dict[str, Design] = {}

    def run_lifecycle(self, filename: str = "lifecycle.afd") -> dict[str, Any]:
        return self.lifecycle.run(filename)

    def _handle(self, request: dict[str, Any]) -> Design:
        token = request.get("document_id")
        if not isinstance(token, str) or token not in self._handles:
            raise BridgeError("invalid_document_handle", "document_id must refer to a current design created or reopened in this Bridge session.")
        return self._handles[token]

    def _path(self, request: dict[str, Any]) -> Path:
        value = request.get("path")
        if not isinstance(value, str) or not value:
            raise BridgeError("invalid_request", "A nonempty path is required.")
        path = Path(value)
        return path if path.is_absolute() else self.lifecycle.run_dir / path

    def call(self, operation: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.monotonic()
        request = {} if request is None else request
        try:
            if not isinstance(request, dict):
                raise BridgeError("invalid_request", "An operation request must be a JSON object.")
            if operation == "lifecycle":
                return self.run_lifecycle(request.get("filename", "lifecycle.afd"))
            data: dict[str, Any]
            document_id = None
            conditions: dict[str, Any] = {}
            scope = "Owned design lifecycle operation; no forming or solver verification."
            if operation == "capabilities":
                data = capabilities()
                scope = "Implementation declarations only; consult live run evidence for verification."
            elif operation == "attach":
                data = {"baseline": [item.to_dict() for item in self.lifecycle.attach()]}
                conditions["baseline_recorded"] = True
            elif operation == "list_designs":
                data = {"designs": [item.to_dict() for item in self.lifecycle.adapter.inventory()]}
                scope = "Observed design inventory; listing does not grant ownership."
            elif operation == "create_design":
                design = self.lifecycle.create_design()
                self._handles[design.token] = design
                document_id = design.token
                data = {"design": design.to_dict()}
                conditions["ownership_verified"] = True
            elif operation == "save_design":
                design = self.lifecycle.save_design(self._handle(request), self._path(request))
                self._handles[design.token] = design
                document_id = design.token
                data = {"design": design.to_dict()}
                conditions["save_verified"] = True
            elif operation == "close_design":
                design = self._handle(request)
                self.lifecycle.close_design(design)
                del self._handles[design.token]
                document_id = design.token
                data = {"closed_document_id": design.token}
                conditions["close_verified"] = True
            elif operation == "reopen_design":
                design = self.lifecycle.reopen_design(self._path(request))
                self._handles[design.token] = design
                document_id = design.token
                data = {"design": design.to_dict()}
                conditions["reopen_same_file_verified"] = True
            elif operation == "readback":
                design = self._handle(request)
                document_id = design.token
                data = {"values": self.lifecycle.readback(design)}
                scope = "Observed semantic values; readback alone does not prove persistence."
            elif operation == "cleanup":
                data = self.lifecycle.cleanup()
                self._handles = {token: value for token, value in self._handles.items() if token in self.lifecycle.owned}
                if not data["cleanup_ok"]:
                    return Result(False, operation, uuid.uuid4().hex, data=data, postconditions=data,
                                  error={"code": "cleanup_failed", "details": data["errors"]},
                                  elapsed_ms=(time.monotonic() - started) * 1000).to_dict()
                conditions = {"cleanup_ok": True, "owned_documents_remaining": 0, "unknown_documents_remaining": 0}
            elif operation == "disconnect":
                self.lifecycle.disconnect()
                data = {"application_exit_requested": False, "owned_documents_remaining": len(self.lifecycle.owned)}
                conditions["workers_stopped"] = True
                scope = "Bridge worker shutdown only; disconnect does not close any design."
            else:
                raise BridgeError("not_implemented", "This operation is not implemented in the new Bridge.", operation=operation)
            return Result(True, operation, uuid.uuid4().hex, document_id=document_id, data=data,
                          verification_scope=scope, postconditions=conditions,
                          evidence=[str(self.lifecycle.run_dir / "lifecycle-events.jsonl")],
                          elapsed_ms=round((time.monotonic() - started) * 1000, 3)).to_dict()
        except (Exception, KeyboardInterrupt) as error:
            return error_result(operation, error, elapsed_ms=(time.monotonic() - started) * 1000)

    def batch(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """Run a sequence with ``@alias`` and ``@last`` document references.

        Each operation can define ``as``. Cleanup and disconnect always run,
        including after invalid requests or failed steps. No implicit claim of
        forming/solver acceptance or persistence verification is made.
        """
        started = time.monotonic()
        results: list[dict[str, Any]] = []
        aliases: dict[str, str] = {}
        failure: dict[str, Any] | None = None
        cleanup = {"cleanup_ok": False, "owned_documents_remaining": None, "unknown_documents_remaining": None}
        stopped = False
        try:
            if not isinstance(operations, list) or not operations:
                raise BridgeError("invalid_request", "batch requires a nonempty operations list.")
            for step in operations:
                if not isinstance(step, dict) or not isinstance(step.get("operation"), str):
                    raise BridgeError("invalid_request", "Each batch step must contain an operation string.")
                operation = step["operation"]
                if operation in {"lifecycle", "disconnect"}:
                    raise BridgeError("invalid_batch_operation", "Batch manages its own cleanup and disconnect; nested lifecycle/disconnect is not allowed.")
                arguments = dict(step.get("request", {}))
                reference = arguments.get("document_id")
                if isinstance(reference, str) and reference.startswith("@"):
                    alias = reference[1:]
                    if alias not in aliases:
                        raise BridgeError("unknown_document_reference", "Document alias has not been produced by a prior step.", alias=alias)
                    arguments["document_id"] = aliases[alias]
                result = self.call(operation, arguments)
                results.append(result)
                if not result["ok"]:
                    failure = result["error"]
                    break
                if result.get("document_id"):
                    aliases["last"] = result["document_id"]
                    if step.get("as"):
                        aliases[str(step["as"])] = result["document_id"]
        except (Exception, KeyboardInterrupt) as error:
            failure = error_result("batch", error)["error"]
        finally:
            try:
                if self.lifecycle.baseline is not None:
                    cleanup = self.lifecycle.cleanup()
                else:
                    cleanup = {"cleanup_ok": True, "owned_documents_remaining": 0, "unknown_documents_remaining": 0,
                               "scope": "No ownership session was attached and no write operation could begin."}
            except (Exception, KeyboardInterrupt) as error:
                failure = failure or error_result("cleanup", error)["error"]
            finally:
                try:
                    self.lifecycle.disconnect()
                    stopped = True
                except (Exception, KeyboardInterrupt) as error:
                    failure = failure or error_result("disconnect", error)["error"]
        if self.lifecycle.evidence_errors:
            failure = failure or {"code": "evidence_write_failed", "details": self.lifecycle.evidence_errors}
        ok = failure is None and bool(cleanup["cleanup_ok"]) and stopped
        if not ok and failure is None:
            failure = {"code": "cleanup_failed", "details": cleanup}
        return Result(ok, "batch", uuid.uuid4().hex, data={"results": results, "cleanup": cleanup, "workers_stopped": stopped,
                      "e2e_ok": False}, postconditions={**cleanup, "workers_stopped": stopped}, error=failure,
                      verification_scope="Requested individual operations and final ownership cleanup; not a lifecycle or solver acceptance claim.",
                      evidence=[str(self.lifecycle.run_dir / "lifecycle-events.jsonl")],
                      elapsed_ms=round((time.monotonic() - started) * 1000, 3)).to_dict()
