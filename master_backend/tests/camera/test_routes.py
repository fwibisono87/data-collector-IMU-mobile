"""Routes: TestClient with the router mounted alone (spec §7).

Covers devices, selection (409 while recording), latest.jpg, preview.mjpg
(≥ 3 multipart parts within 2 s, clean disconnect, per-camera client cap) and
the reference CRUD + compare endpoints.

The preview needs a direct ASGI drive: starlette 0.37.2's TestClient buffers
the entire response body (``portal.call(self.app, ...)`` in testclient.py)
before returning it, which deadlocks on an infinite multipart stream. The
helper below runs the app as a task on the test's own event loop and sends
``http.disconnect`` when enough parts arrived — exercising the same
``request.is_disconnected()`` path a real browser close would.
"""
from __future__ import annotations

import asyncio
import io
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from master_backend.app.api.schema import CameraSelectionItem
from master_backend.app.camera import routes


def _make_app(service) -> FastAPI:
    app = FastAPI()
    app.include_router(routes.router)
    app.state.camera_service = service
    return app


async def _drive_preview(app, path: str, min_parts: int = 3,
                         timeout: float = 2.0):
    """Run one GET against the raw ASGI app; returns (status, ctype, parts,
    clean_stop, elapsed_s)."""
    base, _, query = path.partition("?")
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "GET", "scheme": "http",
        "server": ("testserver", 80), "client": ("testclient", 50000),
        "path": base, "raw_path": base.encode(), "query_string": query.encode(),
        "root_path": "", "headers": [(b"host", b"testserver")], "state": {},
    }
    disconnect = asyncio.Event()
    body = bytearray()
    meta: dict = {}
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            meta["status"] = message["status"]
            headers = dict(message["headers"])
            meta["ctype"] = headers.get(b"content-type", b"").decode()
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    task = asyncio.create_task(app(scope, receive, send))
    started = time.monotonic()
    while (time.monotonic() - started < timeout
           and body.count(b"--frame\r\n") < min_parts):
        await asyncio.sleep(0.02)
    parts = body.count(b"--frame\r\n")
    elapsed = time.monotonic() - started
    disconnect.set()
    try:
        await asyncio.wait_for(task, timeout=3.0)
        clean = True
    except asyncio.TimeoutError:  # pragma: no cover - failure path
        task.cancel()
        clean = False
    return meta.get("status"), meta.get("ctype"), parts, clean, elapsed


def test_devices_and_selection_and_latest(selected):
    app = _make_app(selected)
    with TestClient(app) as client:
        r = client.get("/api/cameras/devices")
        assert r.status_code == 200
        keys = [d["device_key"] for d in r.json()["devices"]]
        assert keys == ["fake:0", "fake:1"]

        # Re-selecting the same devices is idempotent and keeps slots.
        r = client.put("/api/cameras/selection", json={"cameras": [
            {"device_key": "fake:0"}, {"device_key": "fake:1"}]})
        assert r.status_code == 200
        assert [c["cam_id"] for c in r.json()["cameras"]] == ["cam1", "cam2"]

        r = client.put("/api/cameras/selection",
                       json={"cameras": [{"nope": 1}]})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid"

        deadline = time.monotonic() + 3
        r = client.get("/api/cameras/cam1/latest.jpg")
        while r.status_code == 404 and time.monotonic() < deadline:
            time.sleep(0.1)
            r = client.get("/api/cameras/cam1/latest.jpg")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        with Image.open(io.BytesIO(r.content)) as image:
            assert image.size == (1280, 720)

        r = client.get("/api/cameras/nope/latest.jpg")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"


def test_selection_409_while_recording(selected, ssd):
    folder = ssd / "Data_Riset_IMU" / "S001_routerec"  # sorts before _pending_cameras
    folder.mkdir(parents=True, exist_ok=True)
    asyncio.run(selected.start_recording("S_route_rec", folder))
    try:
        with TestClient(_make_app(selected)) as client:
            r = client.put("/api/cameras/selection",
                           json={"cameras": [{"device_key": "fake:0"}]})
            assert r.status_code == 409
            assert r.json()["error"]["code"] == "bad_state"
    finally:
        asyncio.run(selected.stop_recording("S_route_rec"))


def test_preview_streams_and_releases_slot(selected):
    app = _make_app(selected)
    status, ctype, parts, clean, elapsed = asyncio.run(
        _drive_preview(app, "/api/cameras/cam1/preview.mjpg?w=320&fps=10"))
    assert status == 200
    assert ctype.startswith("multipart/x-mixed-replace")
    assert parts >= 3, f"only {parts} parts in {elapsed:.2f}s"
    assert elapsed < 2.0
    assert clean, "preview did not stop on client disconnect"
    assert routes._preview_counts == {}, "preview slot was not released"

    with TestClient(app) as client:
        r = client.get("/api/cameras/nope/preview.mjpg")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"

        # At the per-camera cap → 429 busy (simulated occupancy).
        routes._preview_counts["cam1"] = routes.PREVIEW_MAX_CLIENTS
        try:
            r = client.get("/api/cameras/cam1/preview.mjpg")
            assert r.status_code == 429
            assert r.json()["error"]["code"] == "busy"
        finally:
            routes._preview_counts.clear()


def test_reference_crud_and_compare(selected):
    with TestClient(_make_app(selected)) as client:
        r = client.post("/api/calibration/references", json={"name": "rt"})
        assert r.status_code == 200
        ref = r.json()
        ref_id = ref["ref_id"]
        assert ref["name"] == "rt"
        assert len(ref["cameras"]) == 2
        for cam in ref["cameras"]:
            assert abs(cam["skew_ms"]) <= 43.3

        r = client.get("/api/calibration/references")
        assert r.status_code == 200
        assert [x["ref_id"] for x in r.json()["references"]] == [ref_id]
        assert r.json()["active_ref_id"] is None

        r = client.patch(f"/api/calibration/references/{ref_id}",
                         json={"name": "renamed"})
        assert r.status_code == 200 and r.json()["name"] == "renamed"

        r = client.post(f"/api/calibration/references/{ref_id}/activate")
        assert r.status_code == 200 and r.json() == {}

        r = client.delete(f"/api/calibration/references/{ref_id}")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "bad_state"

        # The fake source (testsrc2) has a ~0.35 s vertical scroll period:
        # within one period of the reference still, live frames match a
        # vertically shifted copy (dy ~ -64) and the score oscillates near the
        # OK threshold. The dashboard polls compare continuously, so the test
        # polls too: the verdicts must settle on OK.
        deadline = time.monotonic() + 6.0
        while True:
            r = client.get("/api/calibration/compare",
                           params={"ref_id": "active"})
            assert r.status_code == 200
            assert r.json()["ref_id"] == ref_id
            assert {c["cam_id"] for c in r.json()["cameras"]} == {"cam1", "cam2"}
            if all(c["verdict"] == "OK" for c in r.json()["cameras"]):
                break
            assert time.monotonic() < deadline, [
                (c["cam_id"], c["verdict"], c["dy_px"]) for c in r.json()["cameras"]]
            time.sleep(0.5)  # > COMPARE_MIN_INTERVAL_S: every poll computes fresh

        r = client.get("/api/calibration/compare")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid"

        url = ref["thumb_urls"]["fake:0"]
        r = client.get(url)
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        r = client.get(url.replace(".jpg", ".edges.png"))
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"

        r = client.post("/api/calibration/deactivate")
        assert r.status_code == 200
        r = client.delete(f"/api/calibration/references/{ref_id}")
        assert r.status_code == 200
        r = client.get(url)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"


def test_compare_unknown_reference_404(selected):
    with TestClient(_make_app(selected)) as client:
        r = client.get("/api/calibration/compare", params={"ref_id": "nope"})
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"


def test_fake_service_gets_clean_not_found(ssd):
    """The in-memory FakeCameraService has no previews or references: the
    routes must answer not_found instead of crashing."""
    from master_backend.app.camera.fake import FakeCameraService

    fake = FakeCameraService(n=1)
    with TestClient(_make_app(fake)) as client:
        assert client.get("/api/cameras/devices").status_code == 200
        r = client.get("/api/cameras/cam1/latest.jpg")
        assert r.status_code == 404
        r = client.post("/api/calibration/references", json={})
        assert r.status_code == 404
        r = client.get("/api/calibration/compare", params={"ref_id": "active"})
        assert r.status_code == 404
    asyncio.run(fake.close())
