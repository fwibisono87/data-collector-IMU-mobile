"""Shared in-process backend harness.

The lifecycle suite needed a backend whose entire storage lives under ``tmp_path`` and
whose module-level singletons (the IoManager writers, the SessionManager registry, the
ledger) are reset between sessions.  That harness is used by more than one module now —
the output-contract suite drives the very same modules end to end — so it lives here
rather than in one test file.

Everything is redirected in place instead of re-imported: reloading the modules would give
finalize_job a different IoManager instance than the one under test, and the suite would
silently exercise an empty session.
"""
from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from master_backend.proto.sensor_packet import SensorPacket


class Rig:
    """Handle on one redirected backend: the modules plus the tmp storage roots."""

    def __init__(self, io_module, sm_module, export_module, io, manager, ssd_path, rescue_path,
                 recovery_path=None):
        self.io_module = io_module
        self.sm_module = sm_module
        self.export_module = export_module
        self.io = io
        self.manager = manager
        self.ssd_path = ssd_path
        self.rescue_path = rescue_path
        self.recovery_path = recovery_path
        self.events: list = []


@contextlib.contextmanager
def make_rig(tmp_path: Path, monkeypatch):
    """Point every backend storage root at ``tmp_path`` and reset the singletons."""
    ssd = Path(tmp_path) / "ssd"
    rescue = Path(tmp_path) / "rescue"
    recovery = Path(tmp_path) / "recovery"
    for directory in (ssd, rescue, recovery):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("SSD_PATH", str(ssd))
    monkeypatch.setenv("RESCUE_PATH", str(rescue))
    monkeypatch.setenv("RECOVERY_PATH", str(recovery))

    from master_backend.app import (
        cameras as cameras_mod,
        dedup_store, export, io_manager as io_mod, session_ledger,
        session_manager as sm_mod, upload,
    )
    from master_backend.app.api import runtime as runtime_mod
    from master_backend.app.audit_logger import audit

    io = io_mod.io_manager
    manager = sm_mod.session_manager

    monkeypatch.setattr(export, "SSD_PATH", ssd)
    monkeypatch.setattr(export, "RESCUE_PATH", rescue)
    monkeypatch.setattr(upload, "RECOVERY_PATH", recovery)
    monkeypatch.setattr(cameras_mod, "SSD_PATH", ssd)
    monkeypatch.setattr(cameras_mod, "PENDING_CAMERAS", ssd / "Data_Riset_IMU" / "_pending_cameras")
    monkeypatch.setattr(runtime_mod, "SSD_PATH", ssd)
    monkeypatch.setattr(io, "_ssd_path", ssd)
    monkeypatch.setattr(io, "_rescue_path", rescue)

    io._writers.clear()
    io._rescue_writers.clear()
    io._reset_late_state()
    io._session_open = False
    io._session_id = ""
    io._base = None
    io._label_ts, io._label_val = [], []
    io._dropped_no_writer.clear()
    io._write_failures.clear()
    io._rows_lost_after_failover.clear()

    if manager._offline_check_task:
        manager._offline_check_task.cancel()
        manager._offline_check_task = None
    manager._devices.clear()
    manager.state = sm_mod.SessionState.IDLE
    manager.session_id = ""
    manager._finalize_job = None
    manager._finalize_task = None
    manager._last_report = {}
    manager._state_path = ssd / ".sessions"
    manager._ledger = session_ledger.SessionLedger(manager._state_path)
    dedup_store.dedup.clear()

    # Per-session caches and locks survive across tests in the real process; clear them so
    # a second run of the same scenario cannot inherit the first run's decisions.
    cameras_mod._camera_locks.clear()
    cameras_mod._mark_hooks.clear()
    export._manifest_scan_cache.clear()
    export._consolidate_locks.clear()
    export._bundle_locks.clear()
    upload._upload_locks.clear()
    audit._buffer.clear()

    # Swap the observer list for this test, then restore whatever the app registered.
    saved_observers = list(manager._observers)
    manager._observers.clear()

    rig = Rig(io_mod, sm_mod, export, io, manager, ssd, rescue, recovery)

    async def observer(event):
        rig.events.append(event)

    manager.add_observer(observer)

    try:
        yield rig
    finally:
        if manager._offline_check_task:
            manager._offline_check_task.cancel()
            manager._offline_check_task = None
        manager._observers[:] = saved_observers


@pytest.fixture
def rig(tmp_path, monkeypatch):
    with make_rig(tmp_path, monkeypatch) as instance:
        yield instance


@pytest.fixture
def rig_builder(monkeypatch):
    """Build independent rigs under a caller-chosen directory (determinism tests)."""

    @contextlib.contextmanager
    def build(base):
        with make_rig(Path(base), monkeypatch) as instance:
            yield instance

    return build


class FakeWs:
    """Control socket stand-in: records what the backend pushed to a device."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass


def packet(device_id: str, seq: int, ts_ms: int, *, acc: float = 0.0) -> SensorPacket:
    return SensorPacket(
        acc_x=acc, acc_y=acc, acc_z=acc,
        gyro_x=acc, gyro_y=acc, gyro_z=acc,
        timestamp_ms=ts_ms, sequence_number=seq, device_id=device_id,
        schema_version=2, acc_ts_ms=ts_ms, gyro_ts_ms=ts_ms, sample_kind=0,
    )


async def start_session(rig, devices=(("dev-chest", "chest"),)) -> str:
    for device_id, role in devices:
        rig.manager.register_device(
            device_id=device_id, role=role, model="Pixel", app_version="2.0.0",
            ws=FakeWs(),
        )
    ok, session_id = await rig.manager.start_recording(
        {"subject_name": "Subject", "session_tag": "Tag", "operator": "Op"}
    )
    assert ok, session_id
    return session_id


async def stream(rig, device_id: str, count: int, *, base_ts: int = 1_700_000_000_000,
                 start_seq: int = 0) -> None:
    for i in range(count):
        # A changing acceleration value keeps this from looking like a held/ZOH stream.
        await rig.io.write_packet(
            packet(device_id, start_seq + i, base_ts + i * 10, acc=float(i % 17))
        )