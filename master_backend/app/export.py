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

The modal's "whole" verdict is strict: every sample source must be either absent or
already folded into the consolidated output, plus the integrity report must PASS. Once
per-role consolidated files exist they take primacy for that verdict (per-role coverage
is checked first, session-wide mtime is the fallback).
"""
import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .audit_logger import audit
from .csv_schema import parse_row
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
            return stem[: -len(suffix)]
    return stem[:-4] if stem.endswith(".csv") else stem


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
    for p in csv_paths:
        if not p.exists():
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    # Version-agnostic: an exact-match test against one header constant
                    # let an older file's header through as a data row once the schema
                    # gained columns, inflating row_count by one per v1 file.
                    parts = parse_row(line)
                    if parts is None:
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
        f["kind"] == "late" for f in files
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
        (r, Path(r["csv_path"])) for r in recovery if r.get("complete") and r.get("csv_exists")
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
        "per_roles": sorted(per_role.keys()),
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

    sources: list[tuple[str, str, Path]] = []
    for f in _session_files(session_id):
        if f["kind"] in _ORIGINAL_KINDS:
            role_key = _slug(_role_from_name(f["name"], session_id)) or "unknown"
            sources.append((role_key, f["kind"], Path(f["path"])))
    for r in _recovery_manifest(session_id):
        if r.get("complete") and r.get("csv_exists"):
            sources.append((_slug(_recovery_role(r)), "recovery", Path(r["csv_path"])))

    if not sources:
        raise HTTPException(status_code=404, detail="no data files to consolidate")

    primary_folder = folders[0]
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

    summary_path = primary_folder / f"{session_id}_consolidation.json"
    summary = {
        "session_id": session_id,
        "consolidated_at_ms": int(__import__("time").time() * 1000),
        "per_role": per_role["per_role"],
        "per_role_files": per_role["files"],
        **result,
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    await audit.log("INFO", "session_consolidated", {
        "session_id": session_id,
        "rows": result["rows"],
        "path": result["path"],
        "sources": result["sources"],
        "per_role_files": per_role["files"],
    })
    return {"session_id": session_id, **result, "per_role": per_role["per_role"]}
