"""Camera service seam: the contract the session core (L4) codes against and the
real camera service (L5) implements.

Nothing in this package starts a process or opens a device. ``interface.py`` holds the
types and the ``CameraService`` protocol; ``fake.py`` is an in-memory implementation
with the same observable behaviour, used by the session-core tests while the real
implementation does not exist yet.

The seam deliberately mirrors today's artefacts:

* video is one WebM per camera and recording run, named exactly
  ``<sid>_<camId>_video_sync[_partN].webm``;
* the wall-clock anchors that make video alignable with the IMU data are written by
  ``master_backend.app.cameras.cameras_mark``, so ``<sid>_cameras.json`` keeps its
  current shape (OUTPUT_CONTRACT.md §9);
* a camera failure never aborts a session: it becomes a ``track_ended`` event and a
  new ``_partN`` file, or a ``missed`` camera in the stop result.
"""

from .fake import FakeCameraService
from .interface import (
    CameraError,
    CameraEvent,
    CameraRecording,
    CameraService,
    CameraStopResult,
    VideoFileReport,
)

__all__ = [
    "CameraError",
    "CameraEvent",
    "CameraRecording",
    "CameraService",
    "CameraStopResult",
    "FakeCameraService",
    "VideoFileReport",
]


def __getattr__(name: str):
    """Lazily import the real service (pulls in numpy/PIL) on first access.

    ``import master_backend.app.camera`` stays cheap for the session core and
    the contract tests, which only need the seam types above; L4's
    ``from master_backend.app.camera import create_service`` still works.
    """
    if name in ("create_service", "RealCameraService"):
        from . import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")