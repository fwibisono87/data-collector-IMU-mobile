"""In-memory ``CameraService`` for the session core's tests.

It behaves like the real service at the seam: deterministic devices (``fake:0`` …
``fake:n-1``), the same file names, the same ``cameras.json`` anchors written through
``master_backend.app.cameras.cameras_mark``, and a stop result shaped like a real one.
No subprocesses, no frames — each file is the placeholder ``b"FAKEWEBM"``.

Inject ``clock`` (seconds, like ``time.time``) to make anchors and durations
deterministic::

    svc = FakeCameraService(2, clock=lambda: 1000.0)

``fail(cam_id)`` simulates a camera that dropped during the session and came back: at
stop that camera produces a ``_part2`` file and a ``track_ended`` event, which is the
multi-part case the packaging step must handle.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from .. import cameras as cameras_module
from ..api.schema import Camera, CameraDevice, CameraSelectionItem, Issue
from .interface import (
    CameraError,
    CameraEvent,
    CameraRecording,
    CameraStopResult,
    VideoFileReport,
)

PLACEHOLDER_BYTES = b"FAKEWEBM"
MIME = "video/webm;codecs=vp9"
_WIDTH = 1280
_HEIGHT = 720
_FPS = 30.0
_MAX_CAMERAS = 5


class FakeCameraService:
    """Implements :class:`~master_backend.app.camera.interface.CameraService` in memory."""

    def __init__(self, n: int = 2, *, clock: Callable[[], float] | None = None) -> None:
        if n < 0:
            raise ValueError("n must be >= 0")
        self._clock = clock or time.time
        self._devices = [
            CameraDevice(device_key=f"fake:{i}", label=f"Fake Camera {i}", backend="fake")
            for i in range(n)
        ]
        self._selection: list[tuple[str, str]] = []  # (device_key, cam_id), slot order
        self._recording = False
        self._closed = False
        self._session_id: str | None = None
        self._folder: Path | None = None
        self._started_at_ms = 0
        self._files: dict[str, list[Path]] = {}
        self._events: list[CameraEvent] = []
        self._failed: dict[str, int] = {}  # cam_id -> at_ms of the simulated dropout

    # -- internals ---------------------------------------------------------
    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _device(self, device_key: str) -> CameraDevice:
        for device in self._devices:
            if device.device_key == device_key:
                return device
        raise CameraError(f"unknown camera device {device_key!r}")

    def _camera(self, device_key: str, cam_id: str) -> Camera:
        device = self._device(device_key)
        return Camera(
            cam_id=cam_id,
            device_key=device_key,
            label=device.label,
            state="live",
            recording=self._recording,
            width=_WIDTH,
            height=_HEIGHT,
            fps=_FPS,
            dropped_frames=0,
            error=None,
            drift=None,
        )

    async def _mark(self, camera: Camera, event: str, ts_ms: int) -> None:
        """Write one anchor exactly the way the real service must."""
        await cameras_module.cameras_mark(
            self._session_id or "",
            {
                "cam_id": camera.cam_id,
                "event": event,
                "ts_ms": ts_ms,
                "device_id": camera.device_key,
                "label": camera.label,
                "mime": MIME,
            },
        )

    def _event(self, camera: Camera, kind: str, at_ms: int,
               detail: str | None = None) -> CameraEvent:
        event = CameraEvent(
            key=camera.device_key,
            sessionId=self._session_id or "",
            camId=camera.cam_id,
            type=kind,
            atMs=at_ms,
            detail=detail,
        )
        self._events.append(event)
        return event

    # -- CameraService -----------------------------------------------------
    def list_devices(self) -> list[CameraDevice]:
        return list(self._devices)

    def cameras(self) -> list[Camera]:
        return [self._camera(device_key, cam_id) for device_key, cam_id in self._selection]

    def set_selection(self, items: list[CameraSelectionItem]) -> list[Camera]:
        if self._recording:
            raise CameraError("cannot change the camera selection while recording")
        if len(items) > _MAX_CAMERAS:
            raise CameraError(f"at most {_MAX_CAMERAS} cameras can be selected")

        known = {device.device_key for device in self._devices}
        used: set[str] = set()
        selection: list[tuple[str, str]] = []
        for item in items:
            if item.device_key not in known:
                raise CameraError(f"unknown camera device {item.device_key!r}")
            if item.cam_id is not None:
                cam_id = item.cam_id
            else:
                index = 1
                while f"cam{index}" in used:
                    index += 1
                cam_id = f"cam{index}"
            if cam_id in used:
                raise CameraError(f"duplicate camera slot {cam_id!r}")
            used.add(cam_id)
            selection.append((item.device_key, cam_id))

        self._selection = selection
        return self.cameras()

    def readiness(self) -> tuple[list[Issue], list[Issue]]:
        blockers: list[Issue] = []
        if not self._selection:
            blockers.append(Issue(
                code="no_camera_selected",
                message="Select at least one camera before starting.",
            ))
        # Every fake camera is live, but keep the check explicit: it documents the
        # contract for the real implementation and catches a future state model.
        for camera in self.cameras():
            if camera.state != "live":
                blockers.append(Issue(
                    code="camera_not_live",
                    message=f"{camera.cam_id} ({camera.label}) is not live.",
                    subject=camera.cam_id,
                ))
        return blockers, []

    async def start_recording(self, session_id: str, folder: Path) -> None:
        if self._closed:
            raise CameraError("camera service is closed")
        if self._recording:
            raise CameraError(f"already recording session {self._session_id}")

        selected = [camera for camera in self.cameras() if camera.state == "live"]
        if not selected:
            raise CameraError("no selected camera is live")

        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)

        self._session_id = session_id
        self._folder = folder
        self._files = {}
        self._events = []
        self._failed = {}
        self._recording = True
        self._started_at_ms = self._now_ms()

        for camera in selected:
            path = folder / f"{session_id}_{camera.cam_id}_video_sync.webm"
            path.write_bytes(PLACEHOLDER_BYTES)
            self._files[camera.cam_id] = [path]
            self._event(camera, "started", self._started_at_ms)
            # The anchors that keep <sid>_cameras.json identical to today.
            await self._mark(camera, "started", self._started_at_ms)
            await self._mark(camera, "flash", self._started_at_ms)

    async def stop_recording(self, session_id: str) -> CameraStopResult:
        if not self._recording:
            raise CameraError("not recording")
        if session_id != self._session_id:
            raise CameraError(
                f"not recording {session_id} (recording {self._session_id})"
            )
        assert self._folder is not None  # set by start_recording

        stopped_at_ms = self._now_ms()
        recordings: list[CameraRecording] = []
        for camera in self.cameras():
            files = list(self._files.get(camera.cam_id, []))
            if not files:
                continue  # reported through missed below

            if camera.cam_id in self._failed:
                part2 = (
                    self._folder
                    / f"{session_id}_{camera.cam_id}_video_sync_part2.webm"
                )
                part2.write_bytes(PLACEHOLDER_BYTES)
                files.append(part2)
                self._event(
                    camera, "track_ended", self._failed[camera.cam_id],
                    detail="simulated camera dropout",
                )
                self._files[camera.cam_id] = list(files)

            duration_ms = stopped_at_ms - self._started_at_ms
            reports = [
                VideoFileReport(
                    path=f"videos/{path.name}",
                    camId=camera.cam_id,
                    ok=True,
                    bytesWritten=len(PLACEHOLDER_BYTES),
                    durationMs=max(0, duration_ms),
                    ebmlHeaders=1,
                )
                for path in files
            ]
            self._event(camera, "stopped", stopped_at_ms)
            await self._mark(camera, "stopped", stopped_at_ms)
            recordings.append(CameraRecording(
                cam_id=camera.cam_id,
                device_key=camera.device_key,
                label=camera.label,
                mime=MIME,
                files=files,
                started_at_ms=self._started_at_ms,
                flash_at_ms=self._started_at_ms,
                stopped_at_ms=stopped_at_ms,
                reports=reports,
            ))

        missed = [camera.cam_id for camera in self.cameras()
                  if not self._files.get(camera.cam_id)]

        self._recording = False
        return CameraStopResult(
            session_id=session_id,
            recordings=recordings,
            events=list(self._events),
            missed=missed,
        )

    def is_recording(self) -> bool:
        return self._recording

    async def close(self) -> None:
        self._closed = True
        self._recording = False

    # -- test hooks --------------------------------------------------------
    def fail(self, cam_id: str) -> None:
        """Simulate a camera that drops mid-session and comes back.

        At ``stop_recording`` that camera produces a second file
        (``..._video_sync_part2.webm``) and a ``track_ended`` event, as the real
        service does after a capture process dies.
        """
        cam_ids = {camera.cam_id for camera in self.cameras()}
        if cam_id not in cam_ids:
            raise CameraError(f"{cam_id!r} is not a selected camera")
        self._failed[cam_id] = self._now_ms()