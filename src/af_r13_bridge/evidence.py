"""Append-only evidence for new Bridge runs. No UI is accessed here."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def hash_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Evidence:
    """Retain unique raw artifacts and an append-only chronological journal."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal = self.directory / "events.jsonl"
        self._lock = threading.Lock()

    def append(self, event: str, **data: Any) -> dict:
        record = {"at_utc": datetime.now(timezone.utc).isoformat(),
                  "event": event, **data}
        encoded = json.dumps(record, ensure_ascii=False, allow_nan=False)
        with self._lock, self.journal.open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
            stream.flush()
        return record

    def write_json(self, label: str, data: Any) -> Path:
        label = re.sub(r"[^\w.-]", "_", label)[:80] or "artifact"
        destination = self.directory / f"{label}-{uuid.uuid4().hex}.json"
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        self.append("artifact", path=str(destination), sha256=hash_file(destination))
        return destination

    @staticmethod
    def hash_file(path: str | Path) -> str:
        return hash_file(path)

    def snapshot(self, label: str, path: str | Path,
                 metadata: dict | None = None) -> dict:
        """Register an existing screenshot; never capture or manipulate the UI."""
        source = Path(path).resolve(strict=True)
        return self.append("snapshot", label=label, path=str(source),
                           sha256=hash_file(source), metadata=metadata or {})
