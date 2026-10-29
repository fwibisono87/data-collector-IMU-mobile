"""Integration: capture with fake cameras (spec §7, fast half).

Select → live within 5 s; the latest frame is a full-resolution decodable
JPEG; killing the capture ffmpeg reconnects automatically and frames flow
again from the *new* process.
"""
from __future__ import annotations

import io
import time

from PIL import Image

from master_backend.app.api.schema import CameraSelectionItem


def test_select_goes_live_within_5s(service):
    started = time.monotonic()
    cameras = service.set_selection([
        CameraSelectionItem(device_key="fake:0"),
        CameraSelectionItem(device_key="fake:1"),
    ])
    assert [c.cam_id for c in cameras] == ["cam1", "cam2"]
    assert service.wait_live(timeout=5), "not live within 5 s"
    assert time.monotonic() - started < 5.0
    for camera in service.cameras():
        assert camera.state == "live"
        assert (camera.width, camera.height) == (1280, 720)
        assert camera.fps is None or camera.fps > 0


def test_latest_frame_is_decodable(selected):
    deadline = time.monotonic() + 3
    frame = selected.latest_frame("cam1")
    while frame is None and time.monotonic() < deadline:
        time.sleep(0.1)
        frame = selected.latest_frame("cam1")
    assert frame is not None
    with Image.open(io.BytesIO(frame.data)) as image:
        assert image.size == (1280, 720)
    assert selected.latest_frame("nope") is None


def test_reconnect_after_capture_kill(selected):
    worker = selected.worker("cam1")
    old_pid = worker.process.pid
    before = worker.frames_received
    worker.process.kill()  # SIGKILL: no clean shutdown, monitor must notice

    deadline = time.monotonic() + 10
    reconnected = False
    while time.monotonic() < deadline:
        proc = worker.process
        if (proc is not None and proc.pid != old_pid
                and worker.state == "live"):
            reconnected = True
            break
        time.sleep(0.1)
    assert reconnected, f"cam1 did not reconnect within 10 s (state={worker.state})"

    deadline = time.monotonic() + 5
    while (worker.frames_received < before + 10
           and time.monotonic() < deadline):
        time.sleep(0.1)
    assert worker.frames_received >= before + 10, "frames stopped flowing"
