"""Detect zero-order hold (ZOH) resampling in IMU telemetry CSV data.

Pure stdlib. No imports from other project modules so this can be used by a
standalone CLI script.
"""

import statistics
from collections import Counter

COL_ACC_X = 1
COL_ACC_Y = 2
COL_ACC_Z = 3
COL_GYRO_X = 4
COL_GYRO_Y = 5
COL_GYRO_Z = 6
COL_SEQUENCE = 9
COL_SAMPLE_KIND = 13

DEFAULT_THRESHOLDS = {
    "rate_partial_frac": 0.90,
    "rate_fail_frac": 0.70,
    "seq_gap_partial_pct": 0.1,
    "seq_gap_fail_pct": 1.0,
}


def _zero() -> dict:
    return {
        "device_id": "",
        "role": "",
        "rows": 0,
        "span_s": 0.0,
        "nominal_hz": 0.0,
        "true_sensor_hz": 0.0,
        "held_row_pct": 0.0,
        "acc_run_length_hist": {},
        "dt_ms": {
            "median": 0.0,
            "mean": 0.0,
            "p95": 0,
            "max": 0,
            "min": 0,
            "non_positive": 0,
        },
        "sequence": {
            "min": 0,
            "max": 0,
            "present": 0,
            "missing": 0,
            "missing_pct": 0.0,
            "largest_gap": 0,
            "duplicates": 0,
        },
        "declared": None,
        "reference_hz": 0.0,
    }


def analyse_device(rows, *, device_id: str = "", role: str = "",
                   expected_hz: float | None = None) -> dict:
    stats = _zero()
    stats["device_id"] = device_id
    stats["role"] = role
    stats["rows"] = len(rows)

    timestamps = []
    seqs = []
    for row in rows:
        try:
            timestamps.append(int(row[0]))
        except (ValueError, IndexError):
            continue
        try:
            seqs.append(int(row[COL_SEQUENCE]))
        except (ValueError, IndexError):
            continue

    usable = len(timestamps)
    if usable < 2:
        reference = expected_hz if (expected_hz and expected_hz > 0) else 0.0
        stats["reference_hz"] = reference
        return stats

    first_ts = timestamps[0]
    last_ts = timestamps[-1]
    span_s = (last_ts - first_ts) / 1000.0
    stats["span_s"] = span_s
    stats["nominal_hz"] = usable / span_s if span_s else 0.0

    dts = []
    for a, b in zip(timestamps, timestamps[1:]):
        dts.append(b - a)
    non_positive = sum(1 for d in dts if d <= 0)
    sorted_d = sorted(dts)
    stats["dt_ms"] = {
        "median": statistics.median(dts),
        "mean": statistics.mean(dts),
        "p95": sorted_d[int(len(sorted_d) * 0.95)] if sorted_d else 0,
        "max": max(dts) if dts else 0,
        "min": min(dts) if dts else 0,
        "non_positive": non_positive,
    }

    runs = []
    run_len = 0
    prev_triple = None
    for row in rows:
        triple = (row[COL_ACC_X], row[COL_ACC_Y], row[COL_ACC_Z])
        if triple == prev_triple:
            run_len += 1
        else:
            if prev_triple is not None:
                runs.append(run_len)
            run_len = 1
            prev_triple = triple
    if prev_triple is not None:
        runs.append(run_len)
    distinct_acc_events = len(runs)
    stats["true_sensor_hz"] = distinct_acc_events / span_s if span_s else 0.0
    stats["acc_run_length_hist"] = dict(sorted(Counter(runs).items()))

    held_count = 0
    prev_six = None
    for row in rows:
        six = (row[COL_ACC_X], row[COL_ACC_Y], row[COL_ACC_Z],
               row[COL_GYRO_X], row[COL_GYRO_Y], row[COL_GYRO_Z])
        if six == prev_six:
            held_count += 1
        prev_six = six
    stats["held_row_pct"] = 100.0 * held_count / usable if usable else 0.0

    present = len(set(seqs))
    if seqs:
        seq_min = min(seqs)
        seq_max = max(seqs)
        span = seq_max - seq_min + 1
        missing = max(0, span - present)
        missing_pct = 100.0 * missing / span if span else 0.0
        present_sorted = sorted(set(seqs))
        largest_gap = 0
        if len(present_sorted) > 1:
            largest_gap = max(b - a - 1 for a, b in zip(present_sorted, present_sorted[1:]))
        duplicates = len(seqs) - present
    else:
        seq_min = 0
        seq_max = 0
        missing = 0
        missing_pct = 0.0
        largest_gap = 0
        duplicates = 0
    stats["sequence"] = {
        "min": seq_min,
        "max": seq_max,
        "present": present,
        "missing": missing,
        "missing_pct": missing_pct,
        "largest_gap": largest_gap,
        "duplicates": duplicates,
    }

    declared_rows = [r for r in rows if len(r) > COL_SAMPLE_KIND and r[COL_SAMPLE_KIND] != ""]
    if declared_rows:
        declared_held = sum(1 for r in declared_rows if r[COL_SAMPLE_KIND] == "1")
        held_row_pct_declared = 100.0 * declared_held / len(declared_rows)
        agree = 0
        prev_six = None
        count = 0
        for r in declared_rows:
            six = (r[COL_ACC_X], r[COL_ACC_Y], r[COL_ACC_Z],
                   r[COL_GYRO_X], r[COL_GYRO_Y], r[COL_GYRO_Z])
            detected_held = 1 if six == prev_six else 0
            if detected_held == int(r[COL_SAMPLE_KIND]):
                agree += 1
            prev_six = six
            count += 1
        agreement_pct = 100.0 * agree / count if count else 0.0
        stats["declared"] = {
            "held_row_pct_declared": held_row_pct_declared,
            "agreement_pct": agreement_pct,
        }

    reference = expected_hz if (expected_hz and expected_hz > 0) else stats["nominal_hz"]
    stats["reference_hz"] = reference
    return stats


def classify(stats: dict, thresholds: dict | None = None) -> tuple:
    merged = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        merged.update(thresholds)

    reference = stats.get("reference_hz", stats.get("nominal_hz", 0.0)) or 0.0
    frac = stats["true_sensor_hz"] / reference if reference else 0.0

    reasons = []
    verdict = "PASS"

    if frac < merged["rate_fail_frac"]:
        reasons.append(f"true rate {stats['true_sensor_hz']:.1f} Hz is "
                       f"{100.0 * frac:.1f}% of reference {reference:.2f} Hz")
        verdict = "FAIL"
    elif frac < merged["rate_partial_frac"]:
        reasons.append(f"true rate {stats['true_sensor_hz']:.1f} Hz is "
                       f"{100.0 * frac:.1f}% of reference {reference:.2f} Hz")
        verdict = "PARTIAL"

    missing_pct = stats["sequence"]["missing_pct"]
    if missing_pct > merged["seq_gap_fail_pct"]:
        reasons.append(f"sequence gap {missing_pct:.2f}% exceeds fail threshold")
        verdict = "FAIL"
    elif missing_pct > merged["seq_gap_partial_pct"]:
        reasons.append(f"sequence gap {missing_pct:.2f}% exceeds partial threshold")
        if verdict != "FAIL":
            verdict = "PARTIAL"

    return verdict, reasons
