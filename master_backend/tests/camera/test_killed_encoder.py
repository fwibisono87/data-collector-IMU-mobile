"""Integration: SIGKILL an encoder mid-recording (spec §7).

The encoder dies without writing its Cues/Duration; stop_recording must remux
the orphaned part so the file is playable: Duration and Cues present.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from master_backend.app.api.schema import CameraSelectionItem
from master_backend.app.camera import webm

pytestmark = pytest.mark.slow


def test_killed_encoder_is_remuxed_on_stop(service, ssd):
    sid = f"S_kill_{int(time.time())}"
    folder = ssd / "Data_Riset_IMU" / "S001_killtest"  # sorts before _pending_cameras
    folder.mkdir(parents=True, exist_ok=True)

    service.set_selection([CameraSelectionItem(device_key="fake:0")])
    assert service.wait_live(timeout=8)

    asyncio.run(service.start_recording(sid, folder))
    time.sleep(3)

    encoder = service._recorder.recorders["cam1"].parts[-1]
    killed = encoder.path
    encoder.proc.kill()  # SIGKILL: no trailer, no cues, no duration
    time.sleep(1.5)      # let the recorder notice and react

    result = asyncio.run(service.stop_recording(sid))
    rec = next(r for r in result.recordings if r.cam_id == "cam1")

    assert killed.name == f"{sid}_cam1_video_sync.webm"
    assert killed in rec.files or killed.exists()
    # The killed part was remuxed into a playable file.
    duration = webm.probe_duration_ms(killed)
    assert duration is not None, "remux did not recover a duration"
    assert 1500 <= duration <= 6000, duration
    assert webm.has_cues(killed), "remux did not write Cues"
    assert webm.count_ebml_headers(killed) == 1
    frames = webm.probe_frame_count(killed)
    assert frames is not None and frames >= 30

    # The crash surfaced as a write_error event, and the session still
    # produced a result instead of raising.
    assert any(e.type == "write_error" and e.camId == "cam1"
               for e in result.events)
    assert result.missed == []
    report = next(r for r in rec.reports if r.path.endswith(killed.name))
    assert report.ok, report.error
    assert report.durationMs is not None
