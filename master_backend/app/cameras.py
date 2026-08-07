"""
Camera sync anchors — durable per-camera wall-clock marks.

Webcam video is recorded in the operator's browser while IMU data is written
server-side. Aligning the two needs per-camera wall-clock anchors (started / flash /
stopped). These used to be assembled client-side and only written into the ZIP at
download time; a dashboard crash before download lost them entirely. This module keeps
the anchors server-side, persisted the moment each mark arrives, so a session's video
remains alignable regardless of what happens to the browser afterwards.

Storage: <session_folder>/<session_id>_cameras.json, where the session folder uses the
same resolution export.py does. If no session folder exists yet (a mark can arrive
before any CSV is flushed) we fall back to SSD_PATH/Data_Riset_IMU/_pending_cameras/
and note that in the audit log. Merges are a whole-file read-merge-write so a mark can
never collide with a concurrent one.
"""
import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .audit_logger import audit
from .export import _session_folders

logger = logging.getLogger(__name__)

router = APIRouter(tags=["cameras"])

SSD_PATH = Path(os.getenv("SSD_PATH", "./data"))
PENDING_CAMERAS = SSD_PATH / "Data_Riset_IMU" / "_pending_cameras"

_VALID_EVENTS = ("started", "flash", "stopped")


def _slug(s: str) -> str:
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in s)


def _validate_session_id(session_id: str) -> str:
    if not session_id or any(c in session_id for c in "/\\"):
        raise HTTPException(status_code=400, detail="invalid session_id")
    return session_id


def _cameras_path(session_id: str) -> Path:
    folders = _session_folders(session_id)
    if folders:
        return folders[0] / f"{session_id}_cameras.json"
    return PENDING_CAMERAS / f"{session_id}_cameras.json"


def _default_file(session_id: str) -> dict:
    return {"session_id": session_id, "cameras": []}


def _read_cameras(path: Path, session_id: str) -> dict:
    if not path.exists():
        return _default_file(session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _default_file(session_id)
    if not isinstance(data, dict) or not isinstance(data.get("cameras"), list):
        return _default_file(session_id)
    data.setdefault("session_id", session_id)
    return data


def _write_cameras(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


@router.post("/cameras/{session_id}/mark")
async def cameras_mark(session_id: str, body: dict):
    _validate_session_id(session_id)

    cam_id = (body.get("cam_id") or "").strip()
    event = (body.get("event") or "").strip()
    if not cam_id:
        raise HTTPException(status_code=400, detail="cam_id is required")
    if event not in _VALID_EVENTS:
        raise HTTPException(status_code=400, detail=f"unknown event '{event}'")

    ts_ms = body.get("ts_ms")
    try:
        ts_ms = int(ts_ms)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="ts_ms must be an integer")

    path = _cameras_path(session_id)
    data = _read_cameras(path, session_id)

    record = next(
        (c for c in data["cameras"] if c.get("cam_id") == cam_id),
        None,
    )
    if record is None:
        record = {
            "session_id": session_id,
            "cam_id": cam_id,
            "device_id": (body.get("device_id") or "").strip(),
            "browser_label": (body.get("label") or "").strip(),
            "mime": (body.get("mime") or "").strip(),
            "started_at_ms": 0,
            "flash_at_ms": 0,
            "stopped_at_ms": 0,
        }
        data["cameras"].append(record)

    record[f"{event}_at_ms"] = ts_ms
    _write_cameras(path, data)

    note = "camera_mark_pending_folder" if not _session_folders(session_id) else "camera_mark"
    await audit.log("INFO", note, {
        "session_id": session_id,
        "cam_id": cam_id,
        "event": event,
        "ts_ms": ts_ms,
        "path": str(path),
    })
    return record


@router.get("/cameras/{session_id}")
async def cameras_get(session_id: str):
    _validate_session_id(session_id)
    path = _cameras_path(session_id)
    return _read_cameras(path, session_id)
