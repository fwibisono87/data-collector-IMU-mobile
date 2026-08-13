"""Runtime writer failure must fail over without silently losing the triggering row."""
import asyncio

from master_backend.app.io_manager import IoManager
from master_backend.proto.sensor_packet import SensorPacket


class _FailingWriter:
    async def write_row(self, row: str) -> None:
        raise OSError("SSD removed")

    async def abandon(self) -> None:
        pass


class _RecordingWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.rows: list[str] = []
        self.fail = fail

    async def write_row(self, row: str) -> None:
        if self.fail:
            raise OSError("rescue volume unavailable")
        self.rows.append(row)


def _packet() -> SensorPacket:
    return SensorPacket(device_id="DEV1", timestamp_ms=1234, sequence_number=7)


def test_primary_write_failure_activates_rescue_and_retries_same_row(monkeypatch):
    async def body():
        manager = IoManager()
        manager._writers["DEV1"] = _FailingWriter()  # type: ignore[assignment]
        rescue = _RecordingWriter()

        async def activate(device_id, failed_writer):
            manager._rescue_writers[device_id] = rescue  # type: ignore[assignment]
            return rescue

        monkeypatch.setattr(manager, "_activate_rescue_writer", activate)
        await manager.write_packet(_packet())

        assert len(rescue.rows) == 1
        assert "DEV1" not in manager._writers
        assert manager.write_failures("DEV1") == 1
        assert manager.rows_lost_after_failover("DEV1") == 0

    asyncio.run(body())


def test_failed_rescue_retry_is_counted_as_a_lost_row(monkeypatch):
    async def body():
        manager = IoManager()
        manager._writers["DEV1"] = _FailingWriter()  # type: ignore[assignment]
        rescue = _RecordingWriter(fail=True)

        async def activate(device_id, failed_writer):
            manager._rescue_writers[device_id] = rescue  # type: ignore[assignment]
            return rescue

        monkeypatch.setattr(manager, "_activate_rescue_writer", activate)
        await manager.write_packet(_packet())

        assert manager.write_failures("DEV1") == 2
        assert manager.rows_lost_after_failover("DEV1") == 1

    asyncio.run(body())
