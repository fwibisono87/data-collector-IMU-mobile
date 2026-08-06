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
from .csv_schema import parse_row
from .io_manager import io_manager
from .sampling_analysis import analyse_device, classify

# Thresholds default here; overridable per-run through env (read inside run so tests
# can monkeypatch os.environ).
DEFAULT_MAX_DRIFT_MS = 100


def _total_offline_ms(intervals: list) -> int:
    now = int(time.time() * 1000)
    return sum((iv.get("end_ms") or now) - iv["start_ms"] for iv in intervals)


def _analyse_csv(path: str, device_id: str, role: str) -> dict:
    """Read a CSV off the event loop and run sampling analysis on its rows.

    A missing/unreadable file degrades gracefully: returns {} so the caller knows the
    sampling checks could not run but is not forced to raise.
    """
    rows = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                fields = parse_row(line)
                if fields is not None:
                    rows.append(fields)
    except OSError:
        return {}
    return analyse_device(rows, device_id=device_id, role=role)


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


class IntegrityValidator:
    async def run(
        self,
        session_id: str,
        file_results: dict,
        devices: list,
        scheduled_start_ms: int = 0,
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

        report: dict = {
            "session_id": session_id,
            "status": "PASS",
            "validated_at_ms": int(time.time() * 1000),
            # Validation runs at STOP, before late-delivery sidecars and phone recovery
            # uploads land, so these numbers describe the main CSVs only.
            "validation_scope": "main_csv_at_stop",
            "devices": [],
            "cross_device_checks": {},
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
                "rows_reordered": result.get("reordered", 0),
                "packets_dropped_no_writer": io_manager.dropped_no_writer(device_id),
            }

            if rows == 0:
                device_report["status"] = "FAIL"
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
            else:
                sampling_verdict, sampling_reasons = classify(stats, thresholds)
                device_report["reasons"].extend(sampling_reasons)
                if sampling_verdict == "FAIL":
                    device_report["status"] = "FAIL"
                elif sampling_verdict == "PARTIAL":
                    if device_report["status"] == "PASS":
                        device_report["status"] = "PARTIAL"

                device_report["sampling"] = {
                    "nominal_hz": stats["nominal_hz"],
                    "true_sensor_hz": stats["true_sensor_hz"],
                    "held_row_pct": stats["held_row_pct"],
                    "reference_hz": stats["reference_hz"],
                    "dt_ms": stats["dt_ms"],
                    "sequence": stats["sequence"],
                }
                sampling_devices.append({
                    **stats,
                    "verdict": sampling_verdict,
                    "reasons": sampling_reasons,
                })

            # Flag sessions with offline intervals
            if dev_obj and dev_obj.offline_intervals:
                device_report["status"] = "PARTIAL"
                device_report["reasons"].append("device had offline intervals")

            # Packets the operator believed were captured (the dashboard counted them)
            # but that never reached disk are a hard failure, not merely PARTIAL (plan D2).
            if device_report["packets_dropped_no_writer"] > 0:
                device_report["status"] = "FAIL"
                device_report["reasons"].append(
                    f"{device_report['packets_dropped_no_writer']} packets had no open writer"
                )
                device_report["issue"] = (
                    f"{device_report['packets_dropped_no_writer']} packets had no open writer"
                )

            report["devices"].append(device_report)

        # ── Cross-device checks (Phase 4, CLAUDE.md §22.8) ───────────────────
        all_checks = {d["device_id"]: d["status"] for d in report["devices"]}

        cross_device_checks = {
            "role_uniqueness": "",
            "start_drift_threshold_ms": max_drift_ms,
            "start_drift_ok": True,
            "scheduled_start_ms": scheduled_start_ms,
            "device_count": len(file_results),
            "all_devices_completed": all(
                r.get("row_count", 0) > 0 for r in file_results.values()
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

        # Overall status = worst of every per-device verdict and every cross-device check.
        report["status"] = _worst_status(all_checks)

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
