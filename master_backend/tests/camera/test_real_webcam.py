"""Optional real-webcam smoke test on /dev/video0 (spec §6b/§7).

Skips cleanly when the device is absent or busy (another lane may hold it):
a 3 s single-frame probe decides before anything is selected. When it runs it
does the full loop: discover → select → live → latest frame → 5 s recording →
playable file.
"""
from __future__ import annotations

import asyncio
import io
import subprocess
import time

import pytest
from PIL import Image

from master_backend.app.api.schema import CameraSelectionItem
from master_backend.app.camera import bins, discovery, webm

pytestmark = pytest.mark.slow

DEV = "/dev/video0"


def _probe_capturable() -> bool:
    try:
        proc = subprocess.run(
            [bins.ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
             "-f", "v4l2", "-i", DEV, "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, timeout=3)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.fixture()
def real_service(ssd, monkeypatch):
    if not _probe_capturable():
        pytest.skip(f"{DEV} absent or busy")
    monkeypatch.setenv("IMU_CAMERAS", "real")  # anything but "fake"
    discovery.invalidate_cache()
    from master_backend.app.camera.service import RealCameraService

    svc = RealCameraService(ssd_root=ssd)
    try:
        yield svc
    finally:
        asyncio.run(svc.close())


def test_real_webcam_discovery_select_record(real_service, ssd):
    devices = real_service.list_devices()
    real = [d for d in devices if d.backend == "v4l2"]
    assert real, f"no v4l2 device discovered: {devices}"
    target = next((d for d in real if DEV in d.device_key), real[0])

    real_service.set_selection([CameraSelectionItem(device_key=target.device_key)])
    assert real_service.wait_live(timeout=8), "real webcam did not go live"

    # ~3 s of preview: frames keep arriving and decode. A dim room makes the
    # webcam's auto-exposure drop to a few fps, so only require *some* frames.
    worker = real_service.worker("cam1")
    first = worker.frames_received
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        time.sleep(0.2)
    assert worker.frames_received > first + 5, "no frames from the real webcam"
    frame = real_service.latest_frame("cam1")
    assert frame is not None
    with Image.open(io.BytesIO(frame.data)) as image:
        assert image.width >= 320 and image.height >= 240

    # 5 s recording → one playable file.
    sid = f"S_real_{int(time.time())}"
    folder = ssd / "Data_Riset_IMU" / "S001_realcam"  # sorts before _pending_cameras
    folder.mkdir(parents=True, exist_ok=True)
    asyncio.run(real_service.start_recording(sid, folder))
    time.sleep(5)
    result = asyncio.run(real_service.stop_recording(sid))

    assert result.missed == []
    rec = result.recordings[0]
    path = rec.files[0]
    assert path.name == f"{sid}_cam1_video_sync.webm"
    duration = webm.probe_duration_ms(path)
    assert duration is not None, "recorded file is not playable (no duration)"
    assert 3000 <= duration <= 8000, duration
    assert webm.count_ebml_headers(path) == 1
    assert webm.has_cues(path)
    frames = webm.probe_frame_count(path)
    # Only require *some* frames: auto-exposure in a dim room can legitimately
    # drop the real webcam to a few fps (the file must just be playable).
    assert frames is not None and frames >= 5
