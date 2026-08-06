"""Tests for tools/analyze_session.py, loaded by path as a non-package script."""

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "tools" / "analyze_session.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("analyze_session", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()

SESSION_ID = "1785921100261"


def _v1_row(t, acc, seq=None, device_id="dev"):
    """A raw 11-column v1 data row string."""
    if seq is None:
        seq = t
    return (f"{t},{acc},0.0,0.0,0.0,0.0,0.0,0,0,{seq},{device_id}")


def write_clean(path, lines_per_axis=50):
    """All-distinct rows at 10 ms spacing -> clean (PASS)."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(
            f"# session_id={SESSION_ID},role=chest,schema_version=1,device_id=dev,"
            f"nominal_hz=100.0\n"
            "timestamp_ms,acc_x_g,acc_y_g,acc_z_g,gyro_x_degs,gyro_y_degs,gyro_z_degs,"
            "label_id,label_name,sequence_number,device_id\n"
        )
        for i in range(lines_per_axis * 2):
            f.write(_v1_row(i * 10, float(i), seq=i) + "\n")


def write_held(path, pairs=50, role="thigh"):
    """Every acc triple repeats exactly once -> 50 % held, FAIL."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(
            f"# session_id={SESSION_ID},role={role},schema_version=1,device_id=dev2,"
            f"nominal_hz=100.0\n"
            "timestamp_ms,acc_x_g,acc_y_g,acc_z_g,gyro_x_degs,gyro_y_degs,gyro_z_degs,"
            "label_id,label_name,sequence_number,device_id\n"
        )
        for i in range(pairs):
            f.write(_v1_row(i * 20, float(i), seq=i * 2, device_id="dev2") + "\n")
            f.write(_v1_row(i * 20 + 10, float(i), seq=i * 2 + 1, device_id="dev2") + "\n")


def _build_session_dir(tmp_path):
    d = tmp_path / "session"
    d.mkdir()
    write_clean(d / f"{SESSION_ID}_chest_consolidated.csv")
    write_held(d / f"{SESSION_ID}_thigh_sensor_data.csv")
    return d, "chest_consolidated", "thigh_sensor_data"


@pytest.fixture(scope="module")
def session_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("sess")
    write_clean(d / f"{SESSION_ID}_chest_consolidated.csv")
    write_held(d / f"{SESSION_ID}_thigh_sensor_data.csv")
    return d


def test_clean_pass_and_duplicated_fail(session_dir):
    result = mod.analyse_path(session_dir)
    by_file = {d["kind"]: d for d in result["devices"]}

    clean = by_file["consolidated"]
    assert clean["verdict"] == "PASS"
    assert clean["held_row_pct"] == 0.0
    assert abs(clean["true_sensor_hz"] - clean["nominal_hz"]) < 1

    held = by_file["sensor_data"]
    assert held["verdict"] == "FAIL"
    assert held["held_row_pct"] > 45
    assert abs(held["true_sensor_hz"] - held["nominal_hz"] / 2) < 1
    assert len(result["devices"]) == 2


def test_json_output_contains_both_devices(session_dir, tmp_path):
    result = mod.analyse_path(session_dir)
    assert set(result.keys()) >= {
        "session_id", "generated_at_ms", "schema_version", "source",
        "thresholds", "devices",
    }
    assert result["schema_version"] == 2
    assert result["generated_at_ms"] > 0
    assert len(result["devices"]) == 2
    for d in result["devices"]:
        for key in ("file", "verdict", "reasons", "true_sensor_hz", "held_row_pct",
                    "nominal_hz", "rows", "span_s", "sequence"):
            assert key in d
    out = tmp_path / "report.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f)
    loaded = json.loads(out.read_text())
    assert len(loaded["devices"]) == 2


def test_zip_matches_directory(session_dir, tmp_path):
    zpath = tmp_path / "export.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for p in session_dir.rglob("*.csv"):
            zf.write(p, arcname=p.name)
    dir_result = mod.analyse_path(session_dir)
    zip_result = mod.analyse_path(zpath)
    assert dir_result["devices"] == zip_result["devices"]
    assert zip_result["source"] == str(zpath)


def test_v2_declared_block_agreement(session_dir, tmp_path):
    v2 = session_dir / f"{SESSION_ID}_leg_sensor_data.csv"
    with open(v2, "w", encoding="utf-8", newline="") as f:
        f.write(
            f"# session_id={SESSION_ID},role=leg,schema_version=2,device_id=dev3,"
            f"nominal_hz=100.0\n"
            "timestamp_ms,acc_x_g,acc_y_g,acc_z_g,gyro_x_degs,gyro_y_degs,gyro_z_degs,"
            "label_id,label_name,sequence_number,device_id,acc_ts_ms,gyro_ts_ms,"
            "sample_kind\n"
        )
        # each acc triple repeats once (second row held, sample_kind=1) -> matches
        # run detection, so agreement_pct must be 100.0.
        for i in range(20):
            acc = float(i)
            f.write(f"{i * 20},{acc},0,0,0,0,0,0,0,{i * 2},dev3,{i * 20},{i * 20},0\n")
            f.write(f"{i * 20 + 10},{acc},0,0,0,0,0,0,0,{i * 2 + 1},dev3,"
                    f"{i * 20 + 10},{i * 20 + 10},1\n")

    result = mod.analyse_path(session_dir)
    leg = next(d for d in result["devices"] if d["kind"] == "sensor_data" and
               d["role"] == "leg")
    assert leg["declared"] is not None
    assert leg["declared"]["agreement_pct"] == 100.0
    assert leg["declared"]["held_row_pct_declared"] > 45


def test_no_matching_csvs_exits_2(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "other.txt").write_text("junk")
    with pytest.raises(SystemExit) as exc:
        mod.analyse_path(empty)
    assert exc.value.code == 2


def test_missing_path_exits_2(tmp_path):
    with pytest.raises(SystemExit) as exc:
        mod.analyse_path(tmp_path / "nope")
    assert exc.value.code == 2


def _v1_row_ext(t, acc, seq, dev):
    return f"{t},{acc},0.0,0.0,0.0,0.0,0.0,0,0,{seq},{dev}"


def test_multi_device_file_splits(tmp_path):
    d = tmp_path / "multi"
    d.mkdir()
    fname = d / f"{SESSION_ID}_consolidated.csv"
    with open(fname, "w", encoding="utf-8", newline="") as f:
        f.write(
            f"# session_id={SESSION_ID},schema_version=1\n"
            "timestamp_ms,acc_x_g,acc_y_g,acc_z_g,gyro_x_degs,gyro_y_degs,gyro_z_degs,"
            "label_id,label_name,sequence_number,device_id\n"
        )
        # device A: half-rate (each acc triple repeats once -> held ~50 %)
        # device B: clean (all-distinct). Rows interleaved by timestamp.
        for k in range(40):
            pair, member = k // 2, k % 2
            a_acc = float(pair)
            b_acc = float(k)
            f.write(_v1_row_ext(k * 20 + 0, a_acc, k * 2, "devA") + "\n")
            f.write(_v1_row_ext(k * 20 + 10, b_acc, k, "devB") + "\n")

    result = mod.analyse_path(d)
    entries = [e for e in result["devices"] if e["file"] == f"{SESSION_ID}_consolidated.csv"]
    assert len(entries) == 2

    dev_a = next(e for e in entries if e["device_id"] == "devA")
    dev_b = next(e for e in entries if e["device_id"] == "devB")

    assert dev_a["verdict"] == "FAIL"
    assert abs(dev_a["held_row_pct"] - 50) < 3
    assert abs(dev_a["true_sensor_hz"] - dev_a["nominal_hz"] / 2) < 1

    assert dev_b["verdict"] == "PASS"
    assert dev_b["held_row_pct"] == 0.0
    assert abs(dev_b["true_sensor_hz"] - dev_b["nominal_hz"]) < 1


def test_single_device_file_unchanged(session_dir):
    result = mod.analyse_path(session_dir)
    clean = next(e for e in result["devices"]
                 if e["file"] == f"{SESSION_ID}_chest_consolidated.csv")
    one = [e for e in result["devices"]
           if e["file"] == f"{SESSION_ID}_chest_consolidated.csv"]
    assert len(one) == 1
    assert clean["held_row_pct"] == 0.0
    assert abs(clean["true_sensor_hz"] - clean["nominal_hz"]) < 1
    assert clean["device_id"] == "dev"


def test_device_id_populated(session_dir, tmp_path):
    zpath = tmp_path / "export.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for p in session_dir.rglob("*.csv"):
            zf.write(p, arcname=p.name)
    result = mod.analyse_path(zpath)
    assert len(result["devices"]) >= 2
    for entry in result["devices"]:
        assert entry["device_id"]
        assert entry["file"]
        assert entry["verdict"] in ("PASS", "PARTIAL", "FAIL")
