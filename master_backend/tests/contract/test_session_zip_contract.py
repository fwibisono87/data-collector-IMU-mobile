"""Golden contract for the server-built session ZIP (OUTPUT_CONTRACT.md §9).

Runs the same deterministic session as ``test_output_contract.py`` (importing its
helpers), records two fake cameras — one of them dropping mid-session so a ``_part2``
file and a ``track_ended`` event exist — then builds the archive the same way the
post-session job does and snapshots every entry name and the generated JSON.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
import zipfile

import pytest

from master_backend.app.api.schema import CameraSelectionItem
from master_backend.app.camera.fake import FakeCameraService
from master_backend.app.export import export_manifest
from master_backend.app.session_zip import build_session_zip

from test_output_contract import (
    _csv_payload,
    normalise,
    pretty,
    run_scenario,
    unified_diff,
)

GOLDEN_PATH = Path(__file__).parent / "golden" / "session_zip_snapshot.json"


class StepClock:
    """A clock the test advances by hand; deterministic camera anchors and durations."""

    def __init__(self, start: float) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


async def run_zip_scenario(rig, monkeypatch) -> dict:
    """Scenario artifacts + fake-camera recordings + the server-built session ZIP."""
    scenario = await run_scenario(rig, monkeypatch)
    session_id, folder = scenario["session_id"], scenario["folder"]

    # Same event loop, same frozen clock: the fake writes its files into the session
    # folder and its anchors through cameras_mark, exactly like the real service.
    clock = StepClock(time.time())
    service = FakeCameraService(2, clock=clock)
    service.set_selection([
        CameraSelectionItem(device_key="fake:0"),
        CameraSelectionItem(device_key="fake:1"),
    ])
    await service.start_recording(session_id, folder)
    service.fail("cam2")                     # dropout -> _part2 file + track_ended
    clock.value += 6.5                       # a deterministic 6_500 ms of footage
    stopped = await service.stop_recording(session_id)

    # The fake always writes a file for every selected camera; "missed" is the camera
    # the operator selected but which never produced footage (e.g. it was not live).
    missed = list(stopped.missed) + ["cam3"]

    manifest = await export_manifest(session_id)
    zip_path = build_session_zip(
        session_id, folder,
        subject=manifest["subject"], session_tag=manifest["session_tag"],
        manifest=manifest,
        recordings=stopped.recordings, events=stopped.events, missed=missed,
    )
    return {
        "session_id": session_id,
        "zip_name": zip_path.name,
        "snapshot": zip_snapshot(zip_path, session_id),
    }


def zip_snapshot(path: Path, session_id: str) -> dict:
    """Every entry name (in archive order) plus the content of the JSON/CSV entries."""
    out: dict = {"entries": [], "json": {}, "csv": {}, "binary_sizes": {}}
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            data = archive.read(name)
            out["entries"].append(name)
            if name.endswith(".json"):
                out["json"][name] = json.loads(data)
            elif name.endswith(".csv"):
                out["csv"][name] = _csv_payload(data.decode("utf-8", "replace"))
            else:
                out["binary_sizes"][name] = len(data)
    manifest = out["json"].get("data/manifest.json")
    if manifest:
        for entry in manifest.get("files", []):
            if str(entry.get("name", "")).endswith("_bundle.zip"):
                # The data bundle embeds absolute artifact paths in its own
                # bundle_manifest.json, so its compressed size depends on the per-run
                # temp directory.  The output contract freezes the bundle's contents,
                # never its size — do the same here.
                entry["size"] = "<volatile>"
    return normalise(out, session_id)


def _drive_zip(rig, monkeypatch) -> dict:
    return asyncio.run(run_zip_scenario(rig, monkeypatch))


@pytest.fixture
def zip_scenario(rig, monkeypatch):
    return _drive_zip(rig, monkeypatch)


# ── Format-level assertions, independent of the golden file ────────────────


def test_entries_are_the_browser_archive(zip_scenario):
    sid = "<SID>"
    assert zip_scenario["zip_name"] == f"SUBJ_01_TAG_A_{zip_scenario['session_id']}.zip"
    assert zip_scenario["snapshot"]["entries"][:3] == [
        f"videos/{sid}_cam1_video_sync.webm",
        f"videos/{sid}_cam2_video_sync.webm",
        f"videos/{sid}_cam2_video_sync_part2.webm",
    ]
    assert zip_scenario["snapshot"]["entries"][-4:] == [
        "data/manifest.json", "cameras.json", "missed_cameras.txt", "video_report.json",
    ]
    # The backend data bundle, camera anchors and ledger never enter the deliverable.
    assert not any(name.startswith("data/") and name.endswith("_bundle.zip")
                   for name in zip_scenario["snapshot"]["entries"])
    assert not any(name.endswith("_cameras.json") for name in zip_scenario["snapshot"]["entries"])
    assert not any("state.json" in name for name in zip_scenario["snapshot"]["entries"])


def test_video_report_covers_every_file_and_is_healthy(zip_scenario):
    report = zip_scenario["snapshot"]["json"]["video_report.json"]
    assert [v["path"] for v in report["videos"]] == [
        f"videos/<SID>_cam1_video_sync.webm",
        f"videos/<SID>_cam2_video_sync.webm",
        f"videos/<SID>_cam2_video_sync_part2.webm",
    ]
    assert report["healthy"] is True
    assert all(v["ebmlHeaders"] == 1 and v["ok"] for v in report["videos"])
    assert report["videos"][0]["durationMs"] == 6_500


def test_cameras_document_lists_both_recordings_and_the_dropout(zip_scenario):
    doc = zip_scenario["snapshot"]["json"]["cameras.json"]
    assert [c["cam_id"] for c in doc["cameras"]] == ["cam1", "cam2"]
    cam2 = doc["cameras"][1]
    assert cam2["files"] == [
        "videos/<SID>_cam2_video_sync.webm",
        "videos/<SID>_cam2_video_sync_part2.webm",
    ]
    assert cam2["file"] == "videos/<SID>_cam2_video_sync.webm"
    assert cam2["device_id"] == "fake:1"
    types = [e["type"] for e in doc["camera_events"]]
    assert types.count("started") == 2 and types.count("stopped") == 2
    assert "track_ended" in types


def test_recovery_csv_is_included_under_data_recovery(zip_scenario):
    names = [name for name in zip_scenario["snapshot"]["entries"] if name.startswith("data/recovery/")]
    assert len(names) == 1
    assert names[0].endswith(".csv")


def test_the_videos_are_stored_not_compressed(tmp_path):
    from master_backend.app.session_zip import build_session_zip
    from test_session_zip import manifest_for, recording

    out = build_session_zip("1700000000000", tmp_path, manifest=manifest_for(tmp_path),
                            recordings=[recording(tmp_path)])
    with zipfile.ZipFile(out) as zf:
        video = zf.getinfo("videos/1700000000000_cam1_video_sync.webm")
        data = zf.getinfo("data/1700000000000_chest_sensor_data.csv")
    assert video.compress_type == zipfile.ZIP_STORED
    assert data.compress_type == zipfile.ZIP_DEFLATED


# ── The golden file ─────────────────────────────────────────────────────────


def test_session_zip_matches_golden(zip_scenario):
    actual = zip_scenario["snapshot"]
    if os.getenv("UPDATE_GOLDEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(pretty(actual) + "\n", encoding="utf-8")
        return
    assert GOLDEN_PATH.is_file(), (
        f"{GOLDEN_PATH} is missing — regenerate with "
        "UPDATE_GOLDEN=1 master_backend/.venv/bin/python -m pytest "
        "master_backend/tests/contract -q"
    )
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    if actual != expected:
        raise AssertionError(
            "session ZIP outputs changed. Regenerate the golden file with "
            "UPDATE_GOLDEN=1 only after re-reading docs/OUTPUT_CONTRACT.md §9.\n"
            + unified_diff(pretty(expected), pretty(actual))
        )