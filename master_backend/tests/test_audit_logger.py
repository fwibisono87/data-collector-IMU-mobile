"""AuditLogger must not lose (or duplicate) entries when flushes overlap.

The periodic flush task builds its text from the buffer, awaits the file write (an
aiofiles thread-pool hop) and then removes what it wrote.  Anything appended during
those awaits used to be removed too, without ever being written: in the frozen-clock
output-contract scenario the periodic flush woke up exactly when the late-delivery
WARN was logged and the ``late_delivery_started`` line vanished, which is what made
the determinism test flaky.  Two concurrent flushes (the periodic task plus an
ERROR-level log) could also both write the same batch.

The fix takes the batch out of the buffer BEFORE the first await, so every entry is
in exactly one batch and later appends are written by the next flush.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from master_backend.app.audit_logger import AuditLogger


class GatedFile:
    """An aiofiles stand-in whose every write blocks until the gate is released."""

    def __init__(self, inner, gate: asyncio.Event) -> None:
        self._inner = inner
        self._gate = gate
        self.writes = 0

    async def write(self, text: str):
        self.writes += 1
        await self._gate.wait()
        return await self._inner.write(text)

    async def flush(self):
        return await self._inner.flush()

    async def close(self):
        return await self._inner.close()


class FailingFile:
    """Fails the next write with a real I/O error, then behaves like the inner file."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.fail_next = True

    async def write(self, text: str):
        if self.fail_next:
            self.fail_next = False
            raise OSError("simulated disk failure")
        return await self._inner.write(text)

    async def flush(self):
        return await self._inner.flush()

    async def close(self):
        return await self._inner.close()


def events(path) -> list[str]:
    return [
        json.loads(line)["event"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_entry_appended_during_an_in_flight_flush_is_not_lost(tmp_path):
    """The exact contract-scenario interleaving: log while the flush is writing."""
    gate = asyncio.Event()
    logger = AuditLogger()
    await logger.open(tmp_path / "audit.jsonl")
    logger._file = GatedFile(logger._file, gate)

    await logger.log("INFO", "first_event", {})
    flush = asyncio.create_task(logger._flush())
    await asyncio.sleep(0)                       # let the flush block inside its write
    await logger.log("WARN", "late_delivery_started", {})   # appended mid-flush
    gate.set()
    await flush
    await logger.close()

    written = events(tmp_path / "audit.jsonl")
    assert "first_event" in written
    assert "late_delivery_started" in written


@pytest.mark.asyncio
async def test_concurrent_flushes_write_each_entry_exactly_once(tmp_path):
    logger = AuditLogger()
    await logger.open(tmp_path / "audit.jsonl")
    await logger.log("INFO", "only_once", {})

    await asyncio.gather(logger._flush(), logger._flush())
    await logger.close()

    written = events(tmp_path / "audit.jsonl")
    assert written.count("only_once") == 1
    assert written.count("audit_log_opened") == 1


@pytest.mark.asyncio
async def test_a_failed_flush_keeps_the_entries_for_the_next_attempt(tmp_path):
    logger = AuditLogger()
    await logger.open(tmp_path / "audit.jsonl")
    logger._file = FailingFile(logger._file)

    await logger.log("INFO", "retry_me", {})
    with pytest.raises(OSError):
        await logger._flush()
    await logger.close()                        # the final flush retries the batch

    assert events(tmp_path / "audit.jsonl").count("retry_me") == 1