"""CSV finalization must survive short Windows file locks without hiding failures."""

from pathlib import Path

import pytest

from master_backend.app import io_manager


HEADER = "# session_id=test\n"
CSV_HEADER = "timestamp_ms,acc_x,acc_y,acc_z,gyro_x,gyro_y,gyro_z,label_id,label_name,sequence_number,device_id,acc_ts_ms,gyro_ts_ms,sample_kind\n"


def _write_out_of_order_csv(path: Path) -> None:
    path.write_text(
        HEADER
        + CSV_HEADER
        + "200,0,0,0,0,0,0,0,0,2,dev,,,2\n"
        + "100,0,0,0,0,0,0,0,0,1,dev,,,2\n",
        encoding="utf-8",
    )


def test_sort_close_reorders_rows_and_keeps_headers(tmp_path):
    path = tmp_path / "session.csv"
    _write_out_of_order_csv(path)

    result = io_manager._sort_rows_by_timestamp(path)

    assert result == {"reordered": 1}
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == [HEADER.rstrip("\n"), CSV_HEADER.rstrip("\n")]
    assert [line.split(",", 1)[0] for line in lines[2:]] == ["100", "200"]
    assert not path.with_name(path.name + ".sort.tmp").exists()


def test_sort_close_retries_transient_replace_lock(tmp_path, monkeypatch):
    path = tmp_path / "session.csv"
    _write_out_of_order_csv(path)
    real_replace = io_manager.os.replace
    calls = 0

    def replace_with_one_transient_lock(source, target):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError(5, "temporarily locked")
        return real_replace(source, target)

    monkeypatch.setattr(io_manager.os, "replace", replace_with_one_transient_lock)
    monkeypatch.setattr(io_manager, "_SORT_REPLACE_BACKOFF_SEC", 0)

    result = io_manager._sort_rows_by_timestamp(path)

    assert result == {"reordered": 1}
    assert calls == 2
    assert [line.split(",", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()[2:]] == ["100", "200"]
    assert not path.with_name(path.name + ".sort.tmp").exists()


def test_sort_close_preserves_sorted_recovery_artifact_on_persistent_lock(tmp_path, monkeypatch):
    path = tmp_path / "session.csv"
    _write_out_of_order_csv(path)
    original = path.read_bytes()

    def permanently_locked(_source, _target):
        raise PermissionError(5, "permanently locked")

    monkeypatch.setattr(io_manager.os, "replace", permanently_locked)
    monkeypatch.setattr(io_manager, "_SORT_REPLACE_ATTEMPTS", 2)
    monkeypatch.setattr(io_manager, "_SORT_REPLACE_BACKOFF_SEC", 0)

    with pytest.raises(PermissionError):
        io_manager._sort_rows_by_timestamp(path)

    recovery = path.with_name(path.name + ".sort.tmp")
    assert path.read_bytes() == original
    assert recovery.exists()
    recovery_lines = recovery.read_text(encoding="utf-8").splitlines()
    assert [line.split(",", 1)[0] for line in recovery_lines[2:]] == ["100", "200"]
