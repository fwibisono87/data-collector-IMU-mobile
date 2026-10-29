"""Post-session job: camera stop and the server-built ZIP (L4 spec §4).

Kept separate from :class:`~master_backend.app.finalize_job.FinalizeJob` so the frozen
ledger and its three steps are untouched.  This job has its own record,
``_state/sessions/<sid>.post.json``, with the two steps the v3 dashboard shows after
the finalize steps: ``cameras`` and ``package``.

Flow at STOP::

    POST /api/session/stop
      session_manager.request_stop()      -> FinalizeJob starts (unchanged)
      PostSessionJob.on_stopped(sid)      -> cameras step starts immediately,
                                             concurrently with FinalizeJob
      package                             -> runs once FinalizeJob has settled AND
                                             cameras is done/failed/skipped

The package step is re-run (debounced by 5 s) when late telemetry or a verified
rescue upload changes the session's data files, and manually via
``POST /api/sessions/{sid}/package``.  On backend start, unfinished records are
resumed; if the backend died mid-recording the cameras step is rebuilt from the
``<sid>_cameras.json`` anchors and the ``*_video_sync*.webm`` files on disk (each file
is probed and remuxed with ffmpeg when it has no duration, per the spec).

Every step change is persisted before it is published, so a crash can never leave the
dashboard showing progress the disk does not agree with.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import time
from pathlib import Path
from typing import Callable, Sequence

from ..camera.interface import (
    CameraEvent,
    CameraRecording,
    CameraStopResult,
    VideoFileReport,
)
from ..export import _session_folders, export_manifest
from ..session_manager import session_manager
from ..session_zip import build_session_zip
from .runtime import Runtime, atomic_write_json, read_json

logger = logging.getLogger(__name__)

STEP_NAMES = ("cameras", "package")
_PENDING, _RUNNING, _DONE, _FAILED, _SKIPPED = (
    "pending", "running", "done", "failed", "skipped",
)
_TERMINAL = (_DONE, _FAILED, _SKIPPED)

#: Debounce window for the late-delivery / rescue-upload re-package.
REPACKAGE_DELAY_S = 5.0
#: How long package may wait for FinalizeJob before failing its step.
FINALIZE_WAIT_S = 600.0

FFPROBE = os.getenv("FFPROBE", "ffprobe")
FFMPEG = os.getenv("FFMPEG", "ffmpeg")
_EBML_MAGIC = b"\x1a\x45\xdf\xa3"


class PostBusy(RuntimeError):
    """A package run for this session is already in progress (409 ``busy``)."""


class PostSessionJob:
    """Owns the ``cameras`` and ``package`` steps for every session of this process."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._tasks: dict[str, asyncio.Task] = {}
        self._debounce: dict[str, asyncio.Task] = {}
        self._running: set[str] = set()
        self._unwire: list[Callable[[], None]] = []

    # -- wiring ------------------------------------------------------------

    def start(self) -> None:
        """Subscribe to the events that trigger a re-package."""
        from ..upload import add_upload_hook

        session_manager.add_observer(self._on_lifecycle)
        add_upload_hook(self._on_upload)
        self._unwire = [
            lambda: self._discard(session_manager._observers, self._on_lifecycle),
            lambda: self._discard_upload_hook(self._on_upload),
        ]

    @staticmethod
    def _discard(items: list, item) -> None:
        with contextlib.suppress(ValueError):
            items.remove(item)

    @staticmethod
    def _discard_upload_hook(callback) -> None:
        from ..upload import remove_upload_hook

        remove_upload_hook(callback)

    async def close(self) -> None:
        for unwire in self._unwire:
            with contextlib.suppress(Exception):
                unwire()
        self._unwire = []
        tasks = list(self._tasks.values()) + list(self._debounce.values())
        self._tasks.clear()
        self._debounce.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -- trigger events ----------------------------------------------------

    async def _on_lifecycle(self, event: dict) -> None:
        if event.get("type") == "LATE_DELIVERY" and event.get("session_id"):
            self.schedule_package(str(event["session_id"]))

    def _on_upload(self, session_id: str, info: dict) -> None:
        if info.get("complete") and info.get("sha256_verified"):
            self.schedule_package(session_id)

    def schedule_package(self, session_id: str, delay: float = REPACKAGE_DELAY_S) -> None:
        """(Re)start the debounce timer for an automatic re-package."""
        if not session_id:
            return
        previous = self._debounce.pop(session_id, None)
        if previous is not None and not previous.done():
            previous.cancel()
        self._debounce[session_id] = asyncio.create_task(
            self._debounced_package(session_id, delay),
            name=f"package-debounce-{session_id}",
        )

    async def _debounced_package(self, session_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._debounce.pop(session_id, None)
        # A run that started meanwhile will not necessarily see the newest late rows;
        # wait for it, then package once more.
        deadline = time.monotonic() + 120
        while session_id in self._running and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        try:
            if not await self._wait_finalize(session_id):
                return
            await self.package(session_id)
        except PostBusy:
            return
        except Exception:
            logger.exception("re-package of %s failed", session_id)

    # -- REST entry points -------------------------------------------------

    async def on_stopped(self, session_id: str) -> dict:
        """Start the cameras step; package follows when finalize and cameras settle."""
        record = self._ensure_record(session_id)
        running = self._tasks.get(session_id)
        if running is not None and not running.done():
            return record
        self._tasks[session_id] = asyncio.create_task(
            self._run_after_stop(session_id), name=f"post-session-{session_id}"
        )
        return record

    async def package(self, session_id: str) -> Path:
        """Build (or rebuild) the session ZIP now. Raises ``PostBusy`` if one is running."""
        if session_id in self._running:
            raise PostBusy(f"package for {session_id} is already running")
        if not session_id:
            raise ValueError("session_id is required")
        self._running.add(session_id)
        try:
            return await self._package(session_id)
        finally:
            self._running.discard(session_id)

    async def resume_pending(self, interrupted: Sequence[str] = ()) -> None:
        """Resume unfinished post records plus sessions this startup recovered."""
        sessions = {sid for sid in interrupted if sid}
        for path in (self.rt.state_root() / "sessions").glob("*.post.json"):
            record = read_json(path, None)
            if isinstance(record, dict) and record.get("session_id"):
                sessions.add(str(record["session_id"]))
        for session_id in sorted(sessions):
            record = self._existing_record(session_id)
            if record is None:
                if session_id not in {sid for sid in interrupted if sid}:
                    continue
                # Interrupted mid-recording: no post record exists yet, but the session
                # was recovered by this startup and still owes cameras + package.
                record = self._ensure_record(session_id)
            steps = record.get("steps") or {}
            if all((steps.get(name) or {}).get("status") in _TERMINAL for name in STEP_NAMES):
                continue
            if session_id not in self._tasks:
                self._tasks[session_id] = asyncio.create_task(
                    self._resume_one(session_id), name=f"post-resume-{session_id}"
                )

    async def _resume_one(self, session_id: str) -> None:
        try:
            record = self._existing_record(session_id) or self._ensure_record(session_id)
            cameras = (record.get("steps") or {}).get("cameras") or {}
            if cameras.get("status") not in _TERMINAL:
                await self._run_cameras(session_id, resume=True)
            if not await self._wait_finalize(session_id):
                await self._set_step(session_id, "package", _FAILED,
                                     "finalize did not settle; package not attempted")
                return
            package = self.record(session_id)["steps"].get("package") or {}
            if package.get("status") == _DONE and self.record(session_id).get("zip_path"):
                return
            await self.package(session_id)
        except PostBusy:
            return
        except Exception:
            logger.exception("resume of post-session job %s failed", session_id)
        finally:
            self._tasks.pop(session_id, None)

    # -- the flow ----------------------------------------------------------

    async def _run_after_stop(self, session_id: str) -> None:
        try:
            await self._run_cameras(session_id)
            if not await self._wait_finalize(session_id):
                await self._set_step(session_id, "package", _FAILED,
                                     "finalize did not settle; package not attempted")
                return
            try:
                await self.package(session_id)
            except Exception as exc:
                logger.error("package for %s failed: %s", session_id, exc)
        finally:
            self._tasks.pop(session_id, None)

    async def _run_cameras(self, session_id: str, *, resume: bool = False) -> None:
        folder = self._session_folder(session_id)
        if folder is None:
            await self._set_step(session_id, "cameras", _FAILED,
                                 "session folder not found on disk")
            return
        await self._set_step(session_id, "cameras", _RUNNING)
        try:
            if resume or not self._cameras_recording():
                result = await self._camera_result_from_disk(session_id, folder)
                status = _DONE if (result.recordings or result.missed) else _SKIPPED
                detail = self._camera_detail(result) if status == _DONE else "no camera recording"
            else:
                result = await self.rt.camera_service.stop_recording(session_id)
                detail = self._camera_detail(result)
                status = _DONE
            self._save_camera_result(session_id, folder, result)
            self._scan_camera_events(result)
            await self._set_step(session_id, "cameras", status, detail)
        except Exception as exc:
            logger.exception("camera stop for %s failed", session_id)
            self.rt.notices.raise_(
                code="camera_stop_failed",
                level="error",
                message=f"Stopping the cameras for session {session_id} failed: {exc}",
                session_id=session_id,
                key=f"camera_stop_failed:{session_id}",
            )
            # The files on disk are still usable; try the anchors instead of losing video.
            with contextlib.suppress(Exception):
                result = await self._camera_result_from_disk(session_id, folder)
                self._save_camera_result(session_id, folder, result)
                self._scan_camera_events(result)
            await self._set_step(session_id, "cameras", _FAILED, str(exc))

    def _cameras_recording(self) -> bool:
        try:
            return bool(self.rt.camera_service.is_recording())
        except Exception:
            return False

    @staticmethod
    def _camera_detail(result: CameraStopResult) -> str:
        files = sum(len(rec.files) for rec in result.recordings)
        detail = f"{len(result.recordings)} camera(s), {files} file(s)"
        if result.missed:
            detail += f"; missed: {', '.join(result.missed)}"
        return detail

    def _scan_camera_events(self, result: CameraStopResult) -> None:
        """Notice sources visible only in the stop result (same keys as the live hook)."""
        session_id = result.session_id
        for event in result.events:
            if event.type not in ("track_ended", "write_error"):
                continue
            self.rt.notices.raise_(
                code=f"camera_{event.type}",
                level="warn",
                message=(
                    f"Camera {event.camId} reported '{event.type}' during session "
                    f"{session_id}; check the video for a gap."
                ),
                session_id=session_id,
                key=f"camera_event:{session_id}:{event.camId}:{event.type}",
            )
        for rec in result.recordings:
            for report in rec.reports:
                if report.ok and report.ebmlHeaders == 1:
                    continue
                self.rt.notices.raise_(
                    code="camera_video_invalid",
                    level="error",
                    message=(
                        f"Video {report.path} for session {session_id} is not a single "
                        f"seekable recording (ok={report.ok}, EBML headers="
                        f"{report.ebmlHeaders}). The raw file was kept."
                    ),
                    session_id=session_id,
                    key=f"camera_video:{session_id}:{report.path}",
                )

    async def _package(self, session_id: str) -> Path:
        folder = self._session_folder(session_id)
        if folder is None:
            await self._set_step(session_id, "package", _FAILED,
                                 "session folder not found on disk")
            raise FileNotFoundError(f"session folder for {session_id} not found")
        await self._set_step(session_id, "package", _RUNNING)
        try:
            ledger = session_manager.get_recovery_session(session_id) or {}
            result = self._load_camera_result(session_id, folder)
            if result is None:
                result = await self._camera_result_from_disk(session_id, folder)
            manifest = None
            error_text = ""
            try:
                manifest = await export_manifest(session_id)
            except Exception as exc:
                error_text = str(exc) or exc.__class__.__name__
                logger.warning("manifest unavailable for %s: %s", session_id, exc)
            zip_path = await asyncio.to_thread(
                build_session_zip,
                session_id,
                folder,
                subject=str(ledger.get("subject_name") or (manifest or {}).get("subject") or ""),
                session_tag=str(ledger.get("session_tag") or (manifest or {}).get("session_tag") or ""),
                manifest=manifest,
                recordings=result.recordings,
                events=result.events,
                missed=result.missed,
                error_text=error_text,
            )
        except Exception as exc:
            self.rt.notices.raise_(
                code="package_failed",
                level="error",
                message=f"Building the session ZIP for {session_id} failed: {exc}",
                session_id=session_id,
                key=f"package_failed:{session_id}",
            )
            await self._set_step(session_id, "package", _FAILED, str(exc))
            raise
        self.rt.notices.resolve(f"package_failed:{session_id}")
        # Record the archive path first, then the step; _set_step re-reads and writes the
        # whole record, so this ordering persists both in one file write.
        record = self._existing_record(session_id) or self._ensure_record(session_id)
        record["zip_path"] = str(zip_path)
        self._write_record(session_id, record)
        await self._set_step(session_id, "package", _DONE, zip_path.name)
        self.rt.note_session(session_id)
        return zip_path

    # -- camera results ----------------------------------------------------

    def _camera_result_path(self, session_id: str, folder: Path) -> Path:
        return folder / "_extras" / f"{session_id}_camera_result.json"

    def _save_camera_result(self, session_id: str, folder: Path,
                            result: CameraStopResult) -> None:
        try:
            atomic_write_json(
                self._camera_result_path(session_id, folder),
                result.model_dump(mode="json"),
            )
        except OSError as exc:
            logger.warning("could not persist camera result for %s: %s", session_id, exc)

    def _load_camera_result(self, session_id: str, folder: Path) -> CameraStopResult | None:
        data = read_json(self._camera_result_path(session_id, folder), None)
        if not isinstance(data, dict):
            return None
        try:
            return CameraStopResult.model_validate(data)
        except Exception:
            return None

    async def _camera_result_from_disk(
        self, session_id: str, folder: Path,
    ) -> CameraStopResult:
        """Rebuild the stop result from anchors + video files (restart / failed stop)."""
        anchors = read_json(folder / f"{session_id}_cameras.json", {"cameras": []})
        recordings: list[CameraRecording] = []
        events: list[CameraEvent] = []
        missed: list[str] = []
        for cam in anchors.get("cameras") or []:
            cam_id = str(cam.get("cam_id") or "")
            if not cam_id:
                continue
            started = int(cam.get("started_at_ms") or 0)
            flash = int(cam.get("flash_at_ms") or 0)
            stopped = int(cam.get("stopped_at_ms") or 0)
            events.extend(self._anchor_events(session_id, cam_id, cam))
            files = _video_files(folder, session_id, cam_id)
            if not files:
                missed.append(cam_id)
                continue
            reports = [await _inspect_video(path, cam_id) for path in files]
            recordings.append(CameraRecording(
                cam_id=cam_id,
                device_key=str(cam.get("device_id") or ""),
                label=str(cam.get("browser_label") or ""),
                mime=str(cam.get("mime") or ""),
                files=files,
                started_at_ms=started,
                flash_at_ms=flash or started,
                stopped_at_ms=stopped,
                reports=reports,
            ))
        return CameraStopResult(
            session_id=session_id, recordings=recordings, events=events, missed=missed,
        )

    @staticmethod
    def _anchor_events(session_id: str, cam_id: str, cam: dict) -> list[CameraEvent]:
        events: list[CameraEvent] = []
        for kind in ("started", "stopped", "track_ended", "write_error"):
            at_ms = int(cam.get(f"{kind}_at_ms") or 0)
            if not at_ms:
                continue
            events.append(CameraEvent(
                key=f"{session_id}__{cam_id}__{at_ms}__anchor",
                sessionId=session_id,
                camId=cam_id,
                type=kind,
                atMs=at_ms,
                detail="recovered from camera anchors" if kind in ("track_ended", "write_error") else None,
            ))
        return events

    # -- finalize wait -----------------------------------------------------

    async def _wait_finalize(self, session_id: str) -> bool:
        deadline = time.monotonic() + FINALIZE_WAIT_S
        while True:
            if _finalize_settled(session_id):
                return True
            if time.monotonic() >= deadline:
                logger.error("finalize for %s did not settle in %.0fs", session_id, FINALIZE_WAIT_S)
                return False
            await asyncio.sleep(0.2)

    # -- records and progress ---------------------------------------------

    def _path(self, session_id: str) -> Path:
        return self.rt.state_root() / "sessions" / f"{session_id}.post.json"

    def _blank_record(self, session_id: str) -> dict:
        now = int(time.time() * 1000)
        return {
            "session_id": session_id,
            "created_at_ms": now,
            "updated_at_ms": now,
            "zip_path": "",
            "steps": {
                name: {"status": _PENDING, "started_at_ms": None,
                       "ended_at_ms": None, "detail": None}
                for name in STEP_NAMES
            },
        }

    def record(self, session_id: str) -> dict | None:
        return self._existing_record(session_id)

    def _existing_record(self, session_id: str) -> dict | None:
        data = read_json(self._path(session_id), None)
        if not isinstance(data, dict) or not isinstance(data.get("steps"), dict):
            return None
        return data

    def _ensure_record(self, session_id: str) -> dict:
        record = self._existing_record(session_id)
        if record is None:
            record = self._blank_record(session_id)
            self._write_record(session_id, record)
        return record

    def _write_record(self, session_id: str, record: dict) -> None:
        record["updated_at_ms"] = int(time.time() * 1000)
        atomic_write_json(self._path(session_id), record)

    async def _set_step(
        self,
        session_id: str,
        name: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        record = self._existing_record(session_id) or self._ensure_record(session_id)
        step = record["steps"].setdefault(
            name, {"status": _PENDING, "started_at_ms": None, "ended_at_ms": None, "detail": None}
        )
        now = int(time.time() * 1000)
        if status == _RUNNING:
            step["started_at_ms"] = now
            step["ended_at_ms"] = None
        else:
            if step.get("started_at_ms") is None:
                step["started_at_ms"] = now
            step["ended_at_ms"] = now
        step["status"] = status
        step["detail"] = detail
        self._write_record(session_id, record)
        await self._publish(session_id)

    async def _publish(self, session_id: str) -> None:
        hub = getattr(self.rt, "hub", None)
        publisher = getattr(hub, "finalize_changed", None) if hub is not None else None
        if publisher is None:
            return
        try:
            result = publisher(session_id)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.debug("finalize publish failed: %s", exc)

    # -- helpers -----------------------------------------------------------

    def _session_folder(self, session_id: str) -> Path | None:
        folders = _session_folders(session_id)
        return folders[0] if folders else None


def _finalize_settled(session_id: str) -> bool:
    """Whether FinalizeJob has reached a terminal record for this session."""
    if session_manager.session_id == session_id:
        task = getattr(session_manager, "_finalize_task", None)
        if task is not None and not task.done():
            return False
        snapshot = session_manager.finalize_snapshot
        if snapshot and snapshot.get("session_id") == session_id:
            return _steps_terminal(snapshot.get("steps"))
    ledger = session_manager.get_recovery_session(session_id) or {}
    if ledger.get("terminal") or ledger.get("state") == "IDLE":
        return True
    return _steps_terminal((ledger.get("finalize") or {}).get("steps"))


def _steps_terminal(steps) -> bool:
    steps = steps or []
    return bool(steps) and all(
        (s.get("state") or s.get("status")) in _TERMINAL for s in steps
    )


def finalize_steps(session_id: str) -> list[dict]:
    """FinalizeJob's steps (live, else from the ledger) as ``FinalizeStep`` dicts."""
    snapshot = None
    if session_manager.session_id == session_id:
        snapshot = session_manager.finalize_snapshot
    if snapshot is None:
        snapshot = (session_manager.get_recovery_session(session_id) or {}).get("finalize")
    if not snapshot:
        return []
    return [
        {
            "name": step.get("step"),
            "status": step.get("state", _PENDING),
            "started_at_ms": step.get("started_ms"),
            "ended_at_ms": step.get("ended_ms"),
            "detail": step.get("detail") or None,
        }
        for step in snapshot.get("steps") or []
        if step.get("step")
    ]


def session_progress(post: "PostSessionJob | None", session_id: str) -> dict | None:
    """``FinalizeProgress`` payload: finalize steps followed by cameras and package."""
    steps = finalize_steps(session_id)
    record = post.record(session_id) if post is not None else None
    if record:
        for name in STEP_NAMES:
            step = (record.get("steps") or {}).get(name)
            if not step:
                continue
            steps.append({
                "name": name,
                "status": step.get("status", _PENDING),
                "started_at_ms": step.get("started_at_ms"),
                "ended_at_ms": step.get("ended_at_ms"),
                "detail": step.get("detail"),
            })
    if not steps:
        return None
    return {"session_id": session_id, "steps": steps}


def _part_index(path: Path) -> int:
    name = path.name.split("_video_sync", 1)[-1]
    if name.startswith("_part"):
        try:
            return int(name[len("_part"):].split(".", 1)[0])
        except ValueError:
            return 999
    return 0


def _video_files(folder: Path, session_id: str, cam_id: str) -> list[Path]:
    prefix = f"{session_id}_{cam_id}_video_sync"
    return sorted(
        (p for p in folder.glob(f"{prefix}*.webm") if p.is_file()),
        key=_part_index,
    )


def _count_ebml_headers(path: Path) -> int:
    """Number of EBML magic bytes in the file (one per MediaRecorder run)."""
    count = 0
    tail = b""
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            data = tail + chunk
            count += data.count(_EBML_MAGIC)
            tail = data[-(len(_EBML_MAGIC) - 1):]
    return count


async def _ffprobe_duration_ms(path: Path) -> int | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            FFPROBE, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except (OSError, asyncio.TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    try:
        seconds = float(out.decode("utf-8", "replace").strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return int(round(seconds * 1000))


async def _remux_in_place(path: Path) -> bool:
    """``ffmpeg -i in -c copy`` to a tmp file, then replace (spec §4 restart path)."""
    tmp = path.with_name(path.name + ".remux.tmp")
    try:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-y", "-v", "error", "-i", str(path), "-c", "copy", str(tmp),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            return False
        os.replace(tmp, path)
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


async def _inspect_video(path: Path, cam_id: str) -> VideoFileReport:
    """Probe one recovered file; remux when it has no duration, as the spec requires."""
    size = path.stat().st_size
    headers = _count_ebml_headers(path)
    duration = await _ffprobe_duration_ms(path)
    ok = True
    error: str | None = None
    if duration is None:
        if await _remux_in_place(path):
            size = path.stat().st_size
            headers = _count_ebml_headers(path)
            duration = await _ffprobe_duration_ms(path)
        if duration is None:
            ok = False
            error = "ffmpeg could not remux the file (no readable duration); raw bytes kept"
    return VideoFileReport(
        path=f"videos/{path.name}",
        camId=cam_id,
        ok=ok,
        bytesWritten=size,
        durationMs=duration,
        ebmlHeaders=headers,
        error=error,
    )