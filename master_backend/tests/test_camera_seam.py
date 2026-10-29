"""The camera seam: the fake service must behave like the contract says.

The session core (L4) and the camera service (L5) are built against
``master_backend/app/camera/interface.py`` in parallel. These tests pin that the fake
implementation is a faithful stand-in — same protocol, same file names, same
``cameras.json`` anchors, same multi-part story — so L4's tests keep meaning something
when L5 swaps the fake for ffmpeg.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from master_backend.app.api.schema import (  # noqa: E402
    Camera,
    CameraDevice,
    CameraSelectionItem,
    Issue,
)
from master_backend.app.camera import (  # noqa: E402
    CameraError,
    CameraEvent,
    CameraService,
    FakeCameraService,
    VideoFileReport,
)
from master_backend.app.camera.fake import PLACEHOLDER_BYTES  # noqa: E402

SESSION_ID = "1793351700000"


class Clock:
    """Deterministic seconds clock; ``advance`` moves it forward."""

    def __init__(self, at: float = 1_793_351_700.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += seconds


def select_all(service: FakeCameraService) -> None:
    service.set_selection(
        [CameraSelectionItem(device_key=service.list_devices()[i].device_key)
         for i in range(len(service.list_devices()))]
    )


def session_folder(rig) -> Path:
    return rig.ssd_path / "Data_Riset_IMU" / "S01_walk"


def read_cameras(rig) -> dict:
    path = session_folder(rig) / f"{SESSION_ID}_cameras.json"
    assert path.exists(), f"no anchors written at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_fake_satisfies_the_protocol_structurally():
    service = FakeCameraService(2)
    assert isinstance(service, CameraService)
    # Every method the session core may call exists with the documented signature.
    assert service.list_devices() and service.cameras() == []
    assert service.readiness() == ([Issue(code="no_camera_selected",
                                          message="Select at least one camera "
                                                  "before starting.")], [])
    assert service.is_recording() is False
    assert callable(service.start_recording) and callable(service.stop_recording)
    assert callable(service.close)


def test_devices_and_slots_are_deterministic():
    service = FakeCameraService(3)
    assert [d.device_key for d in service.list_devices()] == ["fake:0", "fake:1", "fake:2"]
    assert all(isinstance(d, CameraDevice) and d.backend == "fake"
               for d in service.list_devices())

    cams = service.set_selection([
        CameraSelectionItem(device_key="fake:2"),
        CameraSelectionItem(device_key="fake:0", cam_id="cam4"),
    ])
    assert [c.cam_id for c in cams] == ["cam1", "cam4"]
    assert cams[0].device_key == "fake:2"
    assert all(isinstance(c, Camera) and c.state == "live" for c in cams)
    assert (cams[0].width, cams[0].height, cams[0].fps) == (1280, 720, 30.0)
    # Cached state: same call, same answer, no re-enumeration.
    assert service.cameras() == cams


def test_readiness_reports_selection_blocker_then_clears():
    service = FakeCameraService(2)
    blockers, warnings = service.readiness()
    assert [b.code for b in blockers] == ["no_camera_selected"]
    assert warnings == []

    select_all(service)
    assert service.readiness() == ([], [])


def test_start_recording_requires_a_live_selected_camera(rig):
    service = FakeCameraService(2)
    with pytest.raises(CameraError):
        asyncio.run(service.start_recording(SESSION_ID, session_folder(rig)))
    assert not service.is_recording()


def test_stop_recording_requires_a_running_session(rig):
    service = FakeCameraService(1)
    with pytest.raises(CameraError):
        asyncio.run(service.stop_recording(SESSION_ID))


def test_start_stop_cycle_writes_videos_and_camera_anchors(rig):
    clock = Clock()
    service = FakeCameraService(2, clock=clock)
    select_all(service)

    folder = session_folder(rig)
    asyncio.run(service.start_recording(SESSION_ID, folder))
    assert service.is_recording()
    assert [c.recording for c in service.cameras()] == [True, True]

    started_ms = int(clock.at * 1000)
    for cam_id in ("cam1", "cam2"):
        path = folder / f"{SESSION_ID}_{cam_id}_video_sync.webm"
        assert path.read_bytes() == PLACEHOLDER_BYTES
        assert path.read_bytes() == b"FAKEWEBM"

    clock.advance(6.0)
    result = asyncio.run(service.stop_recording(SESSION_ID))
    stopped_ms = started_ms + 6_000

    assert not service.is_recording()
    assert result.session_id == SESSION_ID
    assert [r.cam_id for r in result.recordings] == ["cam1", "cam2"]
    assert result.missed == []
    for recording in result.recordings:
        assert recording.device_key in {"fake:0", "fake:1"}
        assert recording.mime == "video/webm;codecs=vp9"
        assert recording.started_at_ms == started_ms
        assert recording.flash_at_ms == started_ms
        assert recording.stopped_at_ms == stopped_ms
        assert [p.name for p in recording.files] == [
            f"{SESSION_ID}_{recording.cam_id}_video_sync.webm"
        ]
        assert all(p.is_absolute() for p in recording.files)
        [report] = recording.reports
        assert isinstance(report, VideoFileReport)
        assert report.path == f"videos/{recording.files[0].name}"
        assert report.camId == recording.cam_id
        assert report.ok is True
        assert report.ebmlHeaders == 1
        assert report.durationMs == 6_000
        assert report.bytesWritten == len(PLACEHOLDER_BYTES)

    # The anchor file is exactly today's shape, one record per camera.
    data = read_cameras(rig)
    assert data["session_id"] == SESSION_ID
    records = {record["cam_id"]: record for record in data["cameras"]}
    assert set(records) == {"cam1", "cam2"}
    assert records["cam1"]["device_id"] == "fake:0"
    assert records["cam2"]["device_id"] == "fake:1"
    for cam_id, record in records.items():
        assert record["session_id"] == SESSION_ID
        assert record["browser_label"] == f"Fake Camera {int(record['device_id'][-1])}"
        assert record["mime"] == "video/webm;codecs=vp9"
        assert record["started_at_ms"] == started_ms
        assert record["flash_at_ms"] == started_ms
        assert record["stopped_at_ms"] == stopped_ms
        assert cam_id in {"cam1", "cam2"}

    # Event stream: started and stopped per camera, ordered per camera.
    kinds = [(e.camId, e.type) for e in result.events]
    assert kinds == [
        ("cam1", "started"), ("cam2", "started"),
        ("cam1", "stopped"), ("cam2", "stopped"),
    ]
    started_event = result.events[0]
    assert isinstance(started_event, CameraEvent)
    assert started_event.key == "fake:0"
    assert started_event.sessionId == SESSION_ID
    assert started_event.atMs == started_ms


def test_camera_failure_produces_part2_and_track_ended(rig):
    clock = Clock()
    service = FakeCameraService(2, clock=clock)
    select_all(service)
    folder = session_folder(rig)

    asyncio.run(service.start_recording(SESSION_ID, folder))
    clock.advance(2.0)
    service.fail("cam2")
    clock.advance(4.0)
    result = asyncio.run(service.stop_recording(SESSION_ID))

    recordings = {recording.cam_id: recording for recording in result.recordings}
    assert len(recordings["cam2"].files) == 2
    assert [p.name for p in recordings["cam2"].files] == [
        f"{SESSION_ID}_cam2_video_sync.webm",
        f"{SESSION_ID}_cam2_video_sync_part2.webm",
    ]
    assert [report.path for report in recordings["cam2"].reports] == [
        f"videos/{SESSION_ID}_cam2_video_sync.webm",
        f"videos/{SESSION_ID}_cam2_video_sync_part2.webm",
    ]
    assert all(report.ok for report in recordings["cam2"].reports)

    # cam1 is untouched by cam2's dropout.
    assert len(recordings["cam1"].files) == 1

    track_ended = [e for e in result.events if e.type == "track_ended"]
    assert len(track_ended) == 1
    assert track_ended[0].camId == "cam2"
    assert track_ended[0].key == "fake:1"
    assert track_ended[0].atMs == int(clock.at * 1000) - 4_000

    # Both files exist on disk and both anchors are still written.
    assert (folder / f"{SESSION_ID}_cam2_video_sync_part2.webm").exists()
    data = read_cameras(rig)
    assert {record["cam_id"] for record in data["cameras"]} == {"cam1", "cam2"}
    assert all(record["stopped_at_ms"] for record in data["cameras"])


def test_fail_rejects_a_camera_that_is_not_selected():
    service = FakeCameraService(2)
    with pytest.raises(CameraError):
        service.fail("cam1")
    select_all(service)
    with pytest.raises(CameraError):
        service.fail("cam5")


def test_close_is_idempotent_and_aborts_a_recording(rig):
    service = FakeCameraService(2)
    select_all(service)
    asyncio.run(service.start_recording(SESSION_ID, session_folder(rig)))
    asyncio.run(service.close())
    asyncio.run(service.close())
    assert not service.is_recording()
    with pytest.raises(CameraError):
        asyncio.run(service.start_recording(SESSION_ID, session_folder(rig)))


def test_start_recording_is_fast_enough_for_the_seam(rig, tmp_path):
    """The contract promises start_recording returns within 2 s (SPEC §6.4)."""
    import time

    service = FakeCameraService(5)
    select_all(service)
    folder = tmp_path / "speed"
    started = time.monotonic()
    asyncio.run(service.start_recording(SESSION_ID, folder))
    elapsed = time.monotonic() - started
    asyncio.run(service.close())
    assert elapsed < 2.0


def test_camera_event_rejects_an_unknown_type():
    with pytest.raises(ValidationError):
        CameraEvent(key="fake:0", sessionId=SESSION_ID, camId="cam1",
                    type="exploded", atMs=1)