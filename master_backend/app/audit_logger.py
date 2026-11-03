"""
Per-session structured JSONL audit log (CLAUDE.md §13).
Never swallows errors — fail loud if writing fails.
"""
import asyncio
import json
import logging
import time
from pathlib import Path

import aiofiles

logger = logging.getLogger(__name__)


class AuditLogger:
    def __init__(self) -> None:
        self._file = None
        self._path: Path | None = None
        self._buffer: list[str] = []
        self._flush_task: asyncio.Task | None = None

    async def open(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = await aiofiles.open(path, mode="a", encoding="utf-8")
        self._flush_task = asyncio.create_task(self._periodic_flush())
        await self.log("INFO", "audit_log_opened", {"path": str(path)})

    async def log(
        self,
        level: str,
        event: str,
        detail: dict | None = None,
    ) -> None:
        entry = {
            "ts_ms": int(time.time() * 1000),
            "level": level,
            "event": event,
            "detail": detail or {},
        }
        self._buffer.append(json.dumps(entry, ensure_ascii=False))
        if level == "ERROR":
            await self._flush()

    async def _flush(self) -> None:
        # Take the batch out of the buffer BEFORE the first await.  The old version
        # cleared it AFTER the write/flush awaits, so anything logged while the write
        # was in flight (an aiofiles thread-pool hop) was silently discarded — the
        # periodic task woke up exactly when the late-delivery WARN was logged and
        # wiped it, which made the frozen-clock output contract flaky.  Two concurrent
        # flushes could also both write the same batch.  Swapping first means every
        # entry belongs to exactly one batch; later appends wait for the next flush.
        if not (self._file and self._buffer):
            return
        batch = self._buffer
        self._buffer = []
        text = "\n".join(batch) + "\n"
        try:
            await self._file.write(text)
            await self._file.flush()
        except Exception:
            # Real I/O failure: put the batch back so the next flush retries it.
            self._buffer = batch + self._buffer
            raise

    async def _periodic_flush(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                await self._flush()
            except Exception as exc:
                logger.error("Audit flush failed: %s", exc)

    async def close(self) -> None:
        # Let a flush that is already running finish first: its batch left the buffer
        # when it started, so it must not still be writing when the file is closed.
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self._flush()
        if self._file:
            await self._file.close()
            self._file = None


# Global singleton — one logger per backend process, re-opened per session.
audit = AuditLogger()
