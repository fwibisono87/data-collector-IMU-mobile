"""Durable session ledger for crash-safe recording lifecycle recovery.

The CSVs and integrity reports are the data artifacts; this ledger is the lifecycle
index that lets a newly connected dashboard discover a session whose final UI update
was lost. Every write is an atomic replace in the same directory.
"""

from __future__ import annotations

import contextlib
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
        """Atomically install a new record, durably.

        os.replace is atomic against concurrent readers, but the rename can reach the disk
        before the bytes behind it do — leaving a zero-length or partial state file after a
        power loss, which is exactly the scenario this ledger exists to survive. fsync the
        contents first, then the directory entry.
        """
        target = self.path(session_id)
        tmp = target.with_suffix(target.suffix + ".tmp")
        payload = json.dumps(record, indent=2, sort_keys=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        # Directory fsync makes the rename itself durable. Not available on every
        # platform (Windows rejects opening a directory), so failure is non-fatal.
        with contextlib.suppress(OSError, AttributeError):
            dir_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

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
