"""v3 REST + /ws/ui (API.md): the dashboard's whole contract, end to end.

Each test starts the real app (lifespan incl. the v3 runtime) against the shared in-process
rig, with the fake camera service injected by ``IMU_CAMERAS=fake``.
"""
from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from master_backend.app.api import runtime as runtime_module
from master_backend.app.api.schema import CameraSelectionItem

from conftest import FakeWs

SID_PLACEHOLDER = "placeholder"


@pytest.fixture
def api(rig, monkeypatch):
    monkeypatch.setenv("IMU_CAMERAS", "fake")
    monkeypatch.setenv("IMU_FAKE_CAMERAS", "2")
    from master_backend.app.main import app

    with TestClient(app) as client:
        rt = runtime_module.get_runtime()
        rt.camera_service.set_selection([
            CameraSelectionItem(device_key="fake:0"),
            CameraSelectionItem(device_key="fake:1"),
        ])
        yield client, rt


def register_phone(rig, device_id="dev-chest", role="chest"):
    rig.manager.register_device(
        device_id=device_id, role=role, model="Pixel", app_version="2.0.0",
        ws=FakeWs(),
    )


def wait_for_package(client, session_id, timeout=30.0) -> dict:
    deadline = time.monotonic() + timeout
    detail = {}
    while time.monotonic() < deadline:
        detail = client.get(f"/api/sessions/{session_id}").json()
        statuses = {s["name"]: s["status"] for s in detail.get("finalize", [])}
        if statuses.get("package") in ("done", "failed", "skipped"):
            return detail
        time.sleep(0.1)
    raise AssertionError(f"package never settled: {detail}")


def fill_draft(client):
    response = client.put("/api/session/draft", json={
        "subject": "Sub ject", "session_tag": "Tag A", "operator": "Op",
    })
    assert response.status_code == 200, response.text
    return response.json()


# ── snapshot and draft ─────────────────────────────────────────────────────

def test_state_is_the_snapshot_shape_and_tracks_the_draft(api):
    client, rt = api
    state = client.get("/api/state")
    assert state.status_code == 200
    body = state.json()
    assert body["state"] == "IDLE"
    assert body["session"] is None
    assert body["draft"] == {
        "subject": "", "session_tag": "", "operator": "", "updated_at_ms": 0,
    }
    assert body["phones"] == []
    assert isinstance(body["readiness"]["blockers"], list)
    assert body["disk"]["free_bytes"] > 0

    draft = fill_draft(client)
    assert (draft["subject"], draft["session_tag"], draft["operator"]) == (
        "Sub ject", "Tag A", "Op",
    )
    # Merging keeps fields the client did not send.
    merged = client.put("/api/session/draft", json={"subject": "Other"}).json()
    assert merged["subject"] == "Other"
    assert merged["session_tag"] == "Tag A"

    state = client.get("/api/state").json()
    assert state["draft"]["subject"] == "Other"


# ── START / label / STOP / package ─────────────────────────────────────────

def test_start_blocked_then_ok_then_label_stop_and_package(api, rig):
    client, rt = api
    register_phone(rig)

    refused = client.post("/api/session/start", json={})
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "blocked"
    codes = {issue["code"] for issue in error["details"]["blockers"]}
    assert {"subject_missing", "session_tag_missing", "operator_missing"} <= codes

    fill_draft(client)
    started = client.post("/api/session/start", json={})
    assert started.status_code == 200, started.text
    body = started.json()
    session_id = body["session_id"]
    assert session_id
    assert body["scheduled_start_ms"] >= int(session_id)
    assert body["warnings"] is not None
    # The subject is consumed, tag/operator stay for the next session.
    draft = client.get("/api/state").json()["draft"]
    assert draft["subject"] == "" and draft["operator"] == "Op"

    state = client.get("/api/state").json()
    assert state["state"] == "RECORDING"
    assert state["session"]["session_id"] == session_id
    assert state["session"]["subject"] == "Sub ject"
    assert len(state["cameras"]) == 2
    assert all(camera["recording"] for camera in state["cameras"])

    labelled = client.post("/api/session/label", json={"label_id": 7})
    assert labelled.status_code == 200, labelled.text
    assert labelled.json()["label_id"] == 7
    assert labelled.json()["since_ms"] > 0

    for i in range(20):
        rig.manager.increment_packets("dev-chest")

    stopped = client.post("/api/session/stop", json={"reason": "operator_stop"})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json() == {"session_id": session_id}
    # Idempotent for 10 s: the same body, not a 409.
    again = client.post("/api/session/stop", json={})
    assert again.status_code == 200
    assert again.json() == stopped.json()

    detail = wait_for_package(client, session_id)
    statuses = {step["name"]: step["status"] for step in detail["finalize"]}
    assert statuses["package"] == "done", detail
    assert statuses["cameras"] == "done"
    assert detail["verdict"] in ("PASS", "PARTIAL", "FAIL")
    assert detail["zip_path"] and Path(detail["zip_path"]).is_file()
    assert detail["zip_bytes"] == Path(detail["zip_path"]).stat().st_size
    assert detail["files"]
    assert any(f["name"].startswith(session_id) for f in detail["files"])

    # The deliverable is the browser-shaped ZIP, videos included.
    with zipfile.ZipFile(detail["zip_path"]) as archive:
        names = archive.namelist()
    assert f"videos/{session_id}_cam1_video_sync.webm" in names
    assert "data/manifest.json" in names

    listed = client.get("/api/sessions").json()
    assert [s["session_id"] for s in listed["sessions"]] == [session_id]
    assert listed["next"] is None
    assert listed["sessions"][0]["cameras"] == ["cam1", "cam2"]

    # Full download and a byte range (the dashboard resumes interrupted downloads).
    full = client.get(f"/api/sessions/{session_id}/zip")
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-disposition"].startswith("attachment; filename=")
    assert full.content == Path(detail["zip_path"]).read_bytes()

    partial = client.get(f"/api/sessions/{session_id}/zip", headers={"Range": "bytes=0-9"})
    assert partial.status_code == 206
    assert partial.content == full.content[:10]
    assert partial.headers["content-range"] == f"bytes 0-9/{len(full.content)}"

    bad_range = client.get(
        f"/api/sessions/{session_id}/zip",
        headers={"Range": f"bytes={len(full.content) + 5}-"},
    )
    assert bad_range.status_code == 416

    rebuilt = client.post(f"/api/sessions/{session_id}/package")
    assert rebuilt.status_code == 200
    assert rebuilt.json()["zip_path"] == detail["zip_path"]


def test_label_outside_a_session_is_bad_state(api):
    client, _rt = api
    response = client.post("/api/session/label", json={"label_id": 3})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "bad_state"


def test_stop_without_recording_is_bad_state(api):
    client, _rt = api
    response = client.post("/api/session/stop", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "bad_state"


# ── errors ─────────────────────────────────────────────────────────────────

def test_error_bodies_match_api_md(api):
    client, _rt = api

    missing = client.get("/api/sessions/1700000000001")
    assert missing.status_code == 404
    assert missing.json() == {"error": {
        "code": "not_found",
        "message": "session 1700000000001 was not found",
        "details": None,
    }}

    invalid = client.post("/api/session/label", json={"label_id": 999})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid"
    assert invalid.json()["error"]["details"]["errors"]

    unknown = client.get("/api/does-not-exist")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"

    dismissed = client.post("/api/notices/nope/dismiss")
    assert dismissed.status_code == 404
    assert dismissed.json()["error"]["code"] == "not_found"


def test_dismiss_and_reset(api, rig):
    client, rt = api
    register_phone(rig)
    notice = rt.notices.raise_(
        code="test", level="warn", message="Something to dismiss", key="test:1",
    )
    state = client.get("/api/state").json()
    # Other notices may legitimately exist (e.g. the disk-space watchdog); this one must.
    assert notice["id"] in {n["id"] for n in state["notices"]}

    assert client.post(f"/api/notices/{notice['id']}/dismiss").json() == {}
    remaining = client.get("/api/state").json()["notices"]
    assert notice["id"] not in {n["id"] for n in remaining}

    assert client.post("/api/phones/reset").json() == {}
    assert rig.manager.online_devices == []


# ── static dashboard ───────────────────────────────────────────────────────

def test_missing_dashboard_build_serves_the_builtin_page(api):
    client, _rt = api
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Dashboard not built" in response.text
    # SPA fallback for unknown page paths, but never for backend prefixes.
    assert "Dashboard not built" in client.get("/sessions/anything").text
    assert client.get("/api/nothing-here").json()["error"]["code"] == "not_found"


# ── /ws/ui ─────────────────────────────────────────────────────────────────

def test_ws_ui_snapshot_draft_ping_and_calibration(api):
    client, _rt = api
    with client.websocket_connect("/ws/ui") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert first["data"]["state"] == "IDLE"

        ws.send_json({"type": "ping", "req_id": "p1"})
        ack = None
        for _ in range(30):
            message = ws.receive_json()
            if message["type"] == "ack":
                ack = message
                break
        assert ack == {"type": "ack", "req_id": "p1", "ok": True, "error": None}

        # A draft change reaches the socket as a fresh whole snapshot within ~1 s.
        fill_draft(client)
        updated = None
        for _ in range(40):
            message = ws.receive_json()
            if message["type"] == "snapshot" and message["data"]["draft"]["subject"] == "Sub ject":
                updated = message
                break
        assert updated is not None
        # Everything else is in the same snapshot; the dashboard needs no patches.
        assert "readiness" in updated["data"] and "disk" in updated["data"]

        # No camera service compare() here: report that calibration is unavailable.
        ws.send_json({"type": "calibration.watch", "ref_id": "active", "req_id": "c1"})
        ack = None
        for _ in range(30):
            message = ws.receive_json()
            if message["type"] == "ack" and message.get("req_id") == "c1":
                ack = message
                break
        assert ack is not None and ack["ok"] is False
        assert ack["error"]["code"] == "not_found"

# ── restart ────────────────────────────────────────────────────────────────

def test_restart_finalises_and_packages_an_interrupted_session(rig, monkeypatch):
    """The §8 restart case, through the real lifespan.

    The backend is killed mid-session: the ledger still says RECORDING and a post record
    exists with the cameras step not finished and no archive. The next startup must recover
    the session, finish the post chain and leave a downloadable ZIP.
    """
    import asyncio

    monkeypatch.setenv("IMU_CAMERAS", "fake")
    monkeypatch.setenv("IMU_FAKE_CAMERAS", "1")

    async def crash_mid_session() -> str:
        from master_backend.app.api.post_session import PostSessionJob
        from master_backend.app.api.runtime import create_runtime
        from conftest import start_session, stream

        session_id = await start_session(rig)
        await stream(rig, "dev-chest", 30)
        job = PostSessionJob(create_runtime())
        await job.on_stopped(session_id)      # the record exists; package waits on finalize
        await asyncio.sleep(0.2)
        await job.close()                     # the process dies here
        return session_id

    session_id = asyncio.run(crash_mid_session())
    post_record = rig.ssd_path / "Data_Riset_IMU" / "_state" / "sessions" / f"{session_id}.post.json"
    assert post_record.is_file()
    assert json.loads(post_record.read_text(encoding="utf-8"))["steps"]["package"]["status"] != "done"
    assert (rig.manager.get_recovery_session(session_id) or {}).get("state") == "RECORDING"

    from master_backend.app.main import app

    with TestClient(app) as client:
        detail = wait_for_package(client, session_id, timeout=90)
        statuses = {step["name"]: step["status"] for step in detail["finalize"]}
        assert statuses["package"] == "done", detail
        archive = Path(detail["zip_path"])
        assert archive.is_file()
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
        assert "data/manifest.json" in names
        assert f"data/{session_id}_chest_sensor_data.csv" in names
        # The session is terminal now, so a second restart must not repackage it again.
        ledger = client.get(f"/api/sessions/{session_id}").json()
        assert ledger["verdict"] in ("PASS", "PARTIAL", "FAIL")


def test_shutdown_during_recording_stops_the_cameras_and_builds_the_zip(rig, monkeypatch):
    """A clean restart must not leave a finished session without its deliverable.

    Shutdown finalises a RECORDING session outside the REST stop handler, so it has to
    start the post-session chain itself; otherwise the ledger is terminal when the next
    startup looks for interrupted sessions and nothing ever packages it.
    """
    monkeypatch.setenv("IMU_CAMERAS", "fake")
    monkeypatch.setenv("IMU_FAKE_CAMERAS", "1")

    from master_backend.app.main import app

    with TestClient(app) as client:
        runtime_module.get_runtime().camera_service.set_selection(
            [CameraSelectionItem(device_key="fake:0")]
        )
        register_phone(rig)
        fill_draft(client)
        started = client.post("/api/session/start", json={})
        assert started.status_code == 200, started.text
        session_id = started.json()["session_id"]
        folder = rig.ssd_path / "Data_Riset_IMU" / "Sub_ject_Tag_A"
        assert folder.is_dir()

    # The context manager ran the lifespan shutdown: finalize, camera stop, package.
    assert (rig.manager.get_recovery_session(session_id) or {}).get("terminal") is True
    archives = list(folder.glob(f"*_{session_id}.zip"))
    assert len(archives) == 1, [p.name for p in folder.iterdir()]
    with zipfile.ZipFile(archives[0]) as zf:
        names = zf.namelist()
    assert "data/manifest.json" in names
    assert any(name.startswith("videos/") for name in names)


def test_open_folder_is_501_on_an_unsupported_platform(api, rig, monkeypatch):
    """API.md: explorer on Windows, xdg-open on Linux, 501 everywhere else."""
    import sys

    client, _rt = api
    session_id = "1700000000123"
    folder = rig.ssd_path / "Data_Riset_IMU" / "Sub_Tag"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{session_id}_chest_sensor_data.csv").write_text("h\n1\n", encoding="utf-8")

    monkeypatch.setattr(sys, "platform", "darwin")
    refused = client.post(f"/api/sessions/{session_id}/open-folder")
    assert refused.status_code == 501
    assert refused.json()["error"]["code"] == "invalid"
    # And an unknown session is still a 404, not a 501.
    assert client.post("/api/sessions/1700000000999/open-folder").status_code == 404
