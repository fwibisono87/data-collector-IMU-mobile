import asyncio
import time
from pathlib import Path

from master_backend.app import export as export_mod
from master_backend.app import integrity_validator as integrity_mod
from master_backend.app import upload as upload_mod
from master_backend.app.session_ledger import SessionLedger
from master_backend.app.session_manager import SessionManager
from master_backend.app.csv_schema import CSV_HEADER_V2


def test_session_ledger_atomic_round_trip_and_listing(tmp_path: Path):
    ledger = SessionLedger(tmp_path / ".sessions")
    record = {
        "session_id": "123",
        "state": "IDLE",
        "terminal": True,
        "integrity_report": {"status": "PARTIAL"},
    }
    ledger.write("123", record)
    assert ledger.read("123") == record
    assert ledger.list()[0]["session_id"] == "123"


def test_session_ledger_rejects_path_escape(tmp_path: Path):
    ledger = SessionLedger(tmp_path)
    try:
        ledger.path("../outside")
    except ValueError:
        pass
    else:
        raise AssertionError("path traversal must be rejected")


class _NoLossIo:
    def dropped_no_writer(self, device_id: str) -> int:
        return 0

    def write_failures(self, device_id: str) -> int:
        return 0

    def rows_lost_after_failover(self, device_id: str) -> int:
        return 0


def test_restart_reconciles_interrupted_csv_to_terminal_ledger(tmp_path: Path, monkeypatch):
    """A backend death after writes must still produce a terminal, inspectable verdict."""
    ssd = tmp_path / "ssd"
    rescue = tmp_path / "rescue"
    recovery = tmp_path / "recovery"
    monkeypatch.setenv("SSD_PATH", str(ssd))
    monkeypatch.setattr(export_mod, "SSD_PATH", ssd)
    monkeypatch.setattr(export_mod, "RESCUE_PATH", rescue)
    monkeypatch.setattr(upload_mod, "RECOVERY_PATH", recovery)
    monkeypatch.setattr(integrity_mod, "io_manager", _NoLossIo())

    session_id = "1790000000000"
    folder = ssd / "Data_Riset_IMU" / "Alice_T1"
    folder.mkdir(parents=True)
    base_ms = int(time.time() * 1000) - 10_000
    rows = "".join(
        f"{base_ms + i * 10},{i / 1000:.3f},0.2,0.3,1.0,1.1,1.2,0,0,{i},DEV1,"
        f"{base_ms + i * 10},{base_ms + i * 10},0\n"
        for i in range(1000)
    )
    (folder / f"{session_id}_waist_100hz_sensor_data.csv").write_text(
        CSV_HEADER_V2 + rows, encoding="utf-8"
    )

    ledger = SessionLedger(ssd / ".sessions")
    ledger.write(session_id, {
        "session_id": session_id,
        "state": "RECORDING",
        "terminal": False,
        "startup_in_progress": False,
        "subject_name": "Alice",
        "session_tag": "T1",
        "operator": "operator",
        "scheduled_start_ms": 0,
            "recording_started_ms": base_ms,
        "label_timeline": [],
        "devices": [{
            "device_id": "DEV1", "role": "waist", "model": "test",
            "app_version": "test", "packets": 1000, "first_packet_ts": base_ms,
            "offline_intervals": [], "substate": "RECORDING",
        }],
    })

    manager = SessionManager()
    manager._ledger = ledger
    asyncio.run(manager.recover_interrupted_sessions())

    recovered = ledger.read(session_id)
    assert recovered is not None
    assert recovered["terminal"] is True
    assert recovered["state"] == "IDLE"
    assert recovered["recovered_after_backend_restart"] is True
    assert recovered["integrity_report"]["status"] == "PASS"
    assert recovered["integrity_report"]["analysis_ready"] is True
    assert recovered["file_results"]["DEV1"]["rows"] == 1000
    assert (folder / f"{session_id}_waist_consolidated.csv").exists()
