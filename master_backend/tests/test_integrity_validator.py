"""Integrity validator: sampling-rate (ZOH) + sequence-gap checks and new verdict policy."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from master_backend.app import integrity_validator as iv_mod

HEADER = (
    "timestamp_ms,acc_x_g,acc_y_g,acc_z_g,"
    "gyro_x_degs,gyro_y_degs,gyro_z_degs,"
    "label_id,label_name,sequence_number,device_id"
)


def _row(ts: int, acc: float, seq: int, device: str) -> str:
    return (
        f"{ts},{acc},0.0,0.0,"
        f"0.0,0.0,0.0,"
        f"0,0,{seq},{device}"
    )


def _write_csv(path: Path, rows: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _half_rate_rows(device: str, n_pairs: int = 40, spacing_ms: int = 10) -> list[str]:
    """Simulate a 50 Hz sensor fed into a 100 Hz timer: every acc triple repeats once."""
    rows = []
    for i in range(n_pairs):
        t = i * 2 * spacing_ms
        val = float(i)
        rows.append(_row(t, val, seq=2 * i, device=device))
        rows.append(_row(t + spacing_ms, val, seq=2 * i + 1, device=device))
    return rows


def _clean_rows(device: str, n: int = 100, spacing_ms: int = 10) -> list[str]:
    return [_row(i * spacing_ms, float(i), seq=i, device=device) for i in range(n)]


def _device(device_id: str, first_packet_ts: int | None = None,
            offline_intervals: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        device_id=device_id,
        device_role="waist",
        first_packet_ts=first_packet_ts,
        offline_intervals=offline_intervals or [],
        packets_received=1000,
    )


class _FakeIo:
    def dropped_no_writer(self, device_id: str) -> int:
        return 0


@pytest.fixture
def run_validator(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(iv_mod, "io_manager", _FakeIo())

    def _run(session_id, device_specs, devices, scheduled_start_ms=0):
        file_results = {}
        for device_id, csv_path in device_specs.items():
            text = csv_path.read_text(encoding="utf-8").splitlines()
            data_rows = [l for l in text[1:] if l.strip()]
            file_results[device_id] = {
                "path": str(csv_path),
                "rows": len(data_rows),
                "sha256": "deadbeef",
                "reordered": 0,
            }
        return asyncio.run(iv_mod.IntegrityValidator().run(
            session_id, file_results, devices, scheduled_start_ms
        ))

    return _run


def _device_report(report: dict, device_id: str) -> dict:
    return next(d for d in report["devices"] if d["device_id"] == device_id)


def test_half_rate_device_fails(tmp_path: Path, run_validator):
    session = "1785923185204"
    csv = _write_csv(tmp_path / f"{session}_waist_sensor_data.csv",
                     _half_rate_rows("DEV1"))
    report = run_validator(session, {"DEV1": csv}, [_device("DEV1")])
    dev = _device_report(report, "DEV1")
    assert dev["status"] == "FAIL"
    assert report["status"] == "FAIL"
    assert any("rate" in r for r in dev["reasons"])


def test_clean_device_passes(tmp_path: Path, run_validator):
    session = "1785923185205"
    csv = _write_csv(tmp_path / f"{session}_waist_sensor_data.csv",
                     _clean_rows("DEV1"))
    report = run_validator(session, {"DEV1": csv}, [_device("DEV1")])
    dev = _device_report(report, "DEV1")
    assert dev["status"] == "PASS"
    assert report["status"] == "PASS"
    assert dev["reasons"] == []


def test_drift_alone_is_partial_not_fail(tmp_path: Path, run_validator):
    session = "1785923185206"
    csv1 = _write_csv(tmp_path / f"{session}_chest_sensor_data.csv",
                      _clean_rows("DEV1"))
    csv2 = _write_csv(tmp_path / f"{session}_waist_sensor_data.csv",
                      _clean_rows("DEV2"))
    devices = [
        _device("DEV1", first_packet_ts=1_000_000),
        _device("DEV2", first_packet_ts=1_000_117),
    ]
    report = run_validator(session, {"DEV1": csv1, "DEV2": csv2},
                           devices, scheduled_start_ms=999_000)
    assert report["status"] == "PARTIAL"
    assert report["cross_device_checks"]["start_drift_ok"] is False


def test_sequence_gaps_fail(tmp_path: Path, run_validator):
    session = "1785923185207"
    # Good 100 Hz rate, but skip every other sequence number -> >1 % missing.
    rows = []
    for i in range(100):
        rows.append(_row(i * 10, float(i), seq=i * 2, device="DEV1"))
    csv = _write_csv(tmp_path / f"{session}_waist_sensor_data.csv", rows)
    report = run_validator(session, {"DEV1": csv}, [_device("DEV1")])
    dev = _device_report(report, "DEV1")
    assert dev["status"] == "FAIL"
    assert any("sequence gap" in r for r in dev["reasons"])


def test_sampling_sidecar_written(tmp_path: Path, run_validator):
    session = "1785923185208"
    csv = _write_csv(tmp_path / f"{session}_waist_sensor_data.csv",
                     _half_rate_rows("DEV1"))
    run_validator(session, {"DEV1": csv}, [_device("DEV1")])
    sidecar = tmp_path / f"{session}_sampling.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["session_id"] == session
    assert isinstance(data["devices"], list)
    assert "acc_run_length_hist" in data["devices"][0]


def test_env_override_lowers_fail_threshold(tmp_path: Path, monkeypatch, run_validator):
    session = "1785923185209"
    csv = _write_csv(tmp_path / f"{session}_waist_sensor_data.csv",
                     _half_rate_rows("DEV1"))
    monkeypatch.setenv("INTEGRITY_RATE_FAIL_FRAC", "0.10")
    report = run_validator(session, {"DEV1": csv}, [_device("DEV1")])
    dev = _device_report(report, "DEV1")
    assert dev["status"] != "FAIL"
    assert dev["status"] == "PARTIAL"
