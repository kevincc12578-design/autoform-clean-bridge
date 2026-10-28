"""Transport-neutral records for the independently implemented R13 bridge."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Design:
    """One live design identity. Tokens are never reused within a session.

    ``dirty=None`` explicitly means the adapter cannot observe the dirty flag.
    A name is descriptive only; it must never be used as proof of ownership.
    """

    token: str
    name: str
    path: str | None = None
    dirty: bool | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Result:
    ok: bool
    operation: str
    request_id: str
    document_id: str | None = None
    job_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    verification_scope: str = ""
    postconditions: dict[str, Any] = field(default_factory=dict)
    evidence: list[Any] = field(default_factory=list)
    error: dict[str, Any] | None = None
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": self.details}
