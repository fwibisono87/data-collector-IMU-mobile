"""
Post-session integrity checks (CLAUDE.md §10, §22.8).
Phase 2: basic row count + SHA-256.
Phase 4: cross-device start drift, offline intervals, role uniqueness.
Phase 6: per-device sampling-rate (ZOH) + sequence-gap analysis via sampling_analysis.
"""
import asyncio
import json
import os
import time
from pathlib import Path

from .audit_logger import audit
from .csv_schema import is_header_line, is_valid_data_row, parse_row
from .io_manager import io_manager
from .sampling_analysis import analyse_device, classify

# Thresholds default here; overridable per-run through env (read inside run so tests
# can monkeypatch os.environ).
DEFAULT_MAX_DRIFT_MS = 100


def _total_offline_ms(intervals: list) -> int:
    now = int(time.time() * 1000)
    return sum((iv.get("end_ms") or now) - iv["start_ms"] for iv in intervals)


def _telemetry_gaps(dev) -> list:
    return getattr(dev, "telemetry_gaps", None) or []


def _total_telemetry_gap_ms(gaps: list) -> int:
    now = int(time.time() * 1000)
    return sum((gap.get("end_ms") or now) - gap["start_ms"] for gap in gaps)


def _analyse_csv(path: str, device_id: str, role: str) -> dict:
    """Read a CSV off the event loop and run sampling analysis on its rows.

    A missing/unreadable file degrades gracefully: returns {} so the caller knows the
    sampling checks could not run but is not forced to raise.
    """
    rows = []
    invalid_rows = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                fields = parse_row(line)
                # `device_id or None` — an empty id means "do not filter by device", which
                # is how the consolidated per-role pass calls this: those files hold every
                # device for the role, so filtering on "" would reject every single row.
                if fields is not None and is_valid_data_row(
                    fields, expected_device_id=device_id or None
                ):
                    rows.append(fields)
                elif line.strip() and not line.lstrip().startswith("#") and not is_header_line(line):
                    invalid_rows += 1
    except OSError:
        return {}
    stats = analyse_device(rows, device_id=device_id, role=role)
    stats["invalid_rows"] = invalid_rows
    return stats


_VERDICT_RANK = {"PASS": 0, "PARTIAL": 1, "FAIL": 2}


def _worst_status(chosen: dict) -> str:
    """Worst (most severe) verdict from a dict of per-scope statuses.

    Ranks PASS=0, PARTIAL=1, FAIL=2 and takes the max, so ordering bugs are impossible.
    """
    worst = "PASS"
    for status in chosen.values():
        if _VERDICT_RANK[status] > _VERDICT_RANK[worst]:
            worst = status
    return worst


def _escalate(current: str, candidate: str) -> str:
    """Raise a verdict, never lower it.

    Every per-device check must escalate rather than assign: a device that is BOTH
    half-rate (FAIL) and had offline intervals (PARTIAL) must stay FAIL. A plain
    assignment in a later check silently downgrades an earlier, more severe verdict.
    """
    return candidate if _VERDICT_RANK[candidate] > _VERDICT_RANK[current] else current


def validate_consolidated(
    session_id: str,
    per_role_files: list[tuple[str, Path]],
    validated_at_ms: int | None = None,
) -> dict:
    """Check the final per-role files after rescue/late rows have been merged.

    The stop-time report remains authoritative for rate and disconnect evidence. This
    second pass specifically answers the question that cannot be answered at STOP:
    did the final merged files still contain sequence gaps after recovery arrived?
    """
    thresholds = {
        "seq_gap_partial_pct": float(os.getenv("INTEGRITY_SEQ_GAP_PARTIAL_PCT", "0.1")),
        "seq_gap_fail_pct": float(os.getenv("INTEGRITY_SEQ_GAP_FAIL_PCT", "1.0")),
    }
    roles: list[dict] = []
    status = "PASS"
    for role, path in per_role_files:
        stats = _analyse_csv(str(path), "", role)
        role_status = "PASS"
        reasons: list[str] = []
        if not stats:
            role_status = "FAIL"
            reasons.append("consolidated CSV could not be read")
            sequence = {}
            rows = 0
        else:
            rows = stats["rows"]
            sequence = stats["sequence"]
            missing_pct = sequence["missing_pct"]
            if rows == 0:
                role_status = "FAIL"
                reasons.append("consolidated CSV has zero rows")
            elif missing_pct > thresholds["seq_gap_fail_pct"]:
                role_status = "FAIL"
                reasons.append(f"sequence gap {missing_pct:.2f}% exceeds fail threshold")
            elif missing_pct > thresholds["seq_gap_partial_pct"]:
                role_status = "PARTIAL"
                reasons.append(f"sequence gap {missing_pct:.2f}% exceeds partial threshold")
        status = _escalate(status, role_status)
        roles.append({
            "role": role,
            "path": str(path),
            "rows": rows,
            "status": role_status,
            "reasons": reasons,
            "sequence": sequence,
        })

    if not roles:
        status = "FAIL"

    return {
        "session_id": session_id,
        "status": status,
        "validated_at_ms": validated_at_ms or int(time.time() * 1000),
        "validation_scope": "consolidated_per_role",
        "thresholds": thresholds,
        "roles": roles,
    }


class IntegrityValidator:
    async def run(
        self,
        session_id: str,
        file_results: dict,
        devices: list,
        scheduled_start_ms: int = 0,
        label_timeline: list[dict] | None = None,
        session_start_ms: int = 0,
        session_end_ms: int = 0,
        io_source=None,
        validation_scope: str = "main_csv_at_stop",
    ) -> dict:
        # Thresholds are read at call time (not cached at import) so callers/tests can
        # override them through os.environ.
        max_drift_ms = int(os.getenv("INTEGRITY_MAX_DRIFT_MS", str(DEFAULT_MAX_DRIFT_MS)))
        thresholds = {
            "rate_partial_frac": float(os.getenv("INTEGRITY_RATE_PARTIAL_FRAC", "0.90")),
            "rate_fail_frac": float(os.getenv("INTEGRITY_RATE_FAIL_FRAC", "0.70")),
            "seq_gap_partial_pct": float(os.getenv("INTEGRITY_SEQ_GAP_PARTIAL_PCT", "0.1")),
            "seq_gap_fail_pct": float(os.getenv("INTEGRITY_SEQ_GAP_FAIL_PCT", "1.0")),
        }
        coverage_gap_ms = int(os.getenv("INTEGRITY_COVERAGE_GAP_MS", "2000"))
        metrics = io_source or io_manager

        report: dict = {
            "session_id": session_id,
            "status": "PASS",
            "validated_at_ms": int(time.time() * 1000),
            # Validation runs at STOP, before late-delivery sidecars and phone recovery
            # uploads land, so these numbers describe the main CSVs only.
            "validation_scope": validation_scope,
            "label_timeline": list(label_timeline or []),
            "session_start_ms": session_start_ms,
            "session_end_ms": session_end_ms,
            "devices": [],
            "cross_device_checks": {},
            "analysis_ready": True,
            "analysis_ready_reasons": [],
        }

        timeline = sorted(label_timeline or [], key=lambda x: int(x.get("timestamp_ms", 0)))

        def label_at(timestamp_ms: int) -> dict:
            active = {"timestamp_ms": 0, "label_id": 0, "label_name": "0"}
            for transition in timeline:
                if int(transition.get("timestamp_ms", 0)) <= timestamp_ms:
                    active = transition
                else:
                    break
            return active

        def classify_gap(gap: dict) -> dict:
            start_ms = int(gap.get("start_ms", 0))
            end_ms = int(gap.get("end_ms") or session_end_ms or start_ms)
            end_ms = max(start_ms, end_ms)
            start_label = label_at(start_ms)
            end_label = label_at(end_ms)
            nonzero_transition = any(
                int(t.get("label_id", 0)) != 0
                and start_ms < int(t.get("timestamp_ms", 0)) <= end_ms
                for t in timeline
            )
            nonzero_overlap = nonzero_transition or int(start_label.get("label_id", 0)) != 0
            zero_transition = next(
                (
                    int(t.get("timestamp_ms", 0))
                    for t in timeline
                    if start_ms <= int(t.get("timestamp_ms", 0)) <= end_ms
                    and int(t.get("label_id", 0)) == 0
                ),
                None,
            )
            # A short, zero-label boundary glitch can be tolerated.  A long gap while no
            # task label is active is still missing IMU data and must not become a false
            # PASS merely because the operator had not pressed a label button yet.
            waived = (
                end_ms - start_ms <= 10_000
                and int(end_label.get("label_id", 0)) == 0
                and not nonzero_transition
                and (
                    int(start_label.get("label_id", 0)) == 0
                    or zero_transition is not None
                )
            )
            return {
                **gap,
                "duration_ms": end_ms - start_ms,
                "start_label_id": int(start_label.get("label_id", 0)),
                "end_label_id": int(end_label.get("label_id", 0)),
                "nonzero_label_overlap": nonzero_overlap,
                "nonzero_transition": nonzero_transition,
                "zero_transition_ms": zero_transition,
                "waived": waived,
            }

        sampling_devices = []

        # ── Per-device checks ─────────────────────────────────────────────────
        for device_id, result in file_results.items():
            path = Path(result["path"])
            rows = result["rows"]
            sha = result["sha256"]

            dev_obj = next((d for d in devices if d.device_id == device_id), None)
            role = dev_obj.device_role if dev_obj else "unknown"

            device_report = {
                "device_id": device_id,
                "role": role,
                "csv_path": str(path),
                "row_count": rows,
                "csv_sha256": sha,
                "status": "PASS",
                "reasons": [],
                "first_packet_ts": dev_obj.first_packet_ts if dev_obj else None,
                "offline_intervals": dev_obj.offline_intervals if dev_obj else [],
                "packets_received": dev_obj.packets_received if dev_obj else 0,
                "offline_interval_count": len(dev_obj.offline_intervals) if dev_obj else 0,
                "offline_total_ms": _total_offline_ms(dev_obj.offline_intervals) if dev_obj else 0,
                "telemetry_gaps": _telemetry_gaps(dev_obj) if dev_obj else [],
                "telemetry_gap_count": len(_telemetry_gaps(dev_obj)) if dev_obj else 0,
                "telemetry_gap_total_ms": (
                    _total_telemetry_gap_ms(_telemetry_gaps(dev_obj)) if dev_obj else 0
                ),
                "rows_reordered": result.get("reordered", 0),
                "packets_dropped_no_writer": metrics.dropped_no_writer(device_id),
                "csv_write_failures": metrics.write_failures(device_id),
                "rows_lost_after_failover": metrics.rows_lost_after_failover(device_id),
                "analysis_ready": True,
            }

            if result.get("close_failed"):
                device_report["status"] = _escalate(device_report["status"], "FAIL")
                device_report["analysis_ready"] = False
                device_report["reasons"].append(
                    f"CSV close failed: {result.get('close_error', 'unknown error')}"
                )
                report["analysis_ready"] = False
                report["analysis_ready_reasons"].append(f"{role}: CSV close failed")

            if rows == 0:
                device_report["status"] = _escalate(device_report["status"], "FAIL")
                device_report["analysis_ready"] = False
                report["analysis_ready"] = False
                report["analysis_ready_reasons"].append(f"{role}: zero rows written")
                device_report["reasons"].append("zero rows written")
                device_report["issue"] = "zero rows written"

            # Sampling analysis: read the device's CSV off the event loop (three ~25 MB
            # CSVs take ~1s) and classify rate/sequence health.
            stats = await asyncio.get_event_loop().run_in_executor(
                None, _analyse_csv, str(path), device_id, role
            )
            if not stats:
                device_report["reasons"].append("no csv available for sampling analysis")
                device_report["issue"] = "no csv available for sampling analysis"
                device_report["analysis_ready"] = False
                report["analysis_ready"] = False
                report["analysis_ready_reasons"].append(
                    f"{role}: no CSV available for sampling analysis"
                )
            else:
                sampling_verdict, sampling_reasons = classify(stats, thresholds)
                device_report["reasons"].extend(sampling_reasons)
                device_report["status"] = _escalate(device_report["status"], sampling_verdict)
                if sampling_verdict == "FAIL" or any(
                    reason.startswith("true rate") for reason in sampling_reasons
                ):
                    device_report["analysis_ready"] = False
                    report["analysis_ready"] = False
                    report["analysis_ready_reasons"].append(
                        f"{role}: sampling-rate acceptance failed"
                    )

                device_report["sampling"] = {
                    "nominal_hz": stats["nominal_hz"],
                    "true_sensor_hz": stats["true_sensor_hz"],
                    "held_row_pct": stats["held_row_pct"],
                    "reference_hz": stats["reference_hz"],
                    "dt_ms": stats["dt_ms"],
                    "sequence": stats["sequence"],
                }
                expected_start_ms = scheduled_start_ms or session_start_ms
                first_ts = stats.get("first_timestamp_ms")
                last_ts = stats.get("last_timestamp_ms")
                leading_gap_ms = (
                    max(0, int(first_ts) - int(expected_start_ms))
                    if expected_start_ms and first_ts is not None else 0
                )
                trailing_gap_ms = (
                    max(0, int(session_end_ms) - int(last_ts))
                    if session_end_ms and last_ts is not None else 0
                )
                device_report["coverage"] = {
                    "expected_start_ms": expected_start_ms,
                    "first_timestamp_ms": first_ts,
                    "last_timestamp_ms": last_ts,
                    "session_end_ms": session_end_ms,
                    "leading_gap_ms": leading_gap_ms,
                    "trailing_gap_ms": trailing_gap_ms,
                    "accepted_gap_ms": coverage_gap_ms,
                }
                if leading_gap_ms > coverage_gap_ms:
                    device_report["status"] = _escalate(device_report["status"], "PARTIAL")
                    device_report["analysis_ready"] = False
                    device_report["reasons"].append(
                        f"data starts {leading_gap_ms} ms after coordinated start"
                    )
                    report["analysis_ready"] = False
                    report["analysis_ready_reasons"].append(
                        f"{role}: leading coverage gap {leading_gap_ms} ms"
                    )
                if trailing_gap_ms > coverage_gap_ms:
                    device_report["status"] = _escalate(device_report["status"], "PARTIAL")
                    device_report["analysis_ready"] = False
                    device_report["reasons"].append(
                        f"data ends {trailing_gap_ms} ms before session end"
                    )
                    report["analysis_ready"] = False
                    report["analysis_ready_reasons"].append(
                        f"{role}: trailing coverage gap {trailing_gap_ms} ms"
                    )
                device_report["invalid_rows"] = stats.get("invalid_rows", 0)
                device_report["label_aware_sequence_gaps"] = [
                    classify_gap(gap) for gap in stats["sequence"].get("gaps", [])
                ]
                if stats.get("invalid_rows", 0):
                    device_report["status"] = _escalate(device_report["status"], "FAIL")
                    device_report["analysis_ready"] = False
                    device_report["reasons"].append(
                        f"{stats['invalid_rows']} malformed CSV rows"
                    )
                    report["analysis_ready"] = False
                    report["analysis_ready_reasons"].append(
                        f"{role}: malformed CSV rows"
                    )
                sampling_devices.append({
                    **stats,
                    "verdict": sampling_verdict,
                    "reasons": sampling_reasons,
                })

            # Flag sessions with offline intervals
            if dev_obj and dev_obj.offline_intervals:
                device_report["status"] = _escalate(device_report["status"], "PARTIAL")
                device_report["reasons"].append("device had offline intervals")
                device_report["offline_intervals_label_aware"] = [
                    classify_gap({
                        "start_ms": interval.get("start_ms", 0),
                        "end_ms": interval.get("end_ms") or session_end_ms,
                        "source": interval.get("source", "unknown"),
                    })
                    for interval in dev_obj.offline_intervals
                ]

            unwaived_gaps = [
                gap for gap in device_report.get("offline_intervals_label_aware", [])
                if not gap["waived"]
            ] + [
                gap for gap in device_report.get("label_aware_sequence_gaps", [])
                if not gap["waived"]
            ]
            if unwaived_gaps:
                device_report["analysis_ready"] = False
                report["analysis_ready"] = False
                device_report["reasons"].append(
                    f"{len(unwaived_gaps)} gap(s) overlap non-zero labels"
                )
                for gap in unwaived_gaps:
                    report["analysis_ready_reasons"].append(
                        f"{role}: {gap.get('duration_ms', 0)} ms gap overlaps non-zero label"
                    )

            if dev_obj and _telemetry_gaps(dev_obj):
                device_report["status"] = _escalate(device_report["status"], "PARTIAL")
                device_report["reasons"].append("device had telemetry-only gaps")

            # Packets the operator believed were captured (the dashboard counted them)
            # but that never reached disk are a hard failure, not merely PARTIAL (plan D2).
            if device_report["packets_dropped_no_writer"] > 0:
                device_report["status"] = _escalate(device_report["status"], "FAIL")
                device_report["reasons"].append(
                    f"{device_report['packets_dropped_no_writer']} packets had no open writer"
                )
                device_report["issue"] = (
                    f"{device_report['packets_dropped_no_writer']} packets had no open writer"
                )
                device_report["analysis_ready"] = False
                report["analysis_ready"] = False

            # A failed SSD is recoverable only if the exact failed row was written to the
            # rescue volume. Any row lost during that hand-off is a hard data-integrity fail.
            if device_report["rows_lost_after_failover"] > 0:
                device_report["status"] = _escalate(device_report["status"], "FAIL")
                device_report["reasons"].append(
                    f"{device_report['rows_lost_after_failover']} rows lost after writer failover"
                )
                device_report["issue"] = (
                    f"{device_report['rows_lost_after_failover']} rows lost after writer failover"
                )
                device_report["analysis_ready"] = False
                report["analysis_ready"] = False

            report["devices"].append(device_report)

        # ── Cross-device checks (Phase 4, CLAUDE.md §22.8) ───────────────────
        all_checks = {d["device_id"]: d["status"] for d in report["devices"]}

        cross_device_checks = {
            "role_uniqueness": "",
            "start_drift_threshold_ms": max_drift_ms,
            "start_drift_ok": True,
            "scheduled_start_ms": scheduled_start_ms,
            "device_count": len(file_results),
            "expected_device_count": len(devices),
            # file_results values come from io_manager.close_session(), whose keys are
            # {path, rows, sha256, reordered} — "rows", not "row_count". Reading the wrong key
            # made this default to 0 for every device, so the flag reported False even for a
            # session where all three devices wrote 103k rows (2026-08-11, Grace_Testing_Sesi_Pagi).
            "all_devices_completed": (
                bool(devices)
                and set(file_results) == {d.device_id for d in devices}
                and all(r.get("rows", 0) > 0 for r in file_results.values())
            ),
            "missing_devices_intervals": [
                {
                    "device_id": d.device_id,
                    "role": d.device_role,
                    "intervals": d.offline_intervals,
                }
                for d in devices
                if d.offline_intervals
            ],
            "telemetry_gaps": [
                {
                    "device_id": d.device_id,
                    "role": d.device_role,
                    "intervals": _telemetry_gaps(d),
                }
                for d in devices
                if _telemetry_gaps(d)
            ],
        }

        if len(file_results) > 1 and scheduled_start_ms:
            first_timestamps = [
                d.first_packet_ts for d in devices
                if d.first_packet_ts is not None
            ]
            if first_timestamps:
                max_drift = max(first_timestamps) - min(first_timestamps)
                drift_ok = max_drift <= max_drift_ms
                # Drift alone is a warning (PARTIAL), not a hard failure — see the
                # priority-inversion fix in the lane spec.
                if not drift_ok:
                    all_checks["start_drift"] = "PARTIAL"
                else:
                    all_checks["start_drift"] = "PASS"

                cross_device_checks["max_start_drift_ms"] = max_drift
                cross_device_checks["start_drift_ok"] = drift_ok

        # Roles uniqueness check
        roles = [d.device_role for d in devices]
        role_uniqueness = "pass" if len(roles) == len(set(roles)) else "fail"
        cross_device_checks["role_uniqueness"] = role_uniqueness
        if role_uniqueness == "fail":
            all_checks["role_uniqueness"] = "PARTIAL"

        report["cross_device_checks"] = cross_device_checks
        if not cross_device_checks["all_devices_completed"]:
            report["analysis_ready"] = False
            report["analysis_ready_reasons"].append("not every expected device produced a CSV")
            all_checks["all_devices_completed"] = "FAIL"

        # Overall status = worst of every per-device verdict and every cross-device check.
        report["status"] = _worst_status(all_checks)
        if role_uniqueness == "fail":
            report["analysis_ready"] = False
            report["analysis_ready_reasons"].append("duplicate device roles")
        if cross_device_checks.get("start_drift_ok") is False:
            report["analysis_ready"] = False
            report["analysis_ready_reasons"].append("cross-device start drift exceeded threshold")

        # Write report
        if file_results:
            first_path = Path(list(file_results.values())[0]["path"])
            report_path = first_path.parent / f"{session_id}_integrity_report.json"
            try:
                report_path.write_text(json.dumps(report, indent=2))
            except OSError as exc:
                await audit.log("ERROR", "integrity_report_write_failed", {"error": str(exc)})

            # Standalone connectivity.json — the machine-readable answer to "kenapa
            # partial" (peer complaint #3).
            conn = {
                "session_id": session_id,
                "devices": [
                    {
                        "device_id": d.device_id,
                        "role": d.device_role,
                        "intervals": d.offline_intervals,
                        "total_offline_ms": _total_offline_ms(d.offline_intervals),
                        "telemetry_gaps": _telemetry_gaps(d),
                        "total_telemetry_gap_ms": _total_telemetry_gap_ms(_telemetry_gaps(d)),
                    }
                    for d in devices
                ],
            }
            try:
                (first_path.parent / f"{session_id}_connectivity.json").write_text(
                    json.dumps(conn, indent=2)
                )
            except OSError as exc:
                await audit.log("ERROR", "connectivity_report_write_failed", {"error": str(exc)})

            # Standalone sampling.json — full analyse_device output incl. the
            # acc_run_length_hist, kept out of the integrity report for readability.
            sampling_report = {
                "session_id": session_id,
                "generated_at_ms": int(time.time() * 1000),
                "schema_version": 2,
                "thresholds": {
                    **thresholds,
                    "max_drift_ms": max_drift_ms,
                },
                "devices": sampling_devices,
            }
            try:
                (first_path.parent / f"{session_id}_sampling.json").write_text(
                    json.dumps(sampling_report, indent=2)
                )
            except OSError as exc:
                await audit.log("ERROR", "sampling_report_write_failed", {"error": str(exc)})

        return report
