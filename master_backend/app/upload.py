"""
Phone recovery CSV upload + desktop pull endpoints.

Phones keep a complete local rescue CSV per session (LocalSessionRecorder). When the
WebSocket telemetry path drops for long stretches (or a session ends while the phone is
dark), that CSV is the authoritative copy. This module lets a phone upload that CSV to
the backend resumably over plain HTTP (robust to flaky Wi-Fi), then lets the operator
list, download and merge the recovery CSVs from the desktop dashboard — no adb needed.

Upload protocol (chunked, resumable):
  POST /upload/csv
    query:  device_id, session_id, role, subject, session_tag, operator
    header: X-Offset (byte offset this chunk starts at), X-Total (file size),
            X-Sha256 (full-file sha256; send on the final chunk),
            X-Complete (1 on the final chunk)
    body:   the raw chunk bytes

  GET  /upload/status?device_id=&session_id=  -> resume point (received_bytes)

The server appends chunks in order and tracks received_bytes per (device, session).
"""
import hashlib
import json
import logging
import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from .audit_logger import audit
from .csv_schema import (
    COL_DEVICE_ID,
    COL_SEQUENCE,
    CSV_HEADER as _CSV_HEADER,
    is_header_line,
    parse_row,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["recovery"])

RECOVERY_PATH = Path(os.getenv("RECOVERY_PATH", "./data_recovery"))


def _session_dir(session_id: str) -> Path:
    d = RECOVERY_PATH / (_slug(session_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(s: str) -> str:
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in s)


def _info_path(session_id: str, device_id: str) -> Path:
    return _session_dir(session_id) / f"{_slug(device_id)}.info.json"


def _csv_path(session_id: str, device_id: str) -> Path:
    return _session_dir(session_id) / f"{_slug(device_id)}.csv"


def _default_info(session_id: str, device_id: str) -> dict:
    return {
        "session_id": session_id,
        "device_id": device_id,
        "role": "",
        "subject": "",
        "session_tag": "",
        "operator": "",
        "total_bytes": 0,
        "received_bytes": 0,
        "sha256": "",
        "complete": False,
        "done": False,
        "start_epoch_ms": int(time.time() * 1000),
        "updated_at_ms": int(time.time() * 1000),
    }


def _load_info(session_id: str, device_id: str) -> dict:
    p = _info_path(session_id, device_id)
    if not p.exists():
        return _default_info(session_id, device_id)
    try:
        base = _default_info(session_id, device_id)
        base.update(json.loads(p.read_text(encoding="utf-8")))
        return base
    except Exception:
        return _default_info(session_id, device_id)


def _save_info(info: dict) -> None:
    info["updated_at_ms"] = int(time.time() * 1000)
    _info_path(str(info["session_id"]), str(info["device_id"])).write_text(
        json.dumps(info, indent=2), encoding="utf-8"
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@router.get("/upload/status")
async def upload_status(
    device_id: str = Query(...),
    session_id: str = Query(...),
):
    info = _load_info(session_id, device_id)
    return info


@router.post("/upload/csv")
async def upload_csv(request: Request):
    device_id = (request.query_params.get("device_id") or "").strip()
    session_id = (request.query_params.get("session_id") or "").strip()
    role = (request.query_params.get("role") or "").strip()
    subject = (request.query_params.get("subject") or "").strip()
    session_tag = (request.query_params.get("session_tag") or "").strip()
    operator = (request.query_params.get("operator") or "").strip()
    if not device_id or not session_id:
        raise HTTPException(status_code=400, detail="device_id and session_id are required")

    offset = _int_header(request, "x-offset", 0)
    total = _int_header(request, "x-total", 0)
    sha = (request.headers.get("x-sha256") or "").strip()
    complete = request.headers.get("x-complete") == "1"

    info = _load_info(session_id, device_id)
    if info["received_bytes"] > offset:
        # Duplicate / out-of-order chunk (retry after a partial write). Client should resume
        # from received_bytes; return current state instead of corrupting the file.
        return JSONResponse(info)

    body = await request.body()
    csv = _csv_path(session_id, device_id)
    with open(csv, "ab") as f:
        f.write(body)

    info.update({
        "role": role or info["role"],
        "subject": subject or info["subject"],
        "session_tag": session_tag or info["session_tag"],
        "operator": operator or info["operator"],
        "total_bytes": total,
        "received_bytes": offset + len(body),
    })
    if complete:
        info["sha256"] = sha
        info["complete"] = True
        try:
            info["sha256_verified"] = _sha256(csv) == sha
        except Exception:
            info["sha256_verified"] = None
        _save_info(info)
        await audit.log("INFO", "recovery_upload_complete", {
            "session_id": session_id, "device_id": device_id, "bytes": info["received_bytes"],
        })
        return JSONResponse(info)

    _save_info(info)
    return JSONResponse(info)


def _int_header(request: Request, name: str, default: int) -> int:
    try:
        return int(request.headers.get(name) or default)
    except ValueError:
        return default


def merge_csv_sources(
    sources: list[tuple[str, Path]],
    output: Path,
    *,
    metadata_prefix: str = "",
) -> dict:
    """Merge CSVs sharing the sensor schema into one file.

    Dedups rows on (device_id, sequence_number) — the same physical sample can arrive
    through several paths (live WS writes, late delivery sidecars, phone rescue
    uploads), all of which share the backend sequence numbers. Rows are then
    re-sorted by timestamp so the downstream segmentation sees a monotonic series.

    `sources` is a list of (source_label, path). The metadata prefix (when given) is
    written above the header so the merged file stays self-describing.
    """
    seen: set[str] = set()
    rows: list[list[str]] = []
    src_rows: dict[str, int] = {}
    source_files: list[dict] = []
    read_total = 0

    for label, path in sources:
        if path is None or not path.exists():
            continue
        count = 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = parse_row(line)
            if fields is None:
                continue
            read_total += 1
            key = f"{fields[COL_DEVICE_ID]}\t{fields[COL_SEQUENCE]}"
            if key in seen:
                continue
            seen.add(key)
            rows.append(fields)
            count += 1
        src_rows[label] = src_rows.get(label, 0) + count
        source_files.append({"path": str(path), "label": label, "rows": count})

    output.parent.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda p: int(p[0]) if p[0].isdigit() else 0)
    with open(output, "w", encoding="utf-8", newline="") as f:
        if metadata_prefix:
            f.write(f"# {metadata_prefix}\n")
        f.write(_CSV_HEADER)
        for p in rows:
            f.write(",".join(p) + "\n")

    return {
        "path": str(output),
        "rows": len(rows),
        "sources": src_rows,
        "source_files": source_files,
        "duplicates_dropped": read_total - len(rows),
    }


def merge_csv_sources_per_role(
    sources: list[tuple[str, str, Path]],
    output_dir: Path,
    session_id: str,
    *,
    metadata_prefix: str = "",
) -> dict:
    """Merge CSVs into one <session_id>_<role>_consolidated.csv per role bucket.

    Per-role counterpart of merge_csv_sources: `sources` are (role_key, label, path)
    triples. Roles are bucketed independently, each deduped on (device_id, sequence_number)
    and re-sorted by timestamp, then written next to the session-wide consolidated file so
    operators can hand one device's full series (live + late + rescue + recovery) to a
    subject without the other devices' rows mixed in.
    """
    buckets: dict[str, dict] = {}
    read_total = 0
    for role_key, label, path in sources:
        if path is None or not path.exists():
            continue
        b = buckets.setdefault(role_key, {
            "seen": set(),
            "rows": [],
            "src_rows": {},
            "source_files": [],
            "read_total": 0,
        })
        count = 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = parse_row(line)
            if fields is None:
                continue
            b["read_total"] += 1
            read_total += 1
            key = f"{fields[COL_DEVICE_ID]}\t{fields[COL_SEQUENCE]}"
            if key in b["seen"]:
                continue
            b["seen"].add(key)
            b["rows"].append(fields)
            count += 1
        b["src_rows"][label] = b["src_rows"].get(label, 0) + count
        b["source_files"].append({"path": str(path), "label": label, "rows": count})

    output_dir.mkdir(parents=True, exist_ok=True)
    per_role: dict[str, dict] = {}
    written: list[str] = []
    for role_key, b in sorted(buckets.items()):
        b["rows"].sort(key=lambda p: int(p[0]) if p[0].isdigit() else 0)
        out_path = output_dir / f"{session_id}_{role_key}_consolidated.csv"
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            if metadata_prefix:
                f.write(f"# {metadata_prefix}\n")
            f.write(_CSV_HEADER)
            for p in b["rows"]:
                f.write(",".join(p) + "\n")
        per_role[role_key] = {
            "path": str(out_path),
            "rows": len(b["rows"]),
            "sources": b["src_rows"],
            "source_files": b["source_files"],
            "duplicates_dropped": b["read_total"] - len(b["rows"]),
        }
        written.append(str(out_path))

    return {
        "files": written,
        "roles": len(per_role),
        "rows": sum(p["rows"] for p in per_role.values()),
        "per_role": per_role,
    }


# ── Desktop pull ─────────────────────────────────────────────────────────────


@router.get("/recovery/sessions")
async def recovery_sessions(include_done: bool = False):
    """List recovery upload sessions.

    By default hides sessions whose files were all marked done (operator "Done"
    button). Pass `include_done=true` to see them again.
    """
    out = []
    if RECOVERY_PATH.exists():
        for d in sorted(RECOVERY_PATH.iterdir()):
            if not d.is_dir():
                continue
            infos = [json.loads(p.read_text(encoding="utf-8"))
                     for p in d.glob("*.info.json")]
            if not infos:
                continue
            all_done = all(i.get("done", False) for i in infos)
            if all_done and not include_done:
                continue
            out.append({
                "session_id": d.name,
                "files": infos,
                "done": all_done,
            })
    return out


def _set_done(session_id: str, done: bool) -> list[dict]:
    """Flip the operator `done` flag on every file of a recovery session."""
    d = _session_dir(session_id)
    updated = []
    for p in d.glob("*.info.json"):
        try:
            info = dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
        info["done"] = done
        info["updated_at_ms"] = int(time.time() * 1000)
        p.write_text(json.dumps(info, indent=2), encoding="utf-8")
        updated.append(info.get("device_id"))
    return updated


@router.post("/recovery/{session_id}/dismiss")
async def recovery_dismiss(session_id: str):
    """Mark a recovery session as done so it stops cluttering the dashboard.

    Non-destructive: the CSVs remain on disk and can be restored / re-shown.
    """
    updated = _set_done(session_id, True)
    if not updated:
        raise HTTPException(status_code=404, detail="no recovery files for session")
    await audit.log("INFO", "recovery_dismissed", {"session_id": session_id, "devices": updated})
    return {"session_id": session_id, "done": True, "devices": updated}


@router.post("/recovery/{session_id}/restore")
async def recovery_restore(session_id: str):
    """Undo dismiss — bring a done recovery session back to the active list."""
    updated = _set_done(session_id, False)
    if not updated:
        raise HTTPException(status_code=404, detail="no recovery files for session")
    return {"session_id": session_id, "done": False, "devices": updated}


@router.get("/recovery/{session_id}/files")
async def recovery_files(session_id: str):
    d = _session_dir(session_id)
    infos = []
    for p in sorted(d.glob("*.info.json")):
        info = json.loads(p.read_text(encoding="utf-8"))
        info["file"] = p.name.replace(".info.json", ".csv")
        csv = d / (info["file"])
        info["size"] = csv.stat().st_size if csv.exists() else 0
        infos.append(info)
    return infos


@router.get("/recovery/{session_id}/files/{filename}")
async def recovery_download(session_id: str, filename: str):
    if not filename.endswith(".csv") or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    d = _session_dir(session_id)
    fp = d / filename
    if not fp.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(fp, media_type="text/csv", filename=f"{session_id}_{filename}")


@router.post("/recovery/{session_id}/merge")
async def recovery_merge(session_id: str):
    """Merge all complete recovery CSVs into <SSD_PATH>/Data_Riset_IMU/<subject>_<tag>/.

    Union of data rows keyed on (device_id, sequence_number), re-sorted by timestamp, written
    to <subject>_<tag>/<session_id>_merged.csv. Returns the merged file path and row counts.
    """
    d = _session_dir(session_id)
    sources: list[tuple[str, Path]] = []
    for p in d.glob("*.info.json"):
        info = json.loads(p.read_text(encoding="utf-8"))
        csv = d / f"{_slug(info['device_id'])}.csv"
        if csv.exists() and info.get("complete"):
            sources.append((info["device_id"], csv))
    if not sources:
        raise HTTPException(status_code=404, detail="no complete recovery files to merge")

    infos = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in d.glob("*.info.json")
    ]
    subject = infos[0].get("subject", "Unknown") if infos else "Unknown"
    tag = infos[0].get("session_tag", "Session") if infos else "Session"

    ssd = Path(os.getenv("SSD_PATH", "./data")) / "Data_Riset_IMU" / f"{subject}_{tag}".replace(" ", "_")
    out_path = ssd / f"{session_id}_merged.csv"
    result = merge_csv_sources(
        sources,
        out_path,
        metadata_prefix=f"session_id={session_id},subject={subject},session_tag={tag},source=recovery_merge",
    )

    await audit.log("INFO", "recovery_merged", {
        "session_id": session_id, "devices": result["sources"], "rows": result["rows"],
        "path": result["path"],
    })
    return {
        "session_id": session_id,
        "path": result["path"],
        "rows": result["rows"],
        "devices": result["sources"],
        "duplicates_dropped": result["duplicates_dropped"],
    }
