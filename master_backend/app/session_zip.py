"""Server-side session ZIP: the deliverable, shaped exactly like the browser's.

This is a port of ``buildStreamEntries`` in
``master_frontend/src/components/EndSessionModal.tsx`` (frozen by
docs/OUTPUT_CONTRACT.md §9), so the operator can get the archive with the dashboard
closed and without the browser being load-bearing for the research data.  Entry names,
order and the three generated JSON documents must match the browser's; the one
deliberate difference is where the bytes come from (the SSD instead of IndexedDB).

Streamed straight to disk in 1 MiB chunks: no file is ever held in memory, and the
archive is written to a ``.tmp`` path, fsynced and moved into place with ``os.replace``
so an interrupted build never leaves a truncated file that looks finished.
"""
from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path
from typing import Iterable, Sequence

from .api.jsjson import js_json_dumps
from .camera.interface import CameraEvent, CameraRecording

# Ported verbatim from master_frontend/src/lib/export_client.ts (DATA_KINDS).  These are
# the manifest kinds the browser puts under data/; everything else (the backend bundle,
# camera anchors, videos, the ledger) stays out of the deliverable.  The kinds
# themselves are still produced by export.py's manifest builder — never re-classify.
DATA_KINDS = frozenset({
    "main", "csv", "late", "rescue", "merged", "consolidated",
    "integrity", "connectivity", "late_summary", "consolidation",
    "consolidated_validation", "timing",
})

VIDEO_PREFIX = "videos/"
_CHUNK = 1024 * 1024

EXPORT_ERROR_TEXT = (
    "This ZIP contains video only — pull/rescue CSVs from the Recovery screen.\n"
)


def is_data_kind(kind: str) -> bool:
    return kind in DATA_KINDS


def session_zip_name(subject: str | None, session_tag: str | None, session_id: str) -> str:
    """``<subject>_<tag>_<sid>.zip``, whitespace becomes ``_``, empty values defaulted.

    Mirrors ```${subject || "subject"}_${tag || "session"}_${sid}`.replace(/\\s+/g, "_")``.
    Path separators are additionally replaced: the browser only ever builds a save-file
    name, while this name is also a path on the SSD, where '/' would escape the folder.
    """
    name = f"{subject or 'subject'}_{session_tag or 'session'}_{session_id}"
    name = re.sub(r"\s+", "_", name)
    return name.replace("/", "_").replace("\\", "_") + ".zip"


def _camera_doc(session_id: str, rec: CameraRecording) -> dict:
    files = [VIDEO_PREFIX + Path(p).name for p in rec.files]
    return {
        "session_id": session_id,
        "cam_id": rec.cam_id,
        "device_id": rec.device_key,
        "browser_label": rec.label,
        "mime": rec.mime,
        # Legacy single-file field plus every segment, so a recorder restart is never
        # mistaken for one stacked file (OUTPUT_CONTRACT §9).
        "file": files[0] if files else f"{VIDEO_PREFIX}{session_id}_{rec.cam_id}_video_sync.webm",
        "files": files,
        "started_at_ms": rec.started_at_ms,
        "flash_at_ms": rec.flash_at_ms,
        "stopped_at_ms": rec.stopped_at_ms,
    }


def _event_doc(event: CameraEvent) -> dict:
    doc = {
        "key": event.key,
        "sessionId": event.sessionId,
        "camId": event.camId,
        "type": event.type,
        "atMs": event.atMs,
    }
    if event.detail:
        doc["detail"] = event.detail
    return doc


def cameras_document(
    session_id: str,
    recordings: Sequence[CameraRecording],
    events: Sequence[CameraEvent],
) -> dict:
    return {
        "session_id": session_id,
        "cameras": [_camera_doc(session_id, rec) for rec in recordings],
        "camera_events": [_event_doc(event) for event in events],
    }


def _report_doc(report) -> dict:
    doc = {
        "path": report.path,
        "camId": report.camId,
        "ok": report.ok,
        "bytesWritten": report.bytesWritten,
        "durationMs": report.durationMs,
        "ebmlHeaders": report.ebmlHeaders,
    }
    if report.error:
        doc["error"] = report.error
    return doc


def video_report_document(
    session_id: str,
    recordings: Sequence[CameraRecording],
) -> dict:
    videos = [_report_doc(report) for rec in recordings for report in rec.reports]
    return {
        "session_id": session_id,
        "videos": videos,
        "healthy": all(v["ok"] and v["ebmlHeaders"] == 1 for v in videos),
    }


def _write_bytes(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_DEFLATED
    with zf.open(info, "w") as dst:
        dst.write(data)


def _write_text(zf: zipfile.ZipFile, name: str, text: str) -> None:
    _write_bytes(zf, name, text.encode("utf-8"))


def _copy_file(zf: zipfile.ZipFile, name: str, src: Path, *, stored: bool) -> None:
    info = zipfile.ZipInfo.from_file(src, name)
    # WebM is already compressed; storing it saves the CPU and does not grow the file.
    info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with src.open("rb") as fsrc, zf.open(info, "w") as fdst:
        while True:
            chunk = fsrc.read(_CHUNK)
            if not chunk:
                break
            fdst.write(chunk)


def _manifest_entries(manifest: dict) -> Iterable[tuple[str, Path]]:
    """``data/<name>`` for every manifest file whose kind is a data kind."""
    for f in manifest.get("files", []):
        if not is_data_kind(str(f.get("kind", ""))):
            continue
        src = Path(str(f.get("path", "")))
        if src.is_file():
            yield f"data/{f['name']}", src


def _recovery_entries(manifest: dict) -> Iterable[tuple[str, Path]]:
    """``data/recovery/<device_id>.csv`` for complete uploads whose CSV exists."""
    for r in manifest.get("recovery", []):
        if not (r.get("complete") and r.get("csv_exists")):
            continue
        src = Path(str(r.get("csv_path", "")))
        if src.is_file():
            yield f"data/recovery/{r['device_id']}.csv", src


def build_session_zip(
    session_id: str,
    folder: Path,
    *,
    subject: str = "",
    session_tag: str = "",
    manifest: dict | None,
    recordings: Sequence[CameraRecording] = (),
    events: Sequence[CameraEvent] = (),
    missed: Sequence[str] = (),
    error_text: str = "",
) -> Path:
    """Write ``<subject>_<tag>_<sid>.zip`` into ``folder`` and return its path.

    ``manifest`` is export.py's ``export_manifest`` output (already computed by the
    caller, which may have refused to build it): pass None to produce the browser's
    video-only archive with ``data/export_error.txt``.
    Blocking I/O — call it through ``asyncio.to_thread`` from the event loop.
    """
    folder = Path(folder)
    target = folder / session_zip_name(
        subject or (manifest or {}).get("subject", ""),
        session_tag or (manifest or {}).get("session_tag", ""),
        session_id,
    )
    tmp = target.with_name(target.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            # 1. Videos, in each camera's part order — the only video in the deliverable.
            for rec in recordings:
                for path in rec.files:
                    src = Path(path)
                    if src.is_file():
                        _copy_file(zf, VIDEO_PREFIX + src.name, src, stored=True)

            # 2-3. Backend data artifacts and verified recovery CSVs, or the error note.
            if manifest is not None:
                for name, src in _manifest_entries(manifest):
                    _copy_file(zf, name, src, stored=False)
                for name, src in _recovery_entries(manifest):
                    _copy_file(zf, name, src, stored=False)
                _write_text(zf, "data/manifest.json", js_json_dumps(manifest))
            else:
                detail = error_text or "not reachable"
                _write_text(
                    zf,
                    "data/export_error.txt",
                    f"Backend data unavailable ({detail}).\n" + EXPORT_ERROR_TEXT,
                )

            # 4. The camera record the browser used to build from IndexedDB.
            _write_text(
                zf, "cameras.json",
                js_json_dumps(cameras_document(session_id, recordings, events)),
            )

            # 5. Only when a selected camera never produced footage.
            if missed:
                _write_text(zf, "missed_cameras.txt", "\n".join(missed) + "\n")

            # 6. Evidence for every video, written last so it observes all of them.
            _write_text(
                zf, "video_report.json",
                js_json_dumps(video_report_document(session_id, recordings)),
            )

        # Durability before the rename: a crash must not leave a visible partial archive.
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target