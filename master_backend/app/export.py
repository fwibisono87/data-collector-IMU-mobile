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
                                          <session_id>_consolidated.csv

The modal's "whole" verdict is strict: every sample source must be either absent or
already folded into the consolidated output, plus the integrity report must PASS.
"""
import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .audit_logger import audit
from .upload import _CSV_HEADER, _session_dir as _recovery_dir, _slug, merge_csv_sources

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
    if name.endswith(".csv"):
        return "csv"
    return "other"


def _session_files(session_id: str) -> list[dict]:
    files: list[dict] = []
    for folder in _session_folders(session_id):
        for p in sorted(folder.iterdir()):
            if not p.is_file() or not p.name.startswith(f"{session_id}_"):
                continue
            files.append({
                "name": p.name,
                "path": str(p),
                "size": p.stat().st_size,
                "kind": _classify(p.name),
                "folder": str(folder),
            })
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
        out.append(info)
    return out


def _scan_rows(csv_paths: list[Path]) -> tuple[int, list[dict]]:
    """Scan CSVs, deduping rows on (device_id, sequence_number) like the merge helper.

    Returns (distinct_row_count, labels_used). Because a session may have the originals
    AND a merged/consolidated superset on disk, plain line counts would double count; the
    seen-set makes the numbers exact regardless of which artifacts are present.
    """
    seen: set[str] = set()
    counts: dict[tuple[int, str], int] = {}
    row_count = 0
    header_no_nl = _CSV_HEADER.rstrip("\n")
    for p in csv_paths:
        if not p.exists():
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("#") or line == "":
                        continue
                    if line.rstrip("\n") == header_no_nl:
                        continue
                    parts = line.split(",")
                    if len(parts) <= _DEV_COL:
                        continue
                    key = f"{parts[_DEV_COL]}\t{parts[_SEQ_COL]}"
                    if key in seen:
                        continue
                    seen.add(key)
                    row_count += 1
                    try:
                        lid = int(parts[_LABEL_COL_ID])
                    except ValueError:
                        continue
                    lname = parts[_LABEL_COL_NAME].strip()
                    counts[(lid, lname)] = counts.get((lid, lname), 0) + 1
        except OSError:
            continue
    labels = sorted(
        ({"label_id": lid, "label_name": lname, "row_count": cnt}
         for (lid, lname), cnt in counts.items()),
        key=lambda x: x["label_id"],
    )
    return row_count, labels


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
    for f in files:
        p = Path(f["path"])
        try:
            if f["kind"] == "integrity":
                integrity = json.loads(p.read_text(encoding="utf-8"))
            elif f["kind"] == "connectivity":
                connectivity = json.loads(p.read_text(encoding="utf-8"))
            elif f["kind"] == "late_summary":
                late_summary = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue

    recovery = _recovery_manifest(session_id)
    late_sources = [Path(f["path"]) for f in files if f["kind"] in ("late", "late_summary")]
    consolidated = next((Path(f["path"]) for f in files if f["kind"] == "consolidated"), None)
    consolidated_mtime = _mtime(consolidated)

    late_has_rows = bool(late_summary and late_summary.get("devices")) or any(
        f["kind"] == "late" for f in files
    )
    late_pending = bool(late_sources) and late_has_rows and (
        consolidated is None or max(_mtime(p) for p in late_sources) > consolidated_mtime
    )

    recovery_sources = [
        Path(r["csv_path"]) for r in recovery if r.get("complete") and r.get("csv_exists")
    ]
    recovery_pending = bool(recovery_sources) and (
        consolidated is None or max(_mtime(p) for p in recovery_sources) > consolidated_mtime
    )

    status = (integrity or {}).get("status", "") or ("NONE" if not folders else "UNKNOWN")

    reasons: list[str] = []
    if status != "PASS":
        reasons.append(f"integrity report is '{status}'" if status != "NONE"
                       else "no integrity report on disk for this session")
    if late_pending:
        reasons.append("late telemetry rows have not been consolidated yet")
    if recovery_pending:
        reasons.append("phone rescue CSVs have not been consolidated yet")
    whole = status == "PASS" and not late_pending and not recovery_pending

    data_paths = [Path(f["path"]) for f in files if f["kind"] in _SCAN_KINDS]
    recovery_paths = [Path(r["csv_path"]) for r in recovery if r.get("csv_exists")]
    data_rows, labels = _scan_rows(data_paths + recovery_paths)

    meta = _session_meta(session_id)
    return {
        "session_id": session_id,
        "found": bool(folders),
        "subject": meta["subject"],
        "session_tag": meta["session_tag"],
        "operator": meta["operator"],
        "status": status,
        "whole": whole,
        "reasons": reasons,
        "late_pending": late_pending,
        "recovery_pending": recovery_pending,
        "labels_used": labels,
        "data_rows": data_rows,
        "integrity": integrity,
        "connectivity": connectivity,
        "late_summary": late_summary,
        "files": files,
        "recovery": recovery,
    }


@router.get("/export/{session_id}/file")
async def export_file(session_id: str, name: str = Query(...)):
    """Stream one session artifact. `name` is a basename owned by the session."""
    safe = Path(name).name
    if safe != name or not safe.startswith(f"{session_id}_"):
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
    if not folders:
        raise HTTPException(status_code=404, detail="session data not found")

    sources: list[tuple[str, Path]] = []
    for f in _session_files(session_id):
        if f["kind"] in _ORIGINAL_KINDS:
            sources.append((f["kind"], Path(f["path"])))
    for r in _recovery_manifest(session_id):
        if r.get("complete") and r.get("csv_exists"):
            sources.append(("recovery", Path(r["csv_path"])))

    if not sources:
        raise HTTPException(status_code=404, detail="no data files to consolidate")

    primary_folder = folders[0]
    out_path = primary_folder / f"{session_id}_consolidated.csv"
    result = merge_csv_sources(
        sources,
        out_path,
        metadata_prefix=f"session_id={session_id},source=consolidate",
    )

    summary_path = primary_folder / f"{session_id}_consolidation.json"
    summary = {
        "session_id": session_id,
        "consolidated_at_ms": __import__("time").time() * 1000,
        **result,
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    await audit.log("INFO", "session_consolidated", {
        "session_id": session_id, "rows": result["rows"],
        "path": result["path"], "sources": result["sources"],
    })
    return {"session_id": session_id, **result}
