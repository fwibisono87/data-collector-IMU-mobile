"""End-of-session export: per-role (per-device) consolidation.

Covers merge_csv_sources_per_role (split + within-role dedup/sort), the role/extraction
helpers, and the /export consolidate + manifest contract with per-role primacy for the
"whole" verdict.
"""
import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from master_backend.app.export import (  # noqa: E402
    _per_role_consolidated,
    _recovery_role,
    _role_from_name,
    export_consolidate,
    export_manifest,
)
from master_backend.app.upload import (  # noqa: E402
    merge_csv_sources_per_role,
)

SESSION_ID = "1753000000000"
HEADER = (
    "timestamp_ms,acc_x_g,acc_y_g,acc_z_g,"
    "gyro_x_degs,gyro_y_degs,gyro_z_degs,"
    "label_id,label_name,sequence_number,device_id"
)


def _row(ts: int, seq: int, dev: str) -> str:
    return f"{ts},0.0,0.0,0.0,0.0,0.0,0.0,0,0,{seq},{dev}"


def _write_csv(path: Path, rows: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _session_folder(ssd: Path, subject: str = "Alice", tag: str = "T1") -> Path:
    folder = ssd / "Data_Riset_IMU" / f"{subject}_{tag}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _recovery_info(recovery: Path, device: str, *, role: str, seq1: int, n: int) -> Path:
    slug = "".join(c if (c.isalnum() or c in "._-") else "_" for c in device)
    d = recovery / _slug_impl(SESSION_ID)
    d.mkdir(parents=True, exist_ok=True)
    info = {
        "session_id": SESSION_ID,
        "device_id": device,
        "role": role,
        "complete": True,
        "done": False,
    }
    (d / f"{slug}.info.json").write_text(
        __import__("json").dumps(info), encoding="utf-8"
    )
    return _write_csv(d / f"{slug}.csv", [_row(1000 + i, seq1 + i, device) for i in range(n)])


def _slug_impl(s: str) -> str:
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in s)


# ── Pure helpers ──────────────────────────────────────────────────────────────


def test_role_from_name_covers_all_shapes():
    assert _role_from_name(f"{SESSION_ID}_chest_sensor_data.csv", SESSION_ID) == "chest"
    assert _role_from_name(f"{SESSION_ID}_chest_sensor_data_late.csv", SESSION_ID) == "chest"
    assert _role_from_name(f"{SESSION_ID}_chest_sensor_data_rescue.csv", SESSION_ID) == "chest"
    assert _role_from_name(f"{SESSION_ID}_waist_sensor_data.csv", SESSION_ID) == "waist"
    assert _role_from_name(f"{SESSION_ID}_misc.csv", SESSION_ID) == "misc"


def test_recovery_role_fallbacks():
    assert _recovery_role({"role": "waist"}) == "waist"
    assert _recovery_role({"role": ""}) == "unknown"
    assert _recovery_role({"device_id": "ab-cd", "role": ""}) == "ab-cd"
    assert _recovery_role({"device_id": "ab-cd"}) == "ab-cd"


def test_per_role_consolidated_index_skips_session_wide():
    files = [
        {"name": f"{SESSION_ID}_chest_consolidated.csv", "path": "/x/a", "kind": "consolidated"},
        {"name": f"{SESSION_ID}_consolidated.csv", "path": "/x/b", "kind": "consolidated"},
        {"name": f"{SESSION_ID}_waist_consolidated.csv", "path": "/x/c", "kind": "consolidated"},
    ]
    out = _per_role_consolidated(files, SESSION_ID)
    assert set(out.keys()) == {"chest", "waist"}


# ── Per-role merge ────────────────────────────────────────────────────────────


def test_merge_per_role_splits_and_dedups(tmp_path: Path):
    out = tmp_path / "out"
    chest_main = _write_csv(tmp_path / "chest_main.csv", [
        _row(1, 1, "DEV-CHEST"), _row(2, 2, "DEV-CHEST"), _row(3, 3, "DEV-CHEST"),
    ])
    chest_late = _write_csv(tmp_path / "chest_late.csv", [
        _row(2, 2, "DEV-CHEST"), _row(3, 3, "DEV-CHEST"), _row(4, 4, "DEV-CHEST"),
    ])
    waist_main = _write_csv(tmp_path / "waist_main.csv", [
        _row(1, 1, "DEV-WAIST"), _row(2, 2, "DEV-WAIST"),
    ])
    waist_rec = _write_csv(tmp_path / "waist_rec.csv", [
        _row(2, 2, "DEV-WAIST"), _row(5, 5, "DEV-WAIST"),
    ])

    result = merge_csv_sources_per_role(
        [
            ("chest", "main", chest_main),
            ("chest", "late", chest_late),
            ("waist", "main", waist_main),
            ("waist", "recovery", waist_rec),
        ],
        out,
        SESSION_ID,
        metadata_prefix="session_id=1753000000000,source=consolidate",
    )

    assert result["rows"] == 7
    assert set(result["per_role"].keys()) == {"chest", "waist"}
    assert result["per_role"]["chest"]["rows"] == 4          # 1,2,3,4
    assert result["per_role"]["chest"]["duplicates_dropped"] == 2
    assert result["per_role"]["waist"]["rows"] == 3          # 1,2,5
    assert result["per_role"]["waist"]["duplicates_dropped"] == 1

    chest_out = out / f"{SESSION_ID}_chest_consolidated.csv"
    waist_out = out / f"{SESSION_ID}_waist_consolidated.csv"
    assert chest_out.exists() and waist_out.exists()

    chest_rows = chest_out.read_text(encoding="utf-8").splitlines()[2:]
    assert [int(r.split(",")[0]) for r in chest_rows] == [1, 2, 3, 4]   # sorted


# ── End-to-end consolidate + manifest (per-role primacy) ─────────────────────


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    import master_backend.app.export as export_mod
    import master_backend.app.upload as upload_mod

    ssd = tmp_path / "ssd"
    rescue = tmp_path / "rescue"
    recovery = tmp_path / "recovery"
    monkeypatch.setattr(export_mod, "SSD_PATH", ssd)
    monkeypatch.setattr(export_mod, "RESCUE_PATH", rescue)
    monkeypatch.setattr(upload_mod, "RECOVERY_PATH", recovery)
    return {"ssd": ssd, "rescue": rescue, "recovery": recovery}


def _run(coro) -> dict:
    return asyncio.run(coro)


def test_consolidate_and_manifest_whole(store: dict):
    folder = _session_folder(store["ssd"])
    chest_main = _write_csv(folder / f"{SESSION_ID}_chest_sensor_data.csv", [
        _row(1, 1, "DEV-CHEST"), _row(2, 2, "DEV-CHEST"), _row(3, 3, "DEV-CHEST"),
    ])
    chest_late = _write_csv(folder / f"{SESSION_ID}_chest_sensor_data_late.csv", [
        _row(2, 2, "DEV-CHEST"), _row(3, 3, "DEV-CHEST"), _row(4, 4, "DEV-CHEST"),
    ])
    _write_csv(folder / f"{SESSION_ID}_waist_sensor_data.csv", [
        _row(1, 1, "DEV-WAIST"), _row(2, 2, "DEV-WAIST"),
    ])
    # Recovery upload adds one NEW waist row (seq 3) beyond what the WS stream buffered.
    _recovery_info(store["recovery"], "DEV-WAIST", role="waist", seq1=1, n=3)

    integrity = folder / f"{SESSION_ID}_integrity_report.json"
    integrity.write_text('{"status": "PASS", "devices": []}', encoding="utf-8")

    # A recovery file newer than nothing consolidated yet → per-role pending before.
    before = _run(export_manifest(SESSION_ID))
    assert before["whole"] is False
    assert before["recovery_pending"] is True

    res = _run(export_consolidate(SESSION_ID))
    assert res["rows"] == 7
    assert set(res["per_role"].keys()) == {"chest", "waist"}
    assert res["per_role"]["chest"]["rows"] == 4
    assert res["per_role"]["waist"]["rows"] == 3

    # Session-wide file still written (add-alongside), per-role files too.
    assert (folder / f"{SESSION_ID}_consolidated.csv").exists()
    assert (folder / f"{SESSION_ID}_chest_consolidated.csv").exists()
    assert (folder / f"{SESSION_ID}_waist_consolidated.csv").exists()

    after = _run(export_manifest(SESSION_ID))
    assert after["late_pending"] is False
    assert after["recovery_pending"] is False
    assert set(after["per_roles"]) == {"chest", "waist"}
    assert after["whole"] is True


def test_manifest_prefers_per_role_pending(store: dict):
    folder = _session_folder(store["ssd"])
    _write_csv(folder / f"{SESSION_ID}_chest_sensor_data.csv", [
        _row(1, 1, "DEV-CHEST"), _row(2, 2, "DEV-CHEST"),
    ])
    _write_csv(folder / f"{SESSION_ID}_waist_sensor_data.csv", [
        _row(1, 1, "DEV-WAIST"),
    ])
    integrity = folder / f"{SESSION_ID}_integrity_report.json"
    integrity.write_text('{"status": "PASS", "devices": []}', encoding="utf-8")

    _run(export_consolidate(SESSION_ID))

    # Now a brand-new late sidecar arrives for a role that has no per-role file yet.
    # The session-wide consolidated file exists and is old, but per-role primacy means
    # the uncovered role still marks late data as pending.
    _write_csv(folder / f"{SESSION_ID}_arm_sensor_data_late.csv", [
        _row(2, 9, "DEV-ARM"),
    ])

    m = _run(export_manifest(SESSION_ID))
    assert m["late_pending"] is True
    assert m["recovery_pending"] is False
    assert m["whole"] is False
