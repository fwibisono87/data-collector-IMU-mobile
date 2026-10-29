"""Readiness rules and Snapshot state mapping (L4 spec §6, §8)."""
from __future__ import annotations

import pytest

from master_backend.app.api import readiness as readiness_module
from master_backend.app.api.readiness import compute_readiness
from master_backend.app.api.runtime import create_runtime
from master_backend.app.api.schema import CameraSelectionItem
from master_backend.app.api.snapshot import build_snapshot
from master_backend.app.camera.fake import FakeCameraService
from master_backend.app.session_manager import SessionState

from conftest import FakeWs

GB = 1024**3


def make_rt(rig, cameras: int = 1):
    service = FakeCameraService(cameras)
    if cameras:
        service.set_selection([CameraSelectionItem(device_key=f"fake:{i}")
                               for i in range(cameras)])
    rt = create_runtime(camera_service=service)
    rt.drafts.update(subject="Sub", session_tag="Tag", operator="Op")
    return rt, service


def phone(rig, device_id="dev-chest", role="chest", version="2.0.0"):
    rig.manager.register_device(
        device_id=device_id, role=role, model="Pixel", app_version=version,
        ws=FakeWs(),
    )


@pytest.fixture
def disk(monkeypatch):
    """Control the writability probe and free space; defaults are healthy."""
    state = {"writable": True, "free": 100 * GB}

    monkeypatch.setattr(readiness_module, "_writable", lambda _anchor: state["writable"])
    monkeypatch.setattr(readiness_module, "_free_bytes", lambda _anchor: state["free"])
    return state


def codes(issues) -> set[str]:
    return {issue.code for issue in issues}


@pytest.mark.parametrize(
    "writable, free, blockers, warnings",
    [
        (False, 100 * GB, {"disk_not_writable"}, set()),
        (True, 4 * GB, {"disk_full"}, set()),
        (True, 10 * GB, set(), {"disk_low"}),
        (True, 100 * GB, set(), set()),
    ],
)
def test_disk_rules(rig, disk, writable, free, blockers, warnings):
    rt, _ = make_rt(rig)
    phone(rig)
    disk.update(writable=writable, free=free)
    readiness = compute_readiness(rt)
    assert codes(readiness.blockers) == blockers
    assert codes(readiness.warnings) == warnings


def test_no_phone_blocks(rig, disk):
    rt, _ = make_rt(rig)
    readiness = compute_readiness(rt)
    assert "no_phone" in codes(readiness.blockers)


def test_missing_draft_fields_block_and_name_the_field(rig, disk):
    rt, _ = make_rt(rig)
    phone(rig)
    rt.drafts.update(subject="", session_tag=" ", operator="")
    readiness = compute_readiness(rt)
    missing = [i for i in readiness.blockers if i.code.endswith("_missing")]
    assert codes(missing) == {"subject_missing", "session_tag_missing", "operator_missing"}
    assert {i.subject for i in missing} == {"subject", "session_tag", "operator"}
    # The message must say what to do, not just that it is wrong.
    assert all("required" in issue.message for issue in missing)


@pytest.mark.parametrize(
    "state, expected",
    [
        (SessionState.IDLE, None),
        (SessionState.PREFLIGHT, None),
        (SessionState.READY, None),
        (SessionState.RECORDING, "already_recording"),
        (SessionState.FINALIZING, "finalizing"),
        (SessionState.VALIDATING, "finalizing"),
        (SessionState.ERROR, "backend_error"),
    ],
)
def test_state_rules(rig, disk, state, expected):
    rt, _ = make_rt(rig)
    phone(rig)
    rig.manager.state = state
    found = codes(compute_readiness(rt).blockers)
    if expected is None:
        assert not ({"already_recording", "finalizing", "backend_error"} & found)
    else:
        assert expected in found


def test_camera_readiness_is_merged(rig, disk):
    service = FakeCameraService(1)             # a known device, but nothing selected yet
    rt = create_runtime(camera_service=service)
    rt.drafts.update(subject="Sub", session_tag="Tag", operator="Op")
    phone(rig)
    readiness = compute_readiness(rt)
    assert "no_camera_selected" in codes(readiness.blockers)

    service.set_selection([CameraSelectionItem(device_key="fake:0")])
    assert "no_camera_selected" not in codes(compute_readiness(rt).blockers)


def test_streaming_rate_and_version_warnings(rig, disk):
    rt, _ = make_rt(rig)
    phone(rig, "dev-chest", "chest", version="2.0.0")
    phone(rig, "dev-waist", "waist", version="1.9.0")
    chest = rig.manager.get_device("dev-chest")
    chest.packets_received = 100
    chest.true_hz_avg = 60.0
    warnings = compute_readiness(rt).warnings
    assert "phone_rate_low" in codes(warnings)
    assert "app_version_mismatch" in codes(warnings)
    assert next(i for i in warnings if i.code == "phone_rate_low").subject == "dev-chest"


# ── snapshot state mapping ─────────────────────────────────────────────────

def test_snapshot_maps_preflight_and_ready_to_idle(rig):
    rt, _ = make_rt(rig)
    for state in (SessionState.PREFLIGHT, SessionState.READY):
        rig.manager.state = state
        snapshot = build_snapshot(rt)
        assert snapshot.state == "IDLE"
        assert snapshot.session is None


def test_snapshot_has_the_recording_session(rig):
    rt, _ = make_rt(rig)
    rig.manager.state = SessionState.RECORDING
    rig.manager.session_id = "1700000000000"
    rig.manager.subject_name = "Sub"
    rig.manager.session_tag = "Tag"
    rig.manager.operator = "Op"
    rig.manager.scheduled_start_ms = 1700000000500
    rig.manager._recording_started_epoch_ms = 1700000000500
    snapshot = build_snapshot(rt)
    assert snapshot.state == "RECORDING"
    assert snapshot.session.session_id == "1700000000000"
    assert snapshot.session.subject == "Sub"
    assert [c.cam_id for c in snapshot.cameras] == ["cam1"]


def test_snapshot_keeps_the_last_session_for_the_done_view(rig):
    rt, _ = make_rt(rig)
    rig.manager.state = SessionState.IDLE
    rig.manager.session_id = "1700000000000"
    snapshot = build_snapshot(rt)
    assert snapshot.state == "IDLE"
    assert snapshot.session is None
    assert snapshot.last_session_id == "1700000000000"
    # The draft is the server's copy, so every browser shows the same Setup form.
    assert snapshot.draft.subject == "Sub"


def test_snapshot_marks_an_offline_phone_as_buffering_while_recording(rig):
    rt, _ = make_rt(rig)
    phone(rig)
    dev = rig.manager.get_device("dev-chest")
    dev.control_ws = None                    # telemetry-only / dropped control socket
    rig.manager.state = SessionState.RECORDING
    snapshot = build_snapshot(rt)
    assert snapshot.phones[0].buffering is True
    rig.manager.state = SessionState.IDLE
    assert build_snapshot(rt).phones[0].buffering is False

# ── L7a §3.2: disk messages name the data root, abbreviated ────────────────

def test_short_path_keeps_the_last_two_components():
    from pathlib import Path

    from master_backend.app.api.readiness import short_path

    assert short_path(Path("/mnt/ssd/Data_Riset_IMU")) == "…/ssd/Data_Riset_IMU"
    assert short_path(Path("/data")) == "/data"


def test_disk_messages_name_the_data_root_not_the_state_dir(rig, disk, monkeypatch):
    from master_backend.app import export as export_mod
    from master_backend.app.api.readiness import short_path

    (rig.ssd_path / "Data_Riset_IMU" / "_state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(export_mod, "SSD_PATH", rig.ssd_path)
    rt, _ = make_rt(rig)
    phone(rig)
    disk.update(free=4 * GB)

    issue = next(i for i in compute_readiness(rt).blockers if i.code == "disk_full")
    assert short_path(rig.ssd_path) in issue.message
    assert "_state" not in issue.message
    # The full path is not lost: it is the issue's subject (and the snapshot's disk.path).
    assert issue.subject == str(rig.ssd_path)
