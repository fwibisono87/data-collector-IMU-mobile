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
    infos = []
    for p in d.glob("*.info.json"):
        info = json.loads(p.read_text(encoding="utf-8"))
        csv = d / f"{_slug(info['device_id'])}.csv"
        if csv.exists() and info.get("complete"):
            infos.append((info, csv))
    if not infos:
        raise HTTPException(status_code=404, detail="no complete recovery files to merge")

    subject = infos[0][0].get("subject", "Unknown")
    tag = infos[0][0].get("session_tag", "Session")

    header = (
        "timestamp_ms,acc_x_g,acc_y_g,acc_z_g,"
        "gyro_x_degs,gyro_y_degs,gyro_z_degs,"
        "label_id,label_name,sequence_number,device_id\n"
    )
    seen = set()
    rows = []
    sources = {}
    read_total = 0
    for info, csv in infos:
        lines = csv.read_text(encoding="utf-8", errors="replace").splitlines()
        src_rows = 0
        for line in lines:
            if line.startswith("#") or line == "":
                continue
            if line.rstrip("\n") == header.rstrip("\n"):
                continue
            parts = line.split(",")
            if len(parts) < 11:
                continue
            read_total += 1
            seq = parts[9]
            dev = parts[10]
            key = f"{dev}\t{seq}"
            if key in seen:
                continue
            seen.add(key)
            rows.append(parts)
            src_rows += 1
        sources[info["device_id"]] = src_rows
    duplicates_dropped = read_total - len(rows)

    rows.sort(key=lambda p: int(p[0]) if p[0].isdigit() else 0)

    ssd = Path(os.getenv("SSD_PATH", "./data")) / "Data_Riset_IMU" / f"{subject}_{tag}".replace(" ", "_")
    ssd.mkdir(parents=True, exist_ok=True)
    out_path = ssd / f"{session_id}_merged.csv"
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write(f"# session_id={session_id},subject={subject},session_tag={tag},source=recovery_merge\n")
        f.write(header)
        for p in rows:
            f.write(",".join(p) + "\n")

    await audit.log("INFO", "recovery_merged", {
        "session_id": session_id, "devices": sources, "rows": len(rows), "path": str(out_path),
    })
    return {
        "session_id": session_id,
        "path": str(out_path),
        "rows": len(rows),
        "devices": sources,
        "duplicates_dropped": duplicates_dropped,
    }
