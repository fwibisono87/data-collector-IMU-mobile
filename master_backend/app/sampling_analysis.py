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
        "first_timestamp_ms": None,
        "last_timestamp_ms": None,
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
            "gaps": [],
        },
        "invalid_rows": 0,
        "declared": None,
        "reference_hz": 0.0,
    }


def analyse_rows(row_iter, *, device_id: str = "", role: str = "",
                 expected_hz: float | None = None) -> dict:
    """Single-pass sampling analysis over an iterable of parsed CSV rows.

    Streaming, because this runs at STOP on every device CSV of the session. The previous
    implementation took a fully materialised list and walked it four times; a forty-minute
    device at ~100 rows/s is ~240,000 rows, each a list of fourteen strings, so validating a
    three-device session allocated hundreds of megabytes at exactly the moment finalisation
    most needs to be reliable.

    Only two things are retained across the pass and both are small: the list of
    inter-sample deltas (needed for median/p95) and the set of sequence numbers (needed for
    presence and gap width). Everything else — run lengths, held-sample counts, sequence
    gap spans, declared-kind agreement — is accumulated incrementally.
    """
    stats = _zero()
    stats["device_id"] = device_id
    stats["role"] = role

    total_rows = 0
    usable = 0
    first_ts = last_ts = None
    min_ts = max_ts = None
    dts: list[int] = []
    prev_ts_for_dt = None

    seqs: set[int] = set()
    seq_total = 0
    seq_min = seq_max = None

    prev_pair = None                 # (timestamp, sequence) of the last fully-parsed row
    gaps: list[dict] = []

    run_lengths: Counter = Counter()
    run_len = 0
    prev_triple = None
    distinct_acc_events = 0

    held_count = 0
    prev_six = None

    declared_total = 0
    declared_held = 0
    declared_agree = 0
    declared_prev_six = None

    for row in row_iter:
        total_rows += 1

        try:
            timestamp = int(row[0])
        except (ValueError, IndexError):
            timestamp = None
        else:
            usable += 1
            if first_ts is None:
                first_ts = timestamp
            last_ts = timestamp
            min_ts = timestamp if min_ts is None else min(min_ts, timestamp)
            max_ts = timestamp if max_ts is None else max(max_ts, timestamp)
            if prev_ts_for_dt is not None:
                dts.append(timestamp - prev_ts_for_dt)
            prev_ts_for_dt = timestamp

        try:
            sequence = int(row[COL_SEQUENCE])
        except (ValueError, IndexError):
            sequence = None
        else:
            seqs.add(sequence)
            seq_total += 1
            seq_min = sequence if seq_min is None else min(seq_min, sequence)
            seq_max = sequence if seq_max is None else max(seq_max, sequence)

        if timestamp is not None and sequence is not None:
            if prev_pair is not None:
                previous_ts, previous_seq = prev_pair
                missing_in_run = sequence - previous_seq - 1
                # CSVs are timestamp-sorted on close; ignore replay/order reversals here
                # and only describe forward sequence loss.
                if missing_in_run > 0 and timestamp >= previous_ts:
                    gaps.append({
                        "start_ms": previous_ts,
                        "end_ms": timestamp,
                        "duration_ms": timestamp - previous_ts,
                        "missing": missing_in_run,
                        "previous_sequence": previous_seq,
                        "next_sequence": sequence,
                    })
            prev_pair = (timestamp, sequence)

        triple = (row[COL_ACC_X], row[COL_ACC_Y], row[COL_ACC_Z])
        if triple == prev_triple:
            run_len += 1
        else:
            if prev_triple is not None:
                run_lengths[run_len] += 1
                distinct_acc_events += 1
            run_len = 1
            prev_triple = triple

        six = (row[COL_ACC_X], row[COL_ACC_Y], row[COL_ACC_Z],
               row[COL_GYRO_X], row[COL_GYRO_Y], row[COL_GYRO_Z])
        if six == prev_six:
            held_count += 1
        prev_six = six

        if len(row) > COL_SAMPLE_KIND and row[COL_SAMPLE_KIND] != "":
            declared_total += 1
            if row[COL_SAMPLE_KIND] == "1":
                declared_held += 1
            detected_held = 1 if six == declared_prev_six else 0
            try:
                if detected_held == int(row[COL_SAMPLE_KIND]):
                    declared_agree += 1
            except ValueError:
                pass
            declared_prev_six = six

    if prev_triple is not None:
        run_lengths[run_len] += 1
        distinct_acc_events += 1

    stats["rows"] = total_rows

    if usable < 2:
        stats["reference_hz"] = expected_hz if (expected_hz and expected_hz > 0) else 0.0
        return stats

    stats["first_timestamp_ms"] = min_ts
    stats["last_timestamp_ms"] = max_ts
    span_s = (last_ts - first_ts) / 1000.0
    stats["span_s"] = span_s
    stats["nominal_hz"] = usable / span_s if span_s else 0.0

    sorted_d = sorted(dts)
    stats["dt_ms"] = {
        "median": statistics.median(dts),
        "mean": statistics.mean(dts),
        "p95": sorted_d[int(len(sorted_d) * 0.95)] if sorted_d else 0,
        "max": max(dts) if dts else 0,
        "min": min(dts) if dts else 0,
        "non_positive": sum(1 for d in dts if d <= 0),
    }

    stats["true_sensor_hz"] = distinct_acc_events / span_s if span_s else 0.0
    stats["acc_run_length_hist"] = dict(sorted(run_lengths.items()))
    # Denominator is the total row count, matching the loop above — `usable` counts only
    # rows with a parseable timestamp, so using it here would mix two row populations.
    stats["held_row_pct"] = 100.0 * held_count / total_rows if total_rows else 0.0

    present = len(seqs)
    if seq_total:
        span = seq_max - seq_min + 1
        missing = max(0, span - present)
        present_sorted = sorted(seqs)
        largest_gap = 0
        if len(present_sorted) > 1:
            largest_gap = max(b - a - 1 for a, b in zip(present_sorted, present_sorted[1:]))
        stats["sequence"] = {
            "min": seq_min,
            "max": seq_max,
            "present": present,
            "missing": missing,
            "missing_pct": 100.0 * missing / span if span else 0.0,
            "largest_gap": largest_gap,
            "duplicates": seq_total - present,
            "gaps": gaps,
        }
    else:
        stats["sequence"] = {
            "min": 0, "max": 0, "present": 0, "missing": 0, "missing_pct": 0.0,
            "largest_gap": 0, "duplicates": 0, "gaps": gaps,
        }

    if declared_total:
        stats["declared"] = {
            "held_row_pct_declared": 100.0 * declared_held / declared_total,
            "agreement_pct": 100.0 * declared_agree / declared_total,
        }

    stats["reference_hz"] = (
        expected_hz if (expected_hz and expected_hz > 0) else stats["nominal_hz"]
    )
    return stats


def analyse_device(rows, *, device_id: str = "", role: str = "",
                   expected_hz: float | None = None) -> dict:
    """List-taking wrapper around analyse_rows, kept for callers that already have rows."""
    return analyse_rows(rows, device_id=device_id, role=role, expected_hz=expected_hz)

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
