"""Seams between lanes that each lane's own tests could not see."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient


def test_camera_routes_share_the_runtime_camera_service(monkeypatch, tmp_path):
    # Two RealCameraService instances would each try to open the same webcams.
    monkeypatch.setenv("SSD_PATH", str(tmp_path))
    monkeypatch.setenv("IMU_CAMERAS", "fake")
    monkeypatch.setenv("IMU_FAKE_CAMERAS", "1")
    from master_backend.app.api.runtime import get_runtime
    from master_backend.app.main import app

    with TestClient(app):
        assert app.state.camera_service is get_runtime().camera_service
    assert app.state.camera_service is None


def test_session_folders_skip_backend_system_folders(rig, monkeypatch):
    """A lowercase subject sorts after "_", so _pending_cameras used to win the lookup."""
    from master_backend.app import cameras, export

    ssd = rig.ssd_path
    monkeypatch.setattr(export, "SSD_PATH", ssd)
    pending = ssd / "Data_Riset_IMU" / "_pending_cameras"
    monkeypatch.setattr(cameras, "PENDING_CAMERAS", pending)
    sid = "1700000000000"

    # The first anchor arrives before any CSV exists: it lands in _pending_cameras.
    asyncio.run(cameras.cameras_mark(sid, {"cam_id": "cam1", "event": "started", "ts_ms": 1}))
    assert (pending / f"{sid}_cameras.json").exists()

    # Then the session folder appears, named after a lowercase subject.
    session_folder = ssd / "Data_Riset_IMU" / "walk_trial"
    session_folder.mkdir(parents=True)
    (session_folder / f"{sid}_chest_sensor_data.csv").write_text("x\n")

    assert export._session_folders(sid) == [session_folder]
    asyncio.run(cameras.cameras_mark(sid, {"cam_id": "cam1", "event": "stopped", "ts_ms": 2}))
    assert (session_folder / f"{sid}_cameras.json").exists()
    assert not (pending / f"{sid}_cameras.json").exists()
