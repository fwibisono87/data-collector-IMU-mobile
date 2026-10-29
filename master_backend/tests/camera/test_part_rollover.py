"""Integration: part rollover after a camera dropout (spec §7).

Kill one capture process mid-recording, wait for it to come back, then stop.
Expect ``_part2``, a ``track_ended`` event, and both files listed in order.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from master_backend.app.api.schema import CameraSelectionItem
from master_backend.app.camera import webm

pytestmark = pytest.mark.slow


def test_kill_capture_rolls_to_part2(service, ssd):
    sid = f"S_roll_{int(time.time())}"
    folder = ssd / "Data_Riset_IMU" / "S001_rolltest"  # sorts before _pending_cameras
    folder.mkdir(parents=True, exist_ok=True)

    service.set_selection([CameraSelectionItem(device_key="fake:0"),
                           CameraSelectionItem(device_key="fake:1")])
    assert service.wait_live(timeout=8)

    asyncio.run(service.start_recording(sid, folder))
    time.sleep(2)

    worker = service.worker("cam1")
    old_pid = worker.process.pid
    worker.process.kill()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        proc = worker.process
        if proc is not None and proc.pid != old_pid and worker.state == "live":
            break
        time.sleep(0.1)
    else:
        pytest.fail("cam1 never came back after the kill")
    time.sleep(2)  # let part 2 collect frames

    result = asyncio.run(service.stop_recording(sid))

    rec1 = next(r for r in result.recordings if r.cam_id == "cam1")
    rec2 = next(r for r in result.recordings if r.cam_id == "cam2")

    # Both parts exist, in order, with the frozen names.
    assert [p.name for p in rec1.files] == [
        f"{sid}_cam1_video_sync.webm",
        f"{sid}_cam1_video_sync_part2.webm",
    ]
    for path in rec1.files:
        assert path.is_file() and path.stat().st_size > 0
        assert webm.count_ebml_headers(path) == 1
    # The untouched camera kept a single file.
    assert [p.name for p in rec2.files] == [f"{sid}_cam2_video_sync.webm"]

    # track_ended in the stop result and in the anchors file.
    assert any(e.type == "track_ended" and e.camId == "cam1"
               for e in result.events)
    anchors = {c["cam_id"]: c for c in
               json.loads((folder / f"{sid}_cameras.json").read_text())["cameras"]}
    assert anchors["cam1"]["track_ended_at_ms"] > 0
    assert anchors["cam2"]["track_ended_at_ms"] == 0

    # Part 1 covers the time before the kill, part 2 after; both got frames.
    reports = {r.path.split("/")[-1]: r for r in rec1.reports}
    part1 = reports[f"{sid}_cam1_video_sync.webm"]
    part2 = reports[f"{sid}_cam1_video_sync_part2.webm"]
    assert part1.ok and part2.ok
    assert part1.bytesWritten > 0 and part2.bytesWritten > 0
