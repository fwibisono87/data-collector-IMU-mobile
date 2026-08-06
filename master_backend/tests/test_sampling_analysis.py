from master_backend.app.sampling_analysis import (
    analyse_device,
    classify,
    DEFAULT_THRESHOLDS,
)


def _row(t, a, b=0.0, c=0.0, gx=0.0, gy=0.0, gz=0.0, seq=0):
    return [str(t), str(a), str(b), str(c), str(gx), str(gy), str(gz),
            "0", "0", str(seq), "dev", "0", "0", ""]


def test_held_duplicated_series():
    rows = []
    for i in range(50):
        rows.append(_row(i * 20, float(i), seq=i * 2))
        rows.append(_row(i * 20 + 10, float(i), seq=i * 2 + 1))
    stats = analyse_device(rows, expected_hz=100.0)
    assert abs(stats["held_row_pct"] - 50) < 1
    assert abs(stats["true_sensor_hz"] - stats["nominal_hz"] / 2) < 1
    assert stats["acc_run_length_hist"].get(2, 0) > 45


def test_clean_series():
    rows = []
    for i in range(100):
        rows.append(_row(i * 10, float(i), seq=i))
    stats = analyse_device(rows, expected_hz=100.0)
    assert stats["held_row_pct"] == 0
    assert abs(stats["true_sensor_hz"] - stats["nominal_hz"]) < 1


def test_classify_fail():
    stats = analyse_device([_row(i * 20, 1.0, seq=i) for i in range(20)],
                           expected_hz=100.0)
    stats["true_sensor_hz"] = stats["reference_hz"] * 0.5
    verdict, reasons = classify(stats)
    assert verdict == "FAIL"


def test_classify_pass():
    stats = analysis = analyse_device(
        [_row(i * 10, float(i), seq=i) for i in range(100)], expected_hz=100.0)
    stats["true_sensor_hz"] = stats["reference_hz"] * 0.98
    verdict, reasons = classify(stats)
    assert verdict == "PASS"


def test_classify_partial_gap():
    stats = analyse_device([_row(i * 10, float(i), seq=i) for i in range(100)],
                           expected_hz=100.0)
    stats["true_sensor_hz"] = stats["reference_hz"] * 0.98
    stats["sequence"]["missing_pct"] = 0.5
    verdict, reasons = classify(stats)
    assert verdict == "PARTIAL"


def test_empty_and_single_row():
    assert analyse_device([])["rows"] == 0
    stats = analyse_device([_row(0, 1.0)])
    assert stats["rows"] == 1
    assert stats["true_sensor_hz"] == 0.0
