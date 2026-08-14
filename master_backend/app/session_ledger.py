"""Durable session ledger for crash-safe recording lifecycle recovery.

The CSVs and integrity reports are the data artifacts; this ledger is the lifecycle
index that lets a newly connected dashboard discover a session whose final UI update
was lost. Every write is an atomic replace in the same directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class SessionLedger:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id: str) -> Path:
        # Session ids are generated epoch-ms strings. Keep the guard here because this
        # class is also used by HTTP recovery endpoints.
        if not session_id or any(c in session_id for c in "/\\"):
            raise ValueError("invalid session id")
        return self.root / f"{session_id}.state.json"

    def write(self, session_id: str, record: dict[str, Any]) -> None:
        target = self.path(session_id)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)

    def read(self, session_id: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path(session_id).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def list(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.state.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("session_id"):
                records.append(data)
        return records
