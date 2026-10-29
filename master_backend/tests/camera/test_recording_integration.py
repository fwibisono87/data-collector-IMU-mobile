"""Integration: a full 10 s recording with 3 fake cameras (spec §7).

For every file: ffprobe duration within 1 s of ``stopped - started``, exactly
one EBML header, the ``<sid>_cameras.json`` anchors written, and the frames.csv
row count within 2 % of the ffprobe frame count.
"""
from __future__ import annotations

import asyncio
import csv
import json
import time

import pytest

from master_backend.app.api.schema import CameraSelectionItem
from master_backend.app.camera import discovery, webm

pytestmark = pytest.mark.slow


def _set_fake_count(monkeypatch, n: int) -> None:
    """Point fake discovery at ``n`` cameras (bypasses the 1 s cache)."""
    monkeypatch.setenv("IMU_FAKE_CAMERAS", str(n))
    discovery.invalidate_cache()


def test_recording_10s(service, ssd, monkeypatch):
    _set_fake_count(monkeypatch, 3)
    sid = f"S_rec_{int(time.time())}"
    # The folder name must sort before "_pending_cameras" (uppercase/digit
    # prefix does): cameras_mark resolves the session folder through
    # export._session_folders(), which returns the FIRST sorted match, and a
    # pending anchor file makes _pending_cameras itself a match. A lowercase
    # "subj_..." folder would trap every anchor in pending forever.
    folder = ssd / "Data_Riset_IMU" / "S001_rectest"
    folder.mkdir(parents=True, exist_ok=True)

    service.set_selection([CameraSelectionItem(device_key=f"fake:{i}")
                           for i in range(3)])
    assert service.wait_live(timeout=8)

    asyncio.run(service.start_recording(sid, folder))
    time.sleep(10)
    result = asyncio.run(service.stop_recording(sid))

    assert result.session_id == sid
    assert result.missed == []
    assert len(result.recordings) == 3

    anchors_path = folder / f"{sid}_cameras.json"
    assert anchors_path.is_file(), "anchors were not written"
    anchors = {c["cam_id"]: c
               for c in json.loads(anchors_path.read_text())["cameras"]}

    for rec in result.recordings:
        assert len(rec.files) == 1, f"{rec.cam_id} rolled over unexpectedly"
        path = rec.files[0]
        assert path.name == f"{sid}_{rec.cam_id}_video_sync.webm"
        assert path.is_file() and path.stat().st_size > 10_000

        # Duration matches the anchor span within 1 s (and the span ~10 s).
        span_ms = rec.stopped_at_ms - rec.started_at_ms
        assert abs(span_ms - 10_000) < 1500, span_ms
        duration = webm.probe_duration_ms(path)
        assert duration is not None
        assert abs(duration - span_ms) <= 1000, (duration, span_ms)

        # Clean single-file WebM: exactly one EBML header, cues present.
        assert webm.count_ebml_headers(path) == 1
        assert webm.has_cues(path)

        # frames.csv rows match the ffprobe frame count within 2 %.
        csv_path = folder / "_extras" / f"{sid}_{rec.cam_id}_frames.csv"
        with csv_path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        probed = webm.probe_frame_count(path)
        assert probed is not None and probed > 200
        assert abs(len(rows) - probed) <= max(2, probed * 0.02), (
            len(rows), probed)
        assert rows[0]["part"] == "1"
        arrival = [int(r["arrival_epoch_ms"]) for r in rows]
        assert arrival == sorted(arrival)
        assert rec.started_at_ms <= arrival[0]
        assert arrival[-1] <= rec.stopped_at_ms + 500

        # Anchors: started == flash, stopped after started, this camera's slot.
        anchor = anchors[rec.cam_id]
        assert anchor["started_at_ms"] == rec.started_at_ms
        assert anchor["flash_at_ms"] == rec.started_at_ms
        assert anchor["stopped_at_ms"] == rec.stopped_at_ms
        assert anchor["device_id"] == rec.device_key
        assert anchor["mime"] == rec.mime

        # Per-file finalize report.
        assert len(rec.reports) == 1
        report = rec.reports[0]
        assert report.ok, report.error
        assert report.ebmlHeaders == 1
        assert report.durationMs is not None

    # The stop result carries started/stopped events for every camera.
    events = {(e.camId, e.type) for e in result.events}
    for cam_id in ("cam1", "cam2", "cam3"):
        assert (cam_id, "started") in events
        assert (cam_id, "stopped") in events
