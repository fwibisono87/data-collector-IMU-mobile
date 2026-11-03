"""Integration: calibration C1 (simultaneous reference) and C2 (drift), spec §7.

C1: a reference set from 5 fake cameras, max |skew_ms| ≤ 1 frame + 10 ms
(33.3 + 10 = 43.3 ms).
C2: ``set_fake_offset(cam1, 20, 0)`` → MOVED with dx ≈ 20 ± 2 px; offset back
to 0 → OK.

The fake source (testsrc2) is animated — a ~0.35 s vertical scroll period and
a slowly oscillating score — so verdicts are polled until they settle instead
of asserted on the first compare. The dashboard polls the same way.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from master_backend.app.api.schema import CameraSelectionItem
from master_backend.app.camera import discovery

pytestmark = pytest.mark.slow

MAX_SKEW_MS = 33.3 + 10.0  # one frame at 30 fps + 10 ms slack


@pytest.fixture()
def five_live(service, monkeypatch):
    monkeypatch.setenv("IMU_FAKE_CAMERAS", "5")
    discovery.invalidate_cache()
    service.set_selection([CameraSelectionItem(device_key=f"fake:{i}")
                           for i in range(5)])
    assert service.wait_live(timeout=12), "5 fake cameras did not go live"
    return service


def test_c1_reference_skew_within_one_frame(five_live):
    ref = asyncio.run(five_live.capture_reference("c1"))
    assert len(ref.cameras) == 5
    skews = [abs(c.skew_ms) for c in ref.cameras]
    assert max(skews) <= MAX_SKEW_MS, skews
    # Every camera has a real still with parsed geometry and a thumb URL.
    for cam in ref.cameras:
        assert cam.width == 1280 and cam.height == 720
        assert cam.file.endswith(".jpg")
        assert ref.thumb_urls[cam.device_key].endswith(".jpg")


def test_c2_offset_is_moved_and_recovers(five_live):
    ref = asyncio.run(five_live.capture_reference("c2"))
    five_live.activate_reference(ref.ref_id)

    result = _wait_for(five_live, lambda r: _all_verdicts(r) == {"OK"})
    assert _all_verdicts(result) == {"OK"}, _detail(result)

    # Shift cam1's crop window by +20 px → MOVED with dx ≈ +20, others OK.
    five_live.set_fake_offset("cam1", 20, 0)
    assert five_live.wait_live(["cam1"], timeout=10)

    def moved(r):
        cam1 = _by_cam(r)["cam1"]
        others_ok = all(c.verdict == "OK"
                        for c in r.cameras if c.cam_id != "cam1")
        return (cam1.verdict == "MOVED" and abs(cam1.dx_px - 20.0) <= 2.0
                and abs(cam1.dy_px) <= 2.0 and others_ok)

    result = _wait_for(five_live, moved)
    cam1 = _by_cam(result)["cam1"]
    assert cam1.verdict == "MOVED", _detail(result)
    assert abs(cam1.dx_px - 20.0) <= 2.0, _detail(result)
    assert abs(cam1.dy_px) <= 2.0, _detail(result)
    assert all(c.verdict == "OK" for c in result.cameras
               if c.cam_id != "cam1"), _detail(result)

    # Back to 0 → OK again.
    five_live.set_fake_offset("cam1", 0, 0)
    assert five_live.wait_live(["cam1"], timeout=10)
    result = _wait_for(five_live, lambda r: _all_verdicts(r) == {"OK"})
    cam1 = _by_cam(result)["cam1"]
    assert cam1.verdict == "OK", _detail(result)
    assert abs(cam1.dx_px) <= 2.0, _detail(result)


def _wait_for(service, predicate, timeout: float = 10.0):
    """Poll compare until ``predicate`` holds; return the last result either
    way so failing asserts show real verdicts."""
    deadline = time.monotonic() + timeout
    while True:
        service.meter.invalidate()  # bypass the 0.5 s compare cache
        result = asyncio.run(service.compare("active"))
        if predicate(result) or time.monotonic() >= deadline:
            return result
        time.sleep(0.2)


def _by_cam(result):
    return {c.cam_id: c for c in result.cameras}


def _all_verdicts(result):
    return {c.verdict for c in result.cameras}


def _detail(result):
    return [(c.cam_id, c.verdict, round(c.dx_px, 1), round(c.dy_px, 1),
             round(c.score, 3)) for c in result.cameras]
