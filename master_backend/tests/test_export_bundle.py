"""The data bundle must never contain video (OUTPUT_CONTRACT §10.11).

Lane L5 writes ``<sid>_<camId>_video_sync[_partN].webm`` into the same session folder as
the CSVs, so the bundle's file filter is now load-bearing: before this, only the first
segment was classified as video and every dropout segment would have been bundled as
"other" data.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from master_backend.app.export import _classify, build_session_bundle

SID = "1700000000000"
FOLDER = "Subject_Tag"


@pytest.mark.parametrize("suffix", ["", "_part2", "_part10"])
@pytest.mark.parametrize("ext", ["webm", "mp4"])
def test_every_video_segment_is_classified_as_video(suffix, ext):
    assert _classify(f"{SID}_cam1_video_sync{suffix}.{ext}") == "video"


def test_part_named_like_data_is_not_mistaken_for_video():
    assert _classify(f"{SID}_chest_sensor_data.csv") == "main"
    assert _classify(f"{SID}_cameras.json") == "cameras"
    # Only the `_video_sync` stem counts; a CSV that merely mentions it stays data.
    assert _classify(f"{SID}_cam1_video_sync_notes.csv") == "csv"


@pytest.mark.asyncio
async def test_bundle_skips_all_video_segments(rig):
    folder = rig.ssd_path / "Data_Riset_IMU" / FOLDER
    folder.mkdir(parents=True)
    (folder / f"{SID}_chest_sensor_data.csv").write_text("header\n1\n", encoding="utf-8")
    for name in (
        f"{SID}_cam1_video_sync.webm",
        f"{SID}_cam1_video_sync_part2.webm",
        f"{SID}_cam2_video_sync_part10.mp4",
    ):
        (folder / name).write_bytes(b"V" * 64)

    result = await build_session_bundle(SID)
    archive = Path(result["path"])
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        bundle_manifest = json.loads(zf.read("bundle_manifest.json"))

    assert names == [f"data/{SID}_chest_sensor_data.csv", "bundle_manifest.json"]
    assert result["entries"] == [f"data/{SID}_chest_sensor_data.csv"]
    assert bundle_manifest["contains_video"] is False
    assert not any("video" in name for name in names)


@pytest.mark.asyncio
async def test_the_browser_zip_still_gets_the_videos_from_the_stop_result(rig):
    """The exclusion is bundle-only: the deliverable keeps its footage (OUTPUT_CONTRACT §9)."""
    from master_backend.app.session_zip import build_session_zip
    from test_session_zip import recording

    folder = rig.ssd_path / "Data_Riset_IMU" / FOLDER
    folder.mkdir(parents=True)
    rec = recording(folder, "cam1", parts=2)
    out = build_session_zip(SID, folder, manifest=None, recordings=[rec])
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert f"videos/{SID}_cam1_video_sync.webm" in names
    assert f"videos/{SID}_cam1_video_sync_part2.webm" in names