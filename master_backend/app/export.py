"""
Session export — the data half of the end-of-session download flow.

When the operator stops a session the dashboard opens a non-dismissible export modal
that pulls the phone rescue CSVs (RecoveryUploader), consolidates every source of the
session's samples (main writes, rescue-path fallbacks, late-delivery sidecars and phone
rescue uploads) into one CSV, and then bundles all artifacts — data + video — into a
single .zip on the client. This router is the backend contract behind that modal:

  GET  /export/{session_id}/manifest   -> authoritative snapshot from disk
  GET  /export/{session_id}/file?name= -> stream one session artifact
  POST /export/{session_id}/consolidate-> merge main + rescue + late + recovery into
                                          <session_id>_consolidated.csv plus one
                                          <session_id>_<role>_consolidated.csv per role
  POST /export/{session_id}/bundle     -> assemble the data artifacts into
                                          <session_id>_bundle.zip ON THE SSD, so a crashed or
                                          closed dashboard cannot cost the deliverable (video
                                          excluded — it never reaches the backend)

The modal's "whole" verdict is strict: every sample source must be either absent or
already folded into the consolidated output, plus the integrity report must PASS. Once
per-role consolidated files exist they take primacy for that verdict (per-role coverage
is checked first, session-wide mtime is the fallback).
"""
import asyncio
import json
import logging
import os
import sqlite3
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .audit_logger import audit
from .csv_schema import parse_row, strip_tier_token
from .upload import (
    _session_dir as _recovery_dir,
    _slug,
    merge_csv_sources,
    merge_csv_sources_per_role,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["export"])

SSD_PATH = Path(os.getenv("SSD_PATH", "./data"))
RESCUE_PATH = Path(os.getenv("RESCUE_PATH", "./data_rescue"))

_ORIGINAL_KINDS = ("main", "csv", "late", "rescue")
_SCAN_KINDS = ("main", "csv", "late", "rescue", "merged", "consolidated")
_LABEL_COL_ID = 7
_LABEL_COL_NAME = 8
_DEV_COL = 10
_SEQ_COL = 9
_consolidate_locks: dict[str, asyncio.Lock] = {}
_bundle_locks: dict[str, asyncio.Lock] = {}
# Per-session locks accumulate one entry per session for the lifetime of the process.
# A rig that runs for weeks would otherwise grow these without bound.
_MAX_TRACKED_LOCKS = 256


def _lock_for(locks: dict, key) -> asyncio.Lock:
    """Get (or create) a per-key lock, evicting the oldest idle one past the cap."""
    lock = locks.get(key)
    if lock is None:
        if len(locks) >= _MAX_TRACKED_LOCKS:
            for stale_key, stale_lock in list(locks.items()):
                if not stale_lock.locked():
                    locks.pop(stale_key, None)
                    break
        lock = locks.setdefault(key, asyncio.Lock())
    return lock
_manifest_scan_cache: dict[str, tuple[tuple[tuple[str, int, int], ...], tuple[int, list[dict]]]] = {}


def _session_folders(session_id: str) -> list[Path]:
    """Every Data_Riset_IMU/<subject>_<tag> folder (SSD + rescue root) containing files
    for this session."""
    folders: list[Path] = []
    seen: set[str] = set()
    for root in (SSD_PATH, RESCUE_PATH):
        base = root / "Data_Riset_IMU"
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            if not any(p.name.startswith(f"{session_id}_") for p in d.iterdir()):
                continue
            resolved = str(d.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            folders.append(d)
    return folders


def _classify(name: str) -> str:
    if name.endswith("_integrity_report.json"):
        return "integrity"
    if name.endswith("_connectivity.json"):
        return "connectivity"
    if name.endswith("_late_delivery.json"):
        return "late_summary"
    if name.endswith("_consolidation.json"):
        return "consolidation"
    if name.endswith("_consolidated_validation.json"):
        return "consolidated_validation"
    if name.endswith("_timing.json"):
        return "timing"
    if name.endswith("_consolidated.csv"):
        return "consolidated"
    if name.endswith("_merged.csv"):
        return "merged"
    if name.endswith("_late.csv"):
        return "late"
    if name.endswith("_rescue.csv"):
        return "rescue"
    if name.endswith("_sensor_data.csv"):
        return "main"
    if name.endswith("_cameras.json"):
        return "cameras"
    if name.endswith("_video_sync.webm") or name.endswith("_video_sync.mp4"):
        return "video"
    if name.endswith(".csv"):
        return "csv"
    return "other"


def _role_from_name(name: str, session_id: str) -> str:
    """Recover the role from an on-disk source filename.

    Sources are written role-keyed (<sid>_<role>_sensor_data[_(late|rescue)]?.csv); the
    role is whatever is left after stripping the session prefix and known suffix.
    """
    if not name.startswith(f"{session_id}_"):
        return ""
    stem = name[len(session_id) + 1:]
    for suffix in (
        "_sensor_data_late.csv",
        "_sensor_data_rescue.csv",
        "_sensor_data.csv",
        "_late.csv",
        "_rescue.csv",
        ".csv",
    ):
        if stem.endswith(suffix):
            # strip_tier_token: names carry an attained-rate token (<role>_75hz_sensor_data.csv)
            # since 2026-08-07. Without removing it the same physical device buckets separately
            # each time its measured rate crosses a tier boundary.
            return strip_tier_token(stem[: -len(suffix)])
    return strip_tier_token(stem[:-4] if stem.endswith(".csv") else stem)


def _recovery_role(info: dict) -> str:
    """Role for a phone rescue upload. Falls back to slugged device_id so a recovery
    file without a role (backend default "") still lands in a dedicated bucket."""
    role = (info.get("role") or "").strip()
    return role or (_slug(info.get("device_id", "")) or "unknown")


def _per_role_consolidated(files: list[dict], session_id: str) -> dict[str, list[Path]]:
    """Map role_key -> consolidated file paths for that role (per-role primacy)."""
    out: dict[str, list[Path]] = {}
    for f in files:
        if f["kind"] != "consolidated":
            continue
        name = f["name"]
        if not name.startswith(f"{session_id}_"):
            continue
        stem = name[len(session_id) + 1:]
        if not stem.endswith("_consolidated.csv") or stem == "_consolidated.csv":
            continue
        out.setdefault(_slug(stem[: -len("_consolidated.csv")]), []).append(Path(f["path"]))
    return out


def _session_files(session_id: str) -> list[dict]:
    files: list[dict] = []
    for folder in _session_folders(session_id):
        for p in sorted(folder.iterdir()):
            if (
                not p.is_file()
                or not p.name.startswith(f"{session_id}_")
                or p.name.endswith((".tmp", ".sort.tmp", ".sort.sqlite", ".merge.sqlite"))
            ):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            files.append({
                "name": p.name,
                "path": str(p),
                "size": size,
                "kind": _classify(p.name),
                "folder": str(folder),
            })
    # Camera anchors may arrive before the first CSV creates a session folder. Include the
    # pending copy in manifests/bundles until cameras.py migrates it into the real folder.
    pending_camera = SSD_PATH / "Data_Riset_IMU" / "_pending_cameras" / f"{session_id}_cameras.json"
    if pending_camera.is_file() and not any(f["path"] == str(pending_camera) for f in files):
        try:
            files.append({
                "name": pending_camera.name,
                "path": str(pending_camera),
                "size": pending_camera.stat().st_size,
                "kind": "cameras",
                "folder": str(pending_camera.parent),
            })
        except OSError:
            pass
    files.sort(key=lambda f: f["name"])
    return files


def _recovery_manifest(session_id: str) -> list[dict]:
    out: list[dict] = []
    d = _recovery_dir(session_id)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.info.json")):
        try:
            info = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        info = dict(info)
        dev = info.get("device_id", "")
        csv = d / f"{_slug(dev)}.csv"
        info["csv_exists"] = csv.exists()
        info["csv_size"] = csv.stat().st_size if csv.exists() else 0
        info["csv_path"] = str(csv)
        # A legacy sidecar may claim complete without ever having passed a hash check. Never
        # let that claim make the file eligible for export/consolidation.
        info["verified"] = bool(
            info.get("complete")
            and info.get("sha256_verified")
            and info.get("total_bytes") == info["csv_size"]
        )
        if not info["verified"]:
            info["complete"] = False
        out.append(info)
    return out


def _scan_rows(csv_paths: list[Path]) -> tuple[int, list[dict]]:
    """Scan CSVs, deduping rows on (device_id, sequence_number) like the merge helper.

    Returns (distinct_row_count, labels_used). Because a session may have the originals
    AND a merged/consolidated superset on disk, plain line counts would double count; the
    seen-set makes the numbers exact regardless of which artifacts are present.
    """
    db_path = SSD_PATH / ".sessions" / f".manifest-scan-{os.getpid()}-{time.time_ns()}.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        # This is a disposable manifest index, not a durable database. Batch inserts and
        # relax the temporary journal so a 20-minute three-device session does not turn
        # every manifest poll into a minute-long end-session freeze.
        conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute(
            "CREATE TABLE rows (device_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
            "label_id INTEGER, label_name TEXT, PRIMARY KEY(device_id, sequence))"
        )
        batch: list[tuple[str, int, int, str]] = []

        def flush_batch() -> None:
            if not batch:
                return
            conn.executemany(
                "INSERT OR IGNORE INTO rows(device_id, sequence, label_id, label_name) "
                "VALUES (?, ?, ?, ?)",
                batch,
            )
            batch.clear()

        for p in csv_paths:
            if not p.exists():
                continue
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        parts = parse_row(line)
                        if parts is None:
                            continue
                        try:
                            sequence = int(parts[_SEQ_COL])
                        except (ValueError, IndexError):
                            continue
                        try:
                            label_id = int(parts[_LABEL_COL_ID])
                        except (ValueError, IndexError):
                            label_id = 0
                        batch.append(
                            (parts[_DEV_COL], sequence, label_id, parts[_LABEL_COL_NAME].strip())
                        )
                        if len(batch) >= 5000:
                            flush_batch()
            except OSError:
                continue
        flush_batch()
        conn.commit()
        row_count = int(conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0])
        labels = [
            {"label_id": int(lid), "label_name": lname or str(lid), "row_count": int(count)}
            for lid, lname, count in conn.execute(
                "SELECT label_id, label_name, COUNT(*) FROM rows "
                "GROUP BY label_id, label_name ORDER BY label_id"
            )
        ]
        return row_count, labels
    finally:
        conn.close()
        try:
            db_path.unlink()
        except OSError:
            pass


def _mtime(path: Path | None) -> float:
    try:
        return path.stat().st_mtime if path else 0.0
    except OSError:
        return 0.0


def _session_meta(session_id: str) -> dict:
    meta: dict = {"subject": "", "session_tag": "", "operator": ""}
    state_file = SSD_PATH / ".sessions" / f"{session_id}.state.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            meta["subject"] = data.get("subject_name", "")
            meta["session_tag"] = data.get("session_tag", "")
            meta["operator"] = data.get("operator", "")
            return meta
        except Exception:
            pass
    folders = _session_folders(session_id)
    if folders:
        parts = folders[0].name.split("_", 1)
        meta["subject"] = parts[0] if parts else ""
        meta["session_tag"] = parts[1] if len(parts) > 1 else ""
    return meta


def _ledger_record(session_id: str) -> dict:
    path = SSD_PATH / ".sessions" / f"{session_id}.state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


class _LedgerIoMetrics:
    """Expose the original write-loss counters while revalidating an old session.

    IntegrityValidator normally reads the live IoManager. A later operator consolidation can
    happen while another session is recording, so reading that mutable singleton would attach
    the new session's counters to the old report. The terminal ledger already contains the
    original per-device evidence; use that immutable snapshot instead.
    """

    def __init__(self, report: dict) -> None:
        self._devices = {
            str(d.get("device_id")): d
            for d in report.get("devices", [])
            if isinstance(d, dict)
        }

    def _value(self, device_id: str, key: str) -> int:
        try:
            return int(self._devices.get(device_id, {}).get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    def dropped_no_writer(self, device_id: str) -> int:
        return self._value(device_id, "packets_dropped_no_writer")

    def write_failures(self, device_id: str) -> int:
        return self._value(device_id, "csv_write_failures")

    def rows_lost_after_failover(self, device_id: str) -> int:
        return self._value(device_id, "rows_lost_after_failover")


async def _revalidate_consolidated(session_id: str, per_role: dict[str, dict]) -> dict | None:
    """Re-run quality checks over the exact merged files that will be handed to analysis."""
    ledger = _ledger_record(session_id)
    raw_devices = ledger.get("devices", [])
    if not isinstance(raw_devices, list) or not raw_devices:
        return None

    from .integrity_validator import IntegrityValidator
    from .io_manager import _sha256
    from .session_manager import DeviceInfo, DeviceSubstate

    devices: list[DeviceInfo] = []
    file_results: dict[str, dict] = {}
    for raw in raw_devices:
        if not isinstance(raw, dict):
            continue
        device_id = str(raw.get("device_id", ""))
        role = str(raw.get("role", "unknown"))
        first = raw.get("first_packet_ts")
        try:
            first = int(first) if first is not None else None
        except (TypeError, ValueError):
            first = None
        devices.append(DeviceInfo(
            device_id=device_id,
            device_role=role,
            device_model=str(raw.get("model", "")),
            app_version=str(raw.get("app_version", "")),
            is_online=False,
            packets_received=int(raw.get("packets", 0) or 0),
            first_packet_ts=first,
            offline_intervals=list(raw.get("offline_intervals", [])),
            substate=DeviceSubstate.FINALIZED,
        ))
        result = per_role.get(_slug(role))
        if not result:
            continue
        path = Path(result["path"])
        if not path.exists():
            continue
        file_results[device_id] = {
            "path": str(path),
            "rows": int(result.get("rows", 0) or 0),
            "sha256": _sha256(path),
            "reordered": 0,
        }

    prior = ledger.get("integrity_report") or {}
    report = await IntegrityValidator().run(
        session_id=session_id,
        file_results=file_results,
        devices=devices,
        scheduled_start_ms=int(ledger.get("scheduled_start_ms", 0) or 0),
        label_timeline=list(ledger.get("label_timeline", [])),
        session_start_ms=int(ledger.get("recording_started_ms", 0) or 0),
        session_end_ms=int(
            ledger.get("finalized_at_ms") or ledger.get("updated_at_ms") or time.time() * 1000
        ),
        io_source=_LedgerIoMetrics(prior),
        validation_scope="consolidated_sources",
    )
    ledger["integrity_report"] = report
    ledger["file_results"] = file_results
    ledger["revalidated_after_consolidation_ms"] = int(time.time() * 1000)
    # _ledger_record returns a detached JSON object; write it through the same atomic ledger
    # contract used by SessionManager so the next dashboard sees the same verdict.
    from .session_ledger import SessionLedger
    try:
        SessionLedger(SSD_PATH / ".sessions").write(session_id, ledger)
    except OSError as exc:
        # The consolidated files and the report sidecar are already durable. Keep the
        # export usable even if a full SSD prevents the lifecycle index from being updated.
        await audit.log("ERROR", "consolidated_ledger_write_failed", {
            "session_id": session_id, "error": str(exc),
        })
    return report


@router.get("/export/{session_id}/manifest")
async def export_manifest(session_id: str):
    """Authoritative snapshot of every artifact a session produced on this backend."""
    if not session_id or any(c in session_id for c in "/\\"):
        raise HTTPException(status_code=400, detail="invalid session_id")

    files = _session_files(session_id)
    folders = _session_folders(session_id)

    integrity = None
    connectivity = None
    late_summary = None
    validation = None
    for f in files:
        p = Path(f["path"])
        try:
            if f["kind"] == "integrity":
                integrity = json.loads(p.read_text(encoding="utf-8"))
            elif f["kind"] == "connectivity":
                connectivity = json.loads(p.read_text(encoding="utf-8"))
            elif f["kind"] == "late_summary":
                late_summary = json.loads(p.read_text(encoding="utf-8"))
            elif f["kind"] == "consolidated_validation":
                validation = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue

    recovery = _recovery_manifest(session_id)
    ledger = _ledger_record(session_id)
    late_sources = [f for f in files if f["kind"] in ("late", "late_summary")]
    consolidated_files = [Path(f["path"]) for f in files if f["kind"] == "consolidated"]
    session_mtime = max((_mtime(p) for p in consolidated_files), default=0.0)

    # Per-role primacy: once per-role consolidated files exist, a source is only *pending*
    # when its own role's consolidated file is missing or older than the source. Without
    # per-role files we fall back to the old session-wide mtime comparison.
    per_role = _per_role_consolidated(files, session_id)
    has_per_role = bool(per_role)

    def _role_covered(role_key: str, src: Path) -> bool:
        return any(_mtime(p) >= _mtime(src) for p in per_role.get(role_key, []))

    late_has_rows = bool(late_summary and late_summary.get("devices")) or any(
        f["kind"] == "late" and int(f.get("size", 0) or 0) > 0 for f in files
    )
    if late_has_rows and has_per_role:
        late_pending = any(
            f["kind"] == "late"
            and not _role_covered(_slug(_role_from_name(f["name"], session_id)), Path(f["path"]))
            for f in files
        )
    else:
        late_pending = bool(late_sources) and late_has_rows and (
            not consolidated_files
            or max(_mtime(Path(f["path"])) for f in late_sources) > session_mtime
        )

    recovery_sources = [
        (r, Path(r["csv_path"])) for r in recovery
        if r.get("complete") and r.get("sha256_verified") and r.get("csv_exists")
    ]
    if recovery_sources and has_per_role:
        recovery_pending = any(
            not _role_covered(_slug(_recovery_role(r)), csv) for r, csv in recovery_sources
        )
    else:
        recovery_pending = bool(recovery_sources) and (
            not consolidated_files
            or max(_mtime(csv) for _, csv in recovery_sources) > session_mtime
        )

    # `recovery_pending` answers "have VERIFIED uploads been folded into the consolidation".
    # It structurally cannot see a transfer that is still running or one that failed its
    # digest, so a phone mid-upload was invisible and the export modal's wait loop declared
    # "nothing more expected" and consolidated without it. These two say what is still owed.
    transfers_in_progress = [
        {
            "device_id": r.get("device_id", ""),
            "role": _recovery_role(r),
            "state": r.get("state", "receiving"),
            "received_bytes": int(r.get("received_bytes", 0) or 0),
            "total_bytes": int(r.get("total_bytes", 0) or 0),
            "updated_at_ms": int(_mtime(Path(r["csv_path"])) * 1000) if r.get("csv_exists") else 0,
            "corrupt_attempts": int(r.get("corrupt_attempts", 0) or 0),
        }
        for r in recovery
        if not r.get("verified")
    ]
    uploads_in_progress = bool(transfers_in_progress)

    # Fall back to the ledger's copy: a dashboard that reconnects after the backend
    # restarted has no in-memory integrity report, and reporting UNKNOWN for a session
    # that already passed would read as a regression.
    at_stop_status = (integrity or ledger.get("integrity_report") or {}).get("status", "") or (
        "NONE" if not folders else "UNKNOWN"
    )
    final_status = (validation or {}).get("status", "")
    status_rank = {"NONE": 0, "UNKNOWN": 0, "PASS": 1, "PARTIAL": 2, "FAIL": 3}
    status = at_stop_status
    if final_status and status_rank.get(final_status, 0) > status_rank.get(status, 0):
        status = final_status

    reasons: list[str] = []
    if status != "PASS":
        if final_status and final_status != "PASS":
            reasons.append(f"final consolidated validation is '{final_status}'")
        if at_stop_status != "NONE":
            reasons.append(f"integrity report is '{at_stop_status}'")
        else:
            reasons.append("no integrity report on disk for this session")
    if late_pending:
        reasons.append("late telemetry rows have not been consolidated yet")
    if recovery_pending:
        reasons.append("phone rescue CSVs have not been consolidated yet")
    whole = status == "PASS" and not late_pending and not recovery_pending
    consolidated = bool(consolidated_files) and not late_pending and not recovery_pending
    analysis_ready_imu = bool(
        (integrity or ledger.get("integrity_report") or {}).get("analysis_ready", False)
    )

    data_paths = [Path(f["path"]) for f in files if f["kind"] in _SCAN_KINDS]
    recovery_paths = [Path(r["csv_path"]) for r in recovery if r.get("csv_exists")]
    scan_paths = data_paths + recovery_paths
    fingerprint: list[tuple[str, int, int]] = []
    for path in scan_paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        fingerprint.append((str(path), stat.st_size, stat.st_mtime_ns))
    scan_key = tuple(fingerprint)
    cached = _manifest_scan_cache.get(session_id)
    if cached is not None and cached[0] == scan_key:
        data_rows, labels = cached[1]
    else:
        data_rows, labels = await asyncio.get_event_loop().run_in_executor(
            None, _scan_rows, scan_paths
        )
        _manifest_scan_cache[session_id] = (scan_key, (data_rows, labels))
        # Keep this process-local cache bounded when operators run many sessions.
        if len(_manifest_scan_cache) > 64:
            _manifest_scan_cache.pop(next(iter(_manifest_scan_cache)))

    meta = _session_meta(session_id)
    return {
        "session_id": session_id,
        "found": bool(folders or recovery),
        "subject": meta["subject"],
        "session_tag": meta["session_tag"],
        "operator": meta["operator"],
        "status": status,
        "whole": whole,
        "exportable": bool(folders or recovery),
        "consolidated": consolidated,
        "analysis_ready_imu": analysis_ready_imu,
        "terminal": bool(ledger.get("terminal", False)),
        "lifecycle_state": ledger.get("state", ""),
        "reasons": reasons,
        "late_pending": late_pending,
        "recovery_pending": recovery_pending,
        # Phone transfers that have started but not verified. Separate from
        # recovery_pending on purpose: that flag is about consolidation currency, this one
        # is about data that has not arrived yet.
        "uploads_in_progress": uploads_in_progress,
        "transfers_in_progress": transfers_in_progress,
        "per_roles": sorted(per_role.keys()),
        "labels_used": labels,
        "data_rows": data_rows,
        "integrity": integrity,
        "connectivity": connectivity,
        "late_summary": late_summary,
        "validation": validation,
        "files": files,
        "recovery": recovery,
        "ledger": ledger,
    }


@router.get("/export/{session_id}/file")
async def export_file(session_id: str, name: str = Query(...)):
    """Stream one session artifact. `name` is a basename owned by the session."""
    # Some Chromium download paths can be reported with Windows separators even though
    # the API contract is a basename. Normalize those separators before applying the
    # basename/session guard; never relax the session prefix or folder containment checks.
    normalized = name.replace("\\", "/")
    safe = Path(normalized).name
    if not safe.startswith(f"{session_id}_"):
        raise HTTPException(status_code=400, detail="invalid filename")
    for folder in _session_folders(session_id):
        fp = (folder / safe).resolve()
        if (fp.parent == folder.resolve()) and fp.is_file():
            return FileResponse(fp, filename=safe)
    raise HTTPException(status_code=404, detail="file not found")


@router.post("/export/{session_id}/consolidate")
async def export_consolidate(session_id: str):
    """Merge every sample source for the session into <session_id>_consolidated.csv.

    Sources: main writes, rescue-path fallbacks, late-delivery sidecars and complete
    phone rescue uploads. Rows are deduped on (device_id, sequence_number) and
    re-sorted by timestamp (shared merge_csv_sources helper), so the downstream
    segmentation sees one clean, monotonic series.
    """
    if not session_id or any(c in session_id for c in "/\\"):
        raise HTTPException(status_code=400, detail="invalid session_id")

    folders = _session_folders(session_id)
    recovery = _recovery_manifest(session_id)
    verified_recovery = [
        r for r in recovery
        if r.get("complete") and r.get("sha256_verified") and r.get("csv_exists")
    ]
    if not folders and not verified_recovery:
        raise HTTPException(status_code=404, detail="session data not found")

    sources: list[tuple[str, str, Path]] = []
    for f in _session_files(session_id):
        if f["kind"] in _ORIGINAL_KINDS:
            role_key = _slug(_role_from_name(f["name"], session_id)) or "unknown"
            sources.append((role_key, f["kind"], Path(f["path"])))
    for r in verified_recovery:
        sources.append((_slug(_recovery_role(r)), "recovery", Path(r["csv_path"])))

    if not sources:
        raise HTTPException(status_code=404, detail="no data files to consolidate")

    if folders:
        primary_folder = folders[0]
    else:
        first = verified_recovery[0]
        subject = str(first.get("subject") or "Unknown").replace(" ", "_")
        tag = str(first.get("session_tag") or "Session").replace(" ", "_")
        primary_folder = SSD_PATH / "Data_Riset_IMU" / f"{subject}_{tag}"

    def consolidate_sync() -> tuple[dict, dict]:
        primary_folder.mkdir(parents=True, exist_ok=True)
        out_path = primary_folder / f"{session_id}_consolidated.csv"
        result = merge_csv_sources(
            [(label, path) for _, label, path in sources],
            out_path,
            metadata_prefix=f"session_id={session_id},source=consolidate",
        )
        per_role = merge_csv_sources_per_role(
            sources,
            primary_folder,
            session_id,
            metadata_prefix=f"session_id={session_id},source=consolidate",
        )
        return result, per_role

    lock = _lock_for(_consolidate_locks, session_id)
    async with lock:
        try:
            result, per_role = await asyncio.get_event_loop().run_in_executor(
                None, consolidate_sync
            )
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"consolidation failed: {exc}") from exc

        # Both passes stay inside the lock: they read the files just written, and a
        # concurrent consolidation would otherwise rewrite them mid-validation.
        #
        # The stop-time integrity report cannot include phone rescue/late rows that arrive
        # afterward. Re-check the final per-role files so the export modal never reports a
        # complete dataset when sequence gaps remain after consolidation.
        from .integrity_validator import validate_consolidated
        validation_inputs = [
            (role, Path(stats["path"]))
            for role, stats in per_role["per_role"].items()
        ]
        validation = await asyncio.get_event_loop().run_in_executor(
            None, validate_consolidated, session_id, validation_inputs
        )
        validation_path = primary_folder / f"{session_id}_consolidated_validation.json"
        # Sidecars are guarded separately from the merge. The merged CSVs are already
        # durable by this point; a failure to write a report about them must not be
        # reported to the operator as "consolidation failed", which invited them to redo
        # a merge that had in fact succeeded.
        sidecar_errors: list[str] = []
        try:
            validation_path.write_text(json.dumps(validation, indent=2), encoding="utf-8")
        except OSError as exc:
            sidecar_errors.append(f"validation sidecar: {exc}")
            await audit.log("ERROR", "consolidated_validation_write_failed", {
                "session_id": session_id, "error": str(exc),
            })

        # Complementary to the above, not a duplicate: this reconstructs each device from
        # the ledger and re-runs the FULL validator over the merged files, so the export
        # carries rate/disconnect evidence too — not just the sequence-gap verdict.
        try:
            revalidated = await _revalidate_consolidated(session_id, per_role["per_role"])
        except Exception as exc:
            revalidated = None
            sidecar_errors.append(f"revalidation: {exc}")
            await audit.log("ERROR", "consolidated_revalidation_failed", {
                "session_id": session_id, "error": str(exc),
            })

        summary_path = primary_folder / f"{session_id}_consolidation.json"
        summary = {
            "session_id": session_id,
            "consolidated_at_ms": int(time.time() * 1000),
            "per_role": per_role["per_role"],
            "per_role_files": per_role["files"],
            **result,
            "validation": validation,
        }
        try:
            summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        except OSError as exc:
            sidecar_errors.append(f"consolidation summary: {exc}")
            await audit.log("ERROR", "consolidation_summary_write_failed", {
                "session_id": session_id, "error": str(exc),
            })

    await audit.log("INFO", "session_consolidated", {
        "session_id": session_id,
        "rows": result["rows"],
        "path": result["path"],
        "sources": result["sources"],
        "per_role_files": per_role["files"],
    })
    return {
        "session_id": session_id,
        **result,
        "per_role": per_role["per_role"],
        "integrity_report": revalidated,
        "validation": validation,
        # Non-empty means the data merged correctly but a report about it could not be
        # written. The caller shows this as a warning, not as a failed consolidation.
        "sidecar_errors": sidecar_errors,
    }


BUNDLE_SUFFIX = "_bundle.zip"


def _build_bundle(session_id: str, out: Path, files: list[dict], recovery: list[dict]) -> dict:
    """Write the session's data artifacts into `out` as a zip. Blocking; run in an executor.

    Written to a temporary path and moved into place with os.replace, so an interrupted build
    can never leave behind a truncated archive that looks finished. That distinction matters
    here more than usual: this file exists precisely so the operator has something trustworthy
    when the dashboard does not.
    """
    tmp = out.with_name(out.name + ".tmp")
    written: list[str] = []
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for f in files:
            # Never nest a previous bundle inside the new one.
            if f["name"].endswith(BUNDLE_SUFFIX):
                continue
            z.write(f["path"], arcname=f"data/{f['name']}")
            written.append(f"data/{f['name']}")
        for r in recovery:
            if not r.get("csv_exists"):
                continue
            arc = f"data/recovery/{Path(r['csv_path']).name}"
            z.write(r["csv_path"], arcname=arc)
            written.append(arc)
        manifest = {
            "session_id": session_id,
            "built_at_ms": int(time.time() * 1000),
            "entries": written,
            "lifecycle": _ledger_record(session_id),
            "contains_video": False,
            "note": (
                "Data artifacts only. Camera footage is recorded by the browser via "
                "MediaRecorder and never reaches the backend, so it cannot appear here. It is "
                "independently durable in the browser's IndexedDB until the NEXT session starts "
                "— retrieve it from the dashboard's 'Recover buffered video' screen."
            ),
        }
        z.writestr("bundle_manifest.json", json.dumps(manifest, indent=2))
    os.replace(tmp, out)
    return {"path": str(out), "entries": written, "size": out.stat().st_size}


async def _build_bundle_locked(
    session_id: str, out: Path, files: list[dict], recovery: list[dict]
) -> dict:
    """Serialize bundle rebuilds for one session so two download paths cannot share a .tmp."""
    lock = _lock_for(_bundle_locks, session_id)
    async with lock:
        return await asyncio.get_event_loop().run_in_executor(
            None, _build_bundle, session_id, out, files, recovery
        )


class BundleUnavailable(Exception):
    """No artifacts exist for this session, so there is nothing to archive."""


async def build_session_bundle(session_id: str) -> dict:
    """Write the session's data artifacts to <session_id>_bundle.zip on the SSD.

    Shared by the HTTP endpoints and by the finalize job, so the archive an operator
    downloads on demand and the one written automatically at STOP are produced by exactly
    one code path. Raises BundleUnavailable when the session has no artifacts at all.
    """
    if not session_id or any(c in session_id for c in "/\\"):
        raise ValueError("invalid session_id")
    folders = _session_folders(session_id)
    files = _session_files(session_id)
    recovery = _recovery_manifest(session_id)
    if (not folders and not recovery) or (
        not files and not any(r.get("csv_exists") for r in recovery)
    ):
        raise BundleUnavailable(f"no artifacts found for session {session_id}")

    output_folder = folders[0] if folders else _recovery_dir(session_id)
    out = output_folder / f"{session_id}{BUNDLE_SUFFIX}"
    result = await _build_bundle_locked(session_id, out, files, recovery)
    await audit.log("INFO", "session_bundled", {
        "session_id": session_id,
        "path": result["path"],
        "entries": len(result["entries"]),
        "size": result["size"],
    })
    return result


@router.post("/export/{session_id}/bundle")
async def export_bundle(session_id: str):
    """Assemble the session's data artifacts into a zip on the SSD, server-side.

    Until now the end-of-session zip was built entirely in the browser with jszip, which made a
    render bug in the dashboard cost the deliverable even though every byte was already safely
    on disk — exactly what happened on 2026-08-11. This endpoint takes the browser off that
    path: the operator can obtain a complete data bundle with the dashboard closed, crashed, or
    on a different machine.
    """
    try:
        result = await build_session_bundle(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BundleUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        await audit.log("ERROR", "bundle_failed", {"session_id": session_id, "error": str(exc)})
        raise HTTPException(status_code=500, detail=f"could not write bundle: {exc}") from exc
    return {"session_id": session_id, "contains_video": False, **result}


@router.get("/export/{session_id}/bundle/file")
async def export_bundle_file(session_id: str):
    """Download the server-built data bundle, regardless of integrity verdict.

    A PARTIAL or FAIL verdict is a warning about capture quality, never a reason to withhold
    the CSVs that *were* captured. The operator explicitly builds the current bundle first;
    this endpoint then streams that durable SSD artifact through the browser download manager.
    """
    if not session_id or any(c in session_id for c in "/\\"):
        raise HTTPException(status_code=400, detail="invalid session_id")
    folders = _session_folders(session_id)
    # Always rebuild rather than returning an older archive. Late telemetry and verified
    # phone rescue CSVs can arrive after the first bundle was requested; a stale ZIP is a
    # silent data-loss mode for the crash-boundary download link.
    # Make the direct recovery link self-sufficient. This is used by the error boundary
    # when the dashboard itself failed to render before it could POST /bundle.
    files = _session_files(session_id)
    recovery = _recovery_manifest(session_id)
    if (not folders and not recovery) or (not files and not any(r.get("csv_exists") for r in recovery)):
        raise HTTPException(status_code=404, detail="no artifacts found for session")
    output_folder = folders[0] if folders else _recovery_dir(session_id)
    bundle = output_folder / f"{session_id}{BUNDLE_SUFFIX}"
    try:
        await _build_bundle_locked(session_id, bundle, files, recovery)
    except Exception as exc:
        await audit.log("ERROR", "bundle_failed", {"session_id": session_id, "error": str(exc)})
        raise HTTPException(status_code=500, detail=f"could not write bundle: {exc}") from exc
    return FileResponse(bundle, filename=bundle.name, media_type="application/zip")
