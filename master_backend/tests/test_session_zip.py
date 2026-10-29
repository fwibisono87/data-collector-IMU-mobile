"""Server-side session ZIP (OUTPUT_CONTRACT §9): entry order, names and JSON shapes.

The golden contract test runs the whole deterministic scenario; these tests pin the
rules that are easier to state directly, and the failure paths the golden does not
cover (missing backend data, failed video finalize).
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from master_backend.app.camera.interface import CameraEvent, CameraRecording, VideoFileReport
from master_backend.app.session_zip import (
    DATA_KINDS,
    build_session_zip,
    cameras_document,
    session_zip_name,
    video_report_document,
)

SID = "1700000000000"


def recording(folder: Path, cam_id: str = "cam1", *, parts: int = 1,
              ok: bool = True, ebml: int = 1, size: int = 8) -> CameraRecording:
    files, reports = [], []
    for i in range(parts):
        suffix = "" if i == 0 else f"_part{i + 1}"
        path = folder / f"{SID}_{cam_id}_video_sync{suffix}.webm"
        path.write_bytes(b"V" * size)
        files.append(path)
        reports.append(VideoFileReport(
            path=f"videos/{path.name}", camId=cam_id, ok=ok,
            bytesWritten=size, durationMs=6000, ebmlHeaders=ebml,
            error=None if ok else "second EBML header",
        ))
    return CameraRecording(
        cam_id=cam_id, device_key=f"dev-{cam_id}", label=f"Label {cam_id}",
        mime="video/webm", files=files, started_at_ms=1_000, flash_at_ms=1_000,
        stopped_at_ms=7_000, reports=reports,
    )


def manifest_for(folder: Path, *, recovery: bool = True) -> dict:
    data = folder / f"{SID}_chest_sensor_data.csv"
    data.write_text("# meta\nheader\n1,2,3\n", encoding="utf-8")
    cameras_json = folder / f"{SID}_cameras.json"
    cameras_json.write_text("{}", encoding="utf-8")
    entries = {
        "session_id": SID, "found": True, "subject": "Sub ject",
        "session_tag": "Tag A", "status": "PASS", "files": [
            {"name": data.name, "path": str(data), "size": data.stat().st_size, "kind": "main"},
            {"name": cameras_json.name, "path": str(cameras_json), "size": 2, "kind": "cameras"},
        ], "recovery": [],
    }
    if recovery:
        csv = folder / "dev-waist.csv"
        csv.write_text("# rescue\nheader\n4,5,6\n", encoding="utf-8")
        entries["recovery"] = [{
            "device_id": "dev-waist", "complete": True, "csv_exists": True,
            "csv_path": str(csv),
        }]
    return entries


def entries_of(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


def read_json(path: Path, name: str) -> dict:
    with zipfile.ZipFile(path) as zf:
        return json.loads(zf.read(name))


def test_name_follows_the_browser_with_empty_defaults_and_path_safety():
    assert session_zip_name("Sub ject", "Tag\tA", SID) == "Sub_ject_Tag_A_1700000000000.zip"
    assert session_zip_name("", "", SID) == "subject_session_1700000000000.zip"
    assert session_zip_name(None, None, SID) == "subject_session_1700000000000.zip"
    # A slash in user-entered text must not escape the session folder on disk.
    assert session_zip_name("a/b", "c", SID) == "a_b_c_1700000000000.zip"


def test_entry_order_and_contents(tmp_path):
    rec = recording(tmp_path, "cam2", parts=2)
    manifest = manifest_for(tmp_path)
    out = build_session_zip(
        SID, tmp_path, manifest=manifest,
        recordings=[rec], events=[CameraEvent(
            key="k1", sessionId=SID, camId="cam2", type="started", atMs=1_000,
        )], missed=["cam3"],
    )

    assert out.name == "Sub_ject_Tag_A_1700000000000.zip"
    assert out.parent == tmp_path
    assert entries_of(out) == [
        f"videos/{SID}_cam2_video_sync.webm",
        f"videos/{SID}_cam2_video_sync_part2.webm",
        f"data/{SID}_chest_sensor_data.csv",
        "data/recovery/dev-waist.csv",
        "data/manifest.json",
        "cameras.json",
        "missed_cameras.txt",
        "video_report.json",
    ]
    # The backend camera anchor file ("cameras" is not a data kind) never enters.
    assert f"data/{SID}_cameras.json" not in entries_of(out)


def test_csv_bytes_are_copied_exactly_and_videos_are_stored(tmp_path):
    rec = recording(tmp_path, size=2 * 1024 * 1024 + 17)   # crosses the copy chunk
    out = build_session_zip(SID, tmp_path, manifest=manifest_for(tmp_path), recordings=[rec])
    with zipfile.ZipFile(out) as zf:
        video = zf.getinfo(f"videos/{SID}_cam1_video_sync.webm")
        assert video.compress_type == zipfile.ZIP_STORED
        assert zf.read(video.filename) == b"V" * (2 * 1024 * 1024 + 17)
        csv = zf.getinfo(f"data/{SID}_chest_sensor_data.csv")
        assert csv.compress_type == zipfile.ZIP_DEFLATED
        assert zf.read(csv.filename) == (tmp_path / f"{SID}_chest_sensor_data.csv").read_bytes()


def test_manifest_is_javascript_serialised_including_unicode(tmp_path):
    out = build_session_zip(SID, tmp_path, manifest=manifest_for(tmp_path))
    raw = None
    with zipfile.ZipFile(out) as zf:
        raw = zf.read("data/manifest.json").decode("utf-8")
    assert not raw.endswith("\n")
    parsed = json.loads(raw)
    assert parsed["subject"] == "Sub ject"
    # js_json_dumps keeps non-ASCII characters unescaped, like JSON.stringify.
    assert parsed["session_id"] == SID


def test_cameras_document_shape_and_key_order(tmp_path):
    rec = recording(tmp_path, "cam2", parts=2)
    doc = cameras_document(SID, [rec], [
        CameraEvent(key="k1", sessionId=SID, camId="cam2", type="track_ended", atMs=5,
                    detail="dropout"),
        CameraEvent(key="k2", sessionId=SID, camId="cam2", type="stopped", atMs=7_000),
    ])
    camera = doc["cameras"][0]
    assert list(camera) == [
        "session_id", "cam_id", "device_id", "browser_label", "mime",
        "file", "files", "started_at_ms", "flash_at_ms", "stopped_at_ms",
    ]
    assert camera["file"] == f"videos/{SID}_cam2_video_sync.webm"
    assert camera["files"] == [
        f"videos/{SID}_cam2_video_sync.webm",
        f"videos/{SID}_cam2_video_sync_part2.webm",
    ]
    assert camera["device_id"] == "dev-cam2"
    ended, stopped = doc["camera_events"]
    assert list(ended) == ["key", "sessionId", "camId", "type", "atMs", "detail"]
    assert ended["detail"] == "dropout"
    # detail is omitted when empty/None, exactly as the browser's records were.
    assert "detail" not in stopped


def test_video_report_order_health_and_error_omission(tmp_path):
    good = recording(tmp_path, "cam1")
    bad = recording(tmp_path, "cam2", ok=False, ebml=2)
    doc = video_report_document(SID, [good, bad])
    assert [v["camId"] for v in doc["videos"]] == ["cam1", "cam2"]
    assert list(doc["videos"][0]) == [
        "path", "camId", "ok", "bytesWritten", "durationMs", "ebmlHeaders",
    ]
    assert doc["videos"][1]["error"] == "second EBML header"
    assert doc["healthy"] is False


def test_healthy_requires_one_ebml_header_per_file(tmp_path):
    doc = video_report_document(SID, [recording(tmp_path, "cam1", ebml=2)])
    assert doc["healthy"] is False


def test_missed_cameras_file_only_when_non_empty(tmp_path):
    out = build_session_zip(SID, tmp_path, manifest=manifest_for(tmp_path))
    assert "missed_cameras.txt" not in entries_of(out)
    out = build_session_zip(SID, tmp_path, manifest=manifest_for(tmp_path), missed=["cam3", "cam5"])
    with zipfile.ZipFile(out) as zf:
        assert zf.read("missed_cameras.txt") == b"cam3\ncam5\n"


def test_missing_manifest_writes_the_browser_error_note(tmp_path):
    rec = recording(tmp_path)
    out = build_session_zip(SID, tmp_path, manifest=None, recordings=[rec],
                            error_text="HTTP 503")
    names = entries_of(out)
    assert names == [
        f"videos/{SID}_cam1_video_sync.webm",
        "data/export_error.txt",
        "cameras.json",
        "video_report.json",
    ]
    with zipfile.ZipFile(out) as zf:
        text = zf.read("data/export_error.txt").decode("utf-8")
    assert text == (
        "Backend data unavailable (HTTP 503).\n"
        "This ZIP contains video only — pull/rescue CSVs from the Recovery screen.\n"
    )


def test_a_failed_build_leaves_no_tmp_archive(tmp_path, monkeypatch):
    from master_backend.app import session_zip as module

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(module, "_copy_file", boom)
    with pytest.raises(OSError):
        build_session_zip(SID, tmp_path, manifest=manifest_for(tmp_path),
                          recordings=[recording(tmp_path)])
    assert not list(tmp_path.glob("*.zip*"))


def test_data_kinds_match_the_browser_list():
    assert DATA_KINDS == {
        "main", "csv", "late", "rescue", "merged", "consolidated",
        "integrity", "connectivity", "late_summary", "consolidation",
        "consolidated_validation", "timing",
    }