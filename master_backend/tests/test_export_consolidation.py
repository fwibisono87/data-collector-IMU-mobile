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
    merge_csv_sources,
    merge_csv_sources_per_role,
)
from master_backend.app.csv_schema import (  # noqa: E402
    CSV_HEADER_V1,
    CSV_HEADER_V2,
    V2_WIDTH,
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


# ── Version-tolerant merge ────────────────────────────────────────────────────


def _row2(ts: int, seq: int, dev: str) -> str:
    return f"{ts},0.0,0.0,0.0,0.0,0.0,0.0,0,0,{seq},{dev},100,200,stream"


def test_merge_v1_header_not_injected_as_data_row(tmp_path: Path):
    v1_file = tmp_path / "v1.csv"
    v1_file.write_text(
        "# session_id=abc\n"
        + CSV_HEADER_V1
        + "\n".join(_row(1000, i, "DEV") for i in range(3))
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.csv"
    result = merge_csv_sources([("main", v1_file)], out)

    lines = out.read_text(encoding="utf-8").splitlines()
    data_rows = [l for l in lines if l and not l.startswith("#") and not l.startswith("timestamp_ms")]
    assert all(r.split(",")[0].isdigit() for r in data_rows)
    assert len(data_rows) == 3
    assert result["rows"] == 3
    assert result["duplicates_dropped"] == 0
    assert result["sources"] == {"main": 3}


def test_merge_mixed_version_produces_14_fields(tmp_path: Path):
    v1_file = tmp_path / "v1.csv"
    v1_file.write_text(CSV_HEADER_V1 + _row(1, 1, "D1") + "\n", encoding="utf-8")
    v2_file = tmp_path / "v2.csv"
    v2_file.write_text(CSV_HEADER_V2 + _row2(2, 2, "D2") + "\n", encoding="utf-8")
    out = tmp_path / "out.csv"

    result = merge_csv_sources([("main", v1_file), ("main", v2_file)], out)

    lines = out.read_text(encoding="utf-8").splitlines()
    data_rows = [l for l in lines if l and not l.startswith("#") and not l.startswith("timestamp_ms")]
    assert result["rows"] == 2
    assert all(len(r.split(",")) == V2_WIDTH for r in data_rows)
    v1_row = [r for r in data_rows if r.split(",")[10] == "D1"][0]
    assert v1_row.split(",")[11:] == ["", "", ""]
    v2_row = [r for r in data_rows if r.split(",")[10] == "D2"][0]
    assert v2_row.split(",")[11:] == ["100", "200", "stream"]


def test_merge_provenance_accumulates_shared_label(tmp_path: Path):
    out = tmp_path / "out.csv"
    f1 = tmp_path / "a.csv"; f1.write_text(CSV_HEADER_V2 + "\n".join(_row2(1, i, "D") for i in (1, 2, 3)) + "\n", encoding="utf-8")
    f2 = tmp_path / "b.csv"; f2.write_text(CSV_HEADER_V2 + "\n".join(_row2(2, i, "D") for i in (4, 5, 6, 7, 8)) + "\n", encoding="utf-8")
    f3 = tmp_path / "c.csv"; f3.write_text(CSV_HEADER_V2 + "\n".join(_row2(3, i, "D") for i in (9, 10, 11, 12, 13, 14, 15)) + "\n", encoding="utf-8")

    result = merge_csv_sources([("main", f1), ("main", f2), ("main", f3)], out)

    assert result["sources"]["main"] == 15
    assert len(result["source_files"]) == 3
    assert sorted(sf["rows"] for sf in result["source_files"]) == [3, 5, 7]
    assert all(sf["label"] == "main" for sf in result["source_files"])
    assert result["rows"] == 15


def test_merge_dedup_still_works(tmp_path: Path):
    out = tmp_path / "out.csv"
    f1 = tmp_path / "a.csv"; f1.write_text(CSV_HEADER_V2 + "\n".join(_row2(1, i, "D") for i in (1, 2, 3)) + "\n", encoding="utf-8")
    f2 = tmp_path / "b.csv"; f2.write_text(CSV_HEADER_V2 + "\n".join(_row2(2, i, "D") for i in (2, 3, 4)) + "\n", encoding="utf-8")

    result = merge_csv_sources([("main", f1), ("main", f2)], out)

    assert result["rows"] == 4
    assert result["duplicates_dropped"] == 2
    assert result["sources"]["main"] == 4


def test_scan_rows_ignores_v1_header(tmp_path: Path):
    """A v1 header must not be counted as a data row once CSV_HEADER is v2.

    _scan_rows feeds the export manifest's row_count — the number the operator reads to
    decide whether a session is whole. An exact-match header test against the (now v2)
    constant let every v1 file's header through as a data row, inflating that count.
    """
    from master_backend.app.export import _scan_rows

    v1 = tmp_path / "v1.csv"
    v1.write_text(
        "# session_id=abc,schema_version=1\n"
        + CSV_HEADER_V1
        + "\n".join(_row(1000 + i, i, "DEV") for i in range(3))
        + "\n",
        encoding="utf-8",
    )
    v2 = tmp_path / "v2.csv"
    v2.write_text(
        "# session_id=abc,schema_version=2\n"
        + CSV_HEADER_V2
        + "\n".join(_row2(2000 + i, i, "DEV2") for i in range(4))
        + "\n",
        encoding="utf-8",
    )

    row_count, _labels = _scan_rows([v1, v2])

    assert row_count == 7, "header lines of either schema version must not be counted"
