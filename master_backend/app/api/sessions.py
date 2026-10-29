"""Session history, detail, ZIP download, package and open-folder (API.md).

Everything here reads the disk and the ledger; nothing is cached in the browser. The
discovery scan is cached per folder and invalidated by the folder's mtime, which changes
whenever a session starts or a post-session artifact lands.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response, StreamingResponse

from .. import export as export_module
from ..session_manager import session_manager
from ..session_zip import session_zip_name
from .errors import busy, invalid, not_found, not_supported
from .post_session import PostBusy, session_progress
from .runtime import Runtime, get_runtime
from .schema import (
    LateRows,
    PackageResponse,
    SessionDetail,
    SessionFile,
    SessionList,
    SessionSummary,
    UploadStatus,
)

# Mounted under /api by routes.py; keep the paths relative so there is one prefix.
router = APIRouter(tags=["sessions"])

_SESSION_FILE_RE = re.compile(r"^(\d{10,})_")
_CHUNK = 1024 * 1024
_RESERVED_FOLDERS = {"_state", "_pending_cameras", "_recovered"}

_discovery_cache: dict[str, tuple[tuple, dict[str, Path]]] = {}


def _sessions_base() -> Path:
    return export_module.SSD_PATH / "Data_Riset_IMU"


def _discover_sessions() -> dict[str, Path]:
    """session_id → folder, from `<sid>_*` files plus the ledger.

    Folder mtimes change when a session's first file appears, so a cached scan can never
    hide a new session; sessions whose files were removed still surface from the ledger.
    """
    base = _sessions_base()
    signature = (str(base), base.stat().st_mtime_ns if base.is_dir() else 0)
    cached = _discovery_cache.get(str(base))
    if cached is not None and cached[0] == signature:
        found = dict(cached[1])
    else:
        found: dict[str, Path] = {}
        if base.is_dir():
            for folder in sorted(base.iterdir()):
                if not folder.is_dir() or folder.name in _RESERVED_FOLDERS:
                    continue
                for path in folder.iterdir():
                    match = _SESSION_FILE_RE.match(path.name)
                    if match and path.is_file():
                        found.setdefault(match.group(1), folder)
        _discovery_cache[str(base)] = (signature, dict(found))

    for record in session_manager._ledger.list():
        sid = str(record.get("session_id") or "")
        if not sid or sid in found:
            continue
        folders = export_module._session_folders(sid)
        found[sid] = folders[0] if folders else export_module._recovery_dir(sid)
    return found


def _ledger(session_id: str) -> dict:
    return session_manager.get_recovery_session(session_id) or {}


def _camera_ids(session_id: str, folder: Path) -> list[str]:
    data = {}
    try:
        data = json.loads((folder / f"{session_id}_cameras.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [str(c.get("cam_id")) for c in data.get("cameras") or [] if c.get("cam_id")]


def _zip_info(rt: Runtime, session_id: str, folder: Path) -> tuple[str | None, int | None]:
    record = rt.post.record(session_id) if rt.post is not None else None
    candidate = Path(str((record or {}).get("zip_path") or ""))
    if not candidate.is_file():
        found = sorted(folder.glob(f"*_{session_id}.zip"))
        candidate = found[0] if found else None
    if candidate is None or not candidate.is_file():
        return None, None
    try:
        return str(candidate), candidate.stat().st_size
    except OSError:
        return str(candidate), None


def _outstanding_uploads(session_id: str) -> int:
    count = 0
    for info in export_module._recovery_manifest(session_id):
        if info.get("verified") or info.get("done"):
            continue
        count += 1
    return count


def _summary(rt: Runtime, session_id: str, folder: Path) -> SessionSummary:
    ledger = _ledger(session_id)
    report = ledger.get("integrity_report") or {}
    verdict = str(report.get("status") or "UNKNOWN")
    if verdict not in ("PASS", "PARTIAL", "FAIL"):
        verdict = "UNKNOWN"
    started = int(ledger.get("recording_started_ms") or session_id or 0)
    ended = int(ledger["finalized_at_ms"]) if ledger.get("finalized_at_ms") else None
    zip_path, zip_bytes = _zip_info(rt, session_id, folder)
    return SessionSummary(
        session_id=session_id,
        subject=str(ledger.get("subject_name") or ""),
        session_tag=str(ledger.get("session_tag") or ""),
        operator=str(ledger.get("operator") or ""),
        started_at_ms=started,
        ended_at_ms=ended,
        duration_s=round((ended - started) / 1000, 3) if ended and started else None,
        phones=[str(d.get("device_id")) for d in ledger.get("devices") or []
                if d.get("device_id")],
        cameras=_camera_ids(session_id, folder),
        verdict=verdict,  # type: ignore[arg-type]
        folder=str(folder),
        zip_path=zip_path,
        zip_bytes=zip_bytes,
        outstanding_uploads=_outstanding_uploads(session_id),
        notices=rt.notices.count_for_session(session_id),
    )


@router.get("/sessions", response_model=SessionList)
async def list_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    before: str | None = Query(default=None),
):
    rt = get_runtime()
    found = _discover_sessions()
    session_ids = sorted(found, reverse=True)
    if before:
        session_ids = [sid for sid in session_ids if sid < before]
    page = session_ids[:limit]
    sessions = [_summary(rt, sid, found[sid]) for sid in page]
    following = session_ids[limit] if len(session_ids) > limit else None
    return SessionList(sessions=sessions, next=following)


def _late_rows(session_id: str, folder: Path, ledger: dict) -> list[LateRows]:
    try:
        data = json.loads(
            (folder / f"{session_id}_late_delivery.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return []
    roles = {
        str(d.get("device_id")): str(d.get("role") or "")
        for d in ledger.get("devices") or []
    }
    rows: list[LateRows] = []
    for device_id, summary in (data.get("devices") or {}).items():
        rows.append(LateRows(
            device_id=str(device_id),
            role=roles.get(str(device_id), ""),
            rows=int((summary or {}).get("rows_appended", 0) or 0),
        ))
    return rows


def _uploads(session_id: str) -> list[UploadStatus]:
    out: list[UploadStatus] = []
    for info in export_module._recovery_manifest(session_id):
        total = info.get("total_bytes")
        out.append(UploadStatus(
            device_id=str(info.get("device_id") or ""),
            role=str(info.get("role") or ""),
            received_bytes=int(info.get("received_bytes", 0) or 0),
            total_bytes=int(total) if total not in (None, "") else None,
            complete=bool(info.get("complete")),
            verified=bool(info.get("verified")),
        ))
    return out


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def session_detail(session_id: str):
    rt = get_runtime()
    found = _discover_sessions()
    folder = found.get(session_id)
    if folder is None:
        raise not_found(f"session {session_id} was not found")
    summary = _summary(rt, session_id, folder)
    ledger = _ledger(session_id)
    # FinalizeJob's steps (live or from the ledger) followed by cameras/package, so the
    # Done view can show the whole chain that produced the ZIP.
    progress = session_progress(rt.post, session_id)
    steps = progress["steps"] if progress else []
    files = [
        SessionFile(name=f["name"], bytes=int(f.get("size", 0)), kind=str(f.get("kind", "")))
        for f in export_module._session_files(session_id)
    ]
    return SessionDetail(
        **summary.model_dump(),
        finalize=steps,
        integrity_report=ledger.get("integrity_report"),
        files=files,
        late=_late_rows(session_id, folder, ledger),
        uploads=_uploads(session_id),
    )


@router.post("/sessions/{session_id}/package", response_model=PackageResponse)
async def package_session(session_id: str):
    rt = get_runtime()
    if rt.post is None:
        raise busy("the post-session job is not running")
    try:
        path = await rt.post.package(session_id)
    except PostBusy as exc:
        raise busy(str(exc)) from exc
    except FileNotFoundError as exc:
        raise not_found(str(exc)) from exc
    return PackageResponse(zip_path=str(path))


def _zip_for(session_id: str, folder: Path) -> Path:
    posts = get_runtime().post
    record = posts.record(session_id) if posts is not None else None
    candidate = Path(str((record or {}).get("zip_path") or ""))
    if candidate.is_file():
        return candidate
    found = sorted(folder.glob(f"*_{session_id}.zip"))
    if not found:
        raise not_found(f"no ZIP has been built for session {session_id} yet")
    return found[0]


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Parse one ``bytes=`` range. Returns (start, end) inclusive, or None to ignore it."""
    if not header.startswith("bytes="):
        return None
    spec = header[len("bytes="):].split(",", 1)[0].strip()
    if not spec:
        return None
    start_s, _, end_s = spec.partition("-")
    try:
        if not start_s:                        # bytes=-N: the last N bytes
            suffix = int(end_s)
            if suffix <= 0:
                return None
            start = max(size - suffix, 0)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return (-1, -1)                        # valid syntax, unsatisfiable
    return start, min(end, size - 1)


async def _file_chunks(path: Path, start: int, length: int):
    async with aiofiles.open(path, "rb") as handle:
        await handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = await handle.read(min(_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@router.get("/sessions/{session_id}/zip")
async def download_zip(session_id: str, request: Request):
    rt = get_runtime()
    found = _discover_sessions()
    folder = found.get(session_id)
    if folder is None:
        raise not_found(f"session {session_id} was not found")
    path = _zip_for(session_id, folder)
    size = path.stat().st_size
    ledger = _ledger(session_id)
    filename = session_zip_name(
        str(ledger.get("subject_name") or ""),
        str(ledger.get("session_tag") or ""),
        session_id,
    )
    disposition = f'attachment; filename="{filename.replace(chr(34), "_")}"'
    headers = {
        "Content-Disposition": disposition,
        "Accept-Ranges": "bytes",
    }
    header = request.headers.get("range")
    if header:
        parsed = _parse_range(header, size)
        if parsed == (-1, -1):
            return Response(
                status_code=416,
                headers={**headers, "Content-Range": f"bytes */{size}"},
            )
        if parsed is not None:
            start, end = parsed
            return StreamingResponse(
                _file_chunks(path, start, end - start + 1),
                status_code=206,
                media_type="application/zip",
                headers={
                    **headers,
                    "Content-Range": f"bytes {start}-{end}/{size}",
                    "Content-Length": str(end - start + 1),
                },
            )
    return StreamingResponse(
        _file_chunks(path, 0, size),
        media_type="application/zip",
        headers={**headers, "Content-Length": str(size)},
    )


@router.post("/sessions/{session_id}/open-folder")
async def open_folder(session_id: str):
    rt = get_runtime()
    found = _discover_sessions()
    folder = found.get(session_id)
    if folder is None:
        raise not_found(f"session {session_id} was not found")
    if os.name == "nt":
        zip_path, _ = _zip_info(rt, session_id, folder)
        target = zip_path or str(folder)
        args = ["explorer", "/select,", target]
    elif sys.platform.startswith("linux"):
        args = ["xdg-open", str(folder)]
    else:
        raise not_supported("opening a folder is not supported on this platform")
    try:
        # Never block the request on the file manager, and never fail the API if the
        # desktop is headless: the path is also visible in the session detail.
        subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise invalid(f"could not open the folder: {exc}") from exc
    return {}