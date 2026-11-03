"""The camera stack must not starve the asyncio loop that serves the phones.

A 5-camera end-to-end run found every phone dropped with "keepalive ping timeout"
30 s after START: the JPEG splitter stepped through each frame's entropy data one
byte at a time in Python, holding the GIL for most of every second. These tests pin
the splitter's cost and the loop latency the phones actually see.
"""
from __future__ import annotations

import asyncio
import statistics
import subprocess
import time

import pytest

from master_backend.app.api.schema import CameraSelectionItem
from master_backend.app.camera import discovery
from master_backend.app.camera.jpeg import JpegFrameSplitter


def _mjpeg_sample(seconds: int = 2) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=size=1280x720:rate=30", "-t", str(seconds),
         "-c:v", "mjpeg", "-q:v", "4", "-f", "mjpeg", "pipe:1"],
        check=True, capture_output=True,
    ).stdout


def test_splitter_cost_per_720p_frame_is_small():
    data = _mjpeg_sample()
    splitter = JpegFrameSplitter()
    start = time.perf_counter()
    frames = 0
    for i in range(0, len(data), 65536):
        frames += len(splitter.feed(data[i:i + 65536]))
    per_frame_ms = (time.perf_counter() - start) / frames * 1000
    assert frames == 60
    # The byte-stepping version took ~2.3 ms/frame on the dev machine; C-speed find
    # takes ~0.06 ms. 0.5 ms leaves room for a loaded CI box.
    assert per_frame_ms < 0.5, f"{per_frame_ms:.2f} ms per frame"


@pytest.mark.slow
def test_event_loop_stays_responsive_while_five_cameras_record(service, ssd, monkeypatch):
    monkeypatch.setenv("IMU_FAKE_CAMERAS", "5")
    discovery.invalidate_cache()
    folder = ssd / "Data_Riset_IMU" / "S001_latency"
    folder.mkdir(parents=True, exist_ok=True)
    service.set_selection([CameraSelectionItem(device_key=f"fake:{i}") for i in range(5)])
    assert service.wait_live(timeout=10)

    sid = f"S_lat_{int(time.time())}"

    async def run() -> list[float]:
        await service.start_recording(sid, folder)
        lags: list[float] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            t = time.perf_counter()
            await asyncio.sleep(0.02)
            lags.append((time.perf_counter() - t - 0.02) * 1000)
        await service.stop_recording(sid)
        return lags

    lags = asyncio.run(run())
    p99 = statistics.quantiles(lags, n=100)[98]
    # A phone's keepalive is 20 s and its PONG timeout 8 s; the loop was stalling for
    # seconds. 100 ms p99 / 1 s max is far inside both and still catches a regression.
    assert p99 < 100, f"p99 loop lag {p99:.0f} ms"
    assert max(lags) < 1000, f"max loop lag {max(lags):.0f} ms"
