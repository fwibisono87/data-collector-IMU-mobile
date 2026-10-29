"""Types and protocol for the camera service.

The session core (lane L4) only ever talks to a ``CameraService``; the real
implementation (lane L5) drives ffmpeg, and ``fake.py`` stands in for it in tests.
Everything here is synchronous except ``start_recording``, ``stop_recording`` and
``close``.

Artifact obligations (frozen output contract, docs/OUTPUT_CONTRACT.md §9 and
docs/rebuild/SPEC.md §6.4):

* recording files are ``<sid>_<camId>_video_sync.webm``; every run after a camera
  dropout is a new file ``..._part2.webm``, ``..._part3.webm``, … in part order;
* anchors are written with
  ``master_backend.app.cameras.cameras_mark(session_id, body)`` where ``body`` is
  ``{"cam_id", "event", "ts_ms", "device_id": <device_key>, "label", "mime"}``. The
  five anchor events are ``started``, ``flash``, ``stopped``, plus the camera events
  ``track_ended`` and ``write_error`` that also land in ``cameras.json``;
* a camera that never produced a file is reported in ``missed`` — it must never abort
  the session or the camera step of finalize.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from ..api.schema import Camera, CameraDevice, CameraSelectionItem, Issue


class CameraError(RuntimeError):
    """A camera operation could not start or is not valid in the current state.

    Raised by ``start_recording`` when no selected camera is live, and by the other
    state-changing methods when they are called out of order. ``stop_recording`` does
    **not** raise for the failure of an individual camera.
    """


class CameraEvent(BaseModel):
    """One item of the ``camera_events`` array in the session's ``cameras.json``.

    Identical in shape to today's browser-built item (OUTPUT_CONTRACT.md §9):
    ``key`` is the stable device key, ``camId`` the slot at the time.
    """

    key: str
    sessionId: str
    camId: str
    type: Literal["started", "stopped", "track_ended", "write_error"]
    atMs: int
    detail: str | None = None


class VideoFileReport(BaseModel):
    """Finalize evidence for one video file.

    This is today's ``FinalizeResult`` plus ``path`` and ``camId``, as it appears in
    the session ZIP's ``video_report.json`` (OUTPUT_CONTRACT.md §9). ``path`` is
    ZIP-relative, e.g. ``videos/<sid>_cam1_video_sync.webm``. ``durationMs`` is ``None``
    when the duration could not be recovered; ``ok`` is false with ``error`` set when
    finalising failed (the raw bytes are still kept).
    """

    path: str
    camId: str
    ok: bool
    bytesWritten: int
    durationMs: int | None
    ebmlHeaders: int
    error: str | None = None


class CameraRecording(BaseModel):
    """One camera's result for one session: every file it produced, in part order.

    ``started_at_ms`` is the arrival time of the first frame that reached the encoder
    and ``stopped_at_ms`` the arrival time of the last one; ``flash_at_ms`` is kept for
    the frozen ``cameras.json`` shape and equals ``started_at_ms`` (there is no screen
    flash any more). ``files`` are absolute paths on disk; ``reports`` has one entry
    per file, in the same order.
    """

    cam_id: str
    device_key: str
    label: str
    mime: str
    files: list[Path]
    started_at_ms: int
    flash_at_ms: int
    stopped_at_ms: int
    reports: list[VideoFileReport]


class CameraStopResult(BaseModel):
    """Everything the session core needs to write ``cameras.json`` and the ZIP report.

    ``recordings`` has one entry per camera that produced at least one file;
    ``missed`` lists the cam_ids that were selected but produced no file at all.
    ``events`` carries every camera event for the session (started/stopped per camera
    plus any track_ended/write_error).
    """

    session_id: str
    recordings: list[CameraRecording]
    events: list[CameraEvent]
    missed: list[str]


@runtime_checkable
class CameraService(Protocol):
    """The interface the session core depends on.

    Implementations must be safe to call from the FastAPI event loop. Only
    ``start_recording``, ``stop_recording`` and ``close`` may do I/O; every other
    method returns cached state and must not block (no device enumeration, no
    ffprobe, no frame grabs).
    """

    def list_devices(self) -> list[CameraDevice]:
        """Enumerate the cameras the host currently exposes.

        Re-enumeration is expensive (a subprocess on Windows), so implementations are
        expected to cache and rate-limit it to at most once per second. Never raises:
        an unavailable backend returns an empty list.
        """
        ...

    def cameras(self) -> list[Camera]:
        """The current state of the *selected* camera slots, in slot order.

        Includes live preview dimensions/FPS, dropped frames, the last error and the
        latest calibration ``drift`` (``None`` when no reference is active). Never
        blocks and never raises.
        """
        ...

    def set_selection(self, items: list[CameraSelectionItem]) -> list[Camera]:
        """Replace the selected cameras and return the resulting slots.

        Slots ``cam1``..``cam5`` are assigned in list order unless the item names one.
        Not allowed while recording (``CameraError``); at most five items. Devices are
        opened as needed and the selection is persisted by the caller, keyed by the
        stable ``device_key``.
        """
        ...

    def readiness(self) -> tuple[list[Issue], list[Issue]]:
        """The camera half of the preflight check: ``(blockers, warnings)``.

        Blockers (START is refused): no camera selected; a selected camera that is not
        live. Warnings (START allowed, recorded in the session): drift against the
        active calibration reference with verdict CHECK, MOVED,
        RESOLUTION_CHANGED or MISSING. Never blocks.
        """
        ...

    async def start_recording(self, session_id: str, folder: Path) -> None:
        """Start one encoder per selected live camera, writing into ``folder``.

        Obligations:

        * each file is ``<sid>_<camId>_video_sync.webm``; files are created in
          ``folder`` (the session folder), never in a temp directory;
        * the ``started`` and ``flash`` anchors are recorded through
          ``master_backend.app.cameras.cameras_mark(session_id, body)`` with
          ``body = {"cam_id", "event", "ts_ms", "device_id": <device_key>, "label",
          "mime"}``. ``flash_at_ms`` equals the first written frame's arrival time
          (SPEC §6.4); the anchor for it may be written before or after the first
          frame but must carry that timestamp;
        * raises ``CameraError`` when no selected camera is live — the session core
          turns that into a preflight blocker, never a half-started recording;
        * returns within 2 s: the encoders are started in the background and the first
          frames are awaited with a short timeout, not for the whole recording.
        """
        ...

    async def stop_recording(self, session_id: str) -> CameraStopResult:
        """Stop every encoder and finalise every file of ``session_id``.

        Obligations:

        * ask each encoder to finish so ffmpeg writes Duration and Cues, then probe
          every file; a file that is not seekable is remuxed
          (``ffmpeg -c copy``) and reported with the probe result;
        * write the ``stopped`` anchor for every camera through
          ``cameras_mark`` exactly like ``start_recording``;
        * never raise because one camera failed: that camera either appears in
          ``missed`` (no file at all) or gets a ``write_error`` event and an
          ``ok: false`` report; the other cameras still finalise;
        * only raises ``CameraError`` when called while not recording.
        """
        ...

    def is_recording(self) -> bool:
        """Whether a recording run is in progress. Cached; never blocks."""
        ...

    async def close(self) -> None:
        """Release devices and child processes. Safe to call twice.

        Called on backend shutdown. A recording in progress is aborted: files stay on
        disk for the resume path, but no anchors are fabricated.
        """
        ...