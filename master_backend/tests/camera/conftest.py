"""Camera-lane test rig.

Every test here drives the real camera service against fake (lavfi testsrc2)
cameras unless stated otherwise. The rig redirects all storage the camera
stack touches into ``tmp_path``:

* ``export.SSD_PATH`` / ``RESCUE_PATH`` — anchor folder resolution
  (``cameras_mark`` -> ``_session_folders``);
* ``cameras.SSD_PATH`` / ``PENDING_CAMERAS`` — the anchor writer itself;
* the service's calibration store, via ``RealCameraService(ssd_root=...)``.

Module-level caches and loop-bound locks (the per-session asyncio locks in
``cameras.py``, the discovery cache, the route preview counters and service
singleton) are reset between tests. The ``slow`` marker is registered here
because the repo has no pytest config file.
"""
from __future__ import annotations

import asyncio

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: long-running camera integration test (real recordings, ffmpeg)",
    )


@pytest.fixture()
def ssd(tmp_path, monkeypatch):
    """Temporary SSD root with the anchor writer and export pointed at it."""
    root = tmp_path / "ssd"
    rescue = tmp_path / "rescue"
    (root / "Data_Riset_IMU").mkdir(parents=True)
    rescue.mkdir(parents=True)
    monkeypatch.setenv("SSD_PATH", str(root))
    monkeypatch.setenv("RESCUE_PATH", str(rescue))
    monkeypatch.setenv("IMU_CAMERAS", "fake")
    monkeypatch.setenv("IMU_FAKE_CAMERAS", "2")

    from master_backend.app import cameras as cameras_mod, export
    from master_backend.app.audit_logger import audit
    from master_backend.app.camera import discovery, routes

    monkeypatch.setattr(export, "SSD_PATH", root)
    monkeypatch.setattr(export, "RESCUE_PATH", rescue)
    monkeypatch.setattr(cameras_mod, "SSD_PATH", root)
    monkeypatch.setattr(
        cameras_mod, "PENDING_CAMERAS",
        root / "Data_Riset_IMU" / "_pending_cameras")

    cameras_mod._camera_locks.clear()
    export._manifest_scan_cache.clear()
    audit._buffer.clear()
    discovery.invalidate_cache()
    routes.reset_service()
    try:
        yield root
    finally:
        routes.reset_service()
        routes._preview_counts.clear()
        cameras_mod._camera_locks.clear()
        discovery.invalidate_cache()


@pytest.fixture()
def service(ssd):
    """A RealCameraService whose calibration store lives under the tmp SSD."""
    from master_backend.app.camera.service import RealCameraService

    svc = RealCameraService(ssd_root=ssd)
    try:
        yield svc
    finally:
        asyncio.run(svc.close())


@pytest.fixture()
def selected(service):
    """Service with the two default fake cameras selected and live."""
    from master_backend.app.api.schema import CameraSelectionItem

    service.set_selection([CameraSelectionItem(device_key="fake:0"),
                           CameraSelectionItem(device_key="fake:1")])
    assert service.wait_live(timeout=8), "fake cameras did not go live"
    return service
