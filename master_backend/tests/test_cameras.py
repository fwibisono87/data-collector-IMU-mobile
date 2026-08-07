"""Camera sync-anchor router: durable per-(cam, event) wall-clock marks."""
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SESSION_ID = "1753000000000"


class _NoopAudit:
    async def log(self, *args, **kwargs):
        pass


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    ssd = tmp_path / "ssd"
    monkeypatch.setenv("SSD_PATH", str(ssd))
    # cameras.py and export.py read SSD_PATH at import time — force a re-import so the
    # module-level paths point at the temp SSD for this test.
    import master_backend.app.cameras as cam_mod
    import master_backend.app.export as export_mod

    monkeypatch.setattr(export_mod, "SSD_PATH", ssd)
    monkeypatch.setattr(export_mod, "RESCUE_PATH", tmp_path / "rescue")
    monkeypatch.setattr(cam_mod, "SSD_PATH", ssd)
    monkeypatch.setattr(cam_mod, "PENDING_CAMERAS", ssd / "Data_Riset_IMU" / "_pending_cameras")
    monkeypatch.setattr(cam_mod.audit, "log", _NoopAudit().log)
    return ssd


@pytest.fixture
def run():
    import asyncio

    from master_backend.app.cameras import cameras_get, cameras_mark

    async def _run(method: str, session_id: str, body: dict | None = None):
        if method == "GET":
            return await cameras_get(session_id)
        return await cameras_mark(session_id, body or {})

    def invoke(method: str, session_id: str, body: dict | None = None):
        return asyncio.run(_run(method, session_id, body))

    return invoke


def test_started_then_stopped_merge_single_record(env, run):
    r1 = run("POST", SESSION_ID, {"cam_id": "cam1", "event": "started", "ts_ms": 100})
    assert r1["started_at_ms"] == 100
    assert r1["stopped_at_ms"] == 0

    r2 = run("POST", SESSION_ID, {"cam_id": "cam1", "event": "stopped", "ts_ms": 500})
    assert r2["started_at_ms"] == 100
    assert r2["stopped_at_ms"] == 500

    got = run("GET", SESSION_ID)
    assert len(got["cameras"]) == 1
    cam = got["cameras"][0]
    assert cam["cam_id"] == "cam1"
    assert cam["started_at_ms"] == 100
    assert cam["stopped_at_ms"] == 500


def test_two_cameras_do_not_clobber(env, run):
    run("POST", SESSION_ID, {"cam_id": "cam1", "event": "started", "ts_ms": 100})
    run("POST", SESSION_ID, {"cam_id": "cam2", "event": "started", "ts_ms": 200})
    run("POST", SESSION_ID, {"cam_id": "cam2", "event": "stopped", "ts_ms": 900})

    got = run("GET", SESSION_ID)
    cams = {c["cam_id"]: c for c in got["cameras"]}
    assert set(cams) == {"cam1", "cam2"}
    assert cams["cam1"]["started_at_ms"] == 100
    assert cams["cam1"]["stopped_at_ms"] == 0
    assert cams["cam2"]["started_at_ms"] == 200
    assert cams["cam2"]["stopped_at_ms"] == 900


def test_restart_overwrites_only_that_field(env, run):
    run("POST", SESSION_ID, {"cam_id": "cam1", "event": "started", "ts_ms": 100})
    run("POST", SESSION_ID, {"cam_id": "cam1", "event": "stopped", "ts_ms": 500})

    r = run("POST", SESSION_ID, {"cam_id": "cam1", "event": "started", "ts_ms": 120})
    assert r["started_at_ms"] == 120
    assert r["stopped_at_ms"] == 500

    got = run("GET", SESSION_ID)
    cam = got["cameras"][0]
    assert cam["started_at_ms"] == 120
    assert cam["stopped_at_ms"] == 500


def test_unknown_event_and_bad_session_rejected(env, run):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e1:
        run("POST", SESSION_ID, {"cam_id": "cam1", "event": "boom", "ts_ms": 0})
    assert e1.value.status_code == 400

    with pytest.raises(HTTPException) as e2:
        run("POST", "bad/session", {"cam_id": "cam1", "event": "started", "ts_ms": 0})
    assert e2.value.status_code == 400

    with pytest.raises(HTTPException) as e3:
        run("GET", "")
    assert e3.value.status_code == 400


def test_get_absent_session_returns_empty_shape(env, run):
    got = run("GET", SESSION_ID)
    assert got == {"session_id": SESSION_ID, "cameras": []}


def test_classify_routes_cameras_and_video(env):
    from master_backend.app.export import _SCAN_KINDS, _classify

    assert _classify(f"{SESSION_ID}_cameras.json") == "cameras"
    assert _classify(f"{SESSION_ID}_cam1_video_sync.webm") == "video"
    assert _classify(f"{SESSION_ID}_cam1_video_sync.mp4") == "video"
    assert "cameras" not in _SCAN_KINDS
    assert "video" not in _SCAN_KINDS
