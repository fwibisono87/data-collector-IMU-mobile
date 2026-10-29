"""Unit tests for the v3 runtime layer: notice store, draft store, camera factory,
lifecycle wiring and the watchdog checks (L4 spec §2–§3)."""
from __future__ import annotations

import json

import pytest

from conftest import make_rig
from master_backend.app import cameras as cameras_mod
from master_backend.app.api import runtime as runtime_mod
from master_backend.app.api.runtime import (
    DraftStore,
    NoticeStore,
    check_disk,
    check_rescue_uploads,
    create_camera_service,
    create_runtime,
    wire_lifecycle,
)
from master_backend.app.camera.fake import FakeCameraService


@pytest.fixture
def store_root(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# notice store
# ---------------------------------------------------------------------------

def test_notice_raise_is_idempotent_on_key(store_root):
    store = NoticeStore(lambda: store_root)
    first = store.raise_("disk_space", "warn", "20 GB left", key="disk_space")
    second = store.raise_("disk_space", "warn", "19 GB left", key="disk_space")
    assert first["id"] == second["id"]
    snapshot = store.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].message == "19 GB left"
    # an unkeyed raise always creates a new notice
    store.raise_("one_off", "info", "no key")
    store.raise_("one_off", "info", "no key")
    assert len(store.snapshot()) == 3


def test_notice_reraise_updates_level(store_root):
    store = NoticeStore(lambda: store_root)
    store.raise_("disk_space", "warn", "19 GB left", key="disk_space")
    store.raise_("disk_space", "error", "4 GB left", key="disk_space")
    snapshot = store.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].level == "error"


def test_notice_resolve_then_reraise_creates_fresh(store_root):
    store = NoticeStore(lambda: store_root)
    first = store.raise_("rescue", "warn", "outstanding", key="rescue:1")
    assert store.resolve("rescue:1") == 1
    assert store.snapshot() == []
    second = store.raise_("rescue", "warn", "outstanding again", key="rescue:1")
    assert second["id"] != first["id"]
    assert [n.message for n in store.snapshot()] == ["outstanding again"]
    assert store.resolve("never_used") == 0


def test_notice_dismiss_hides_but_keeps_record(store_root):
    store = NoticeStore(lambda: store_root)
    record = store.raise_("camera_track_ended", "warn", "cam1 dropped", key="c:1")
    assert store.dismiss(record["id"]) is True
    assert store.dismiss("nope") is False
    assert store.snapshot() == []
    kept = store.get(record["id"])
    assert kept is not None
    assert kept["resolved"] is False
    assert kept["dismissed_at_ms"] is not None


def test_notice_persistence_across_reload(store_root):
    store = NoticeStore(lambda: store_root)
    record = store.raise_("late_delivery", "info", "5 rows", session_id="s1", key="late:s1")
    store.raise_("other", "warn", "still open", key="other")
    store.dismiss(record["id"])

    reloaded = NoticeStore(lambda: store_root)
    snapshot = reloaded.snapshot()
    assert [n.code for n in snapshot] == ["other"]
    assert reloaded.get(record["id"])["dismissed_at_ms"] is not None


def test_notice_corrupt_file_starts_empty(store_root):
    (store_root / "notices.json").write_text("{not json", encoding="utf-8")
    store = NoticeStore(lambda: store_root)
    assert store.snapshot() == []


def test_notice_count_for_session(store_root):
    store = NoticeStore(lambda: store_root)
    store.raise_("a", "info", "x", session_id="s1", key="a")
    store.raise_("b", "info", "y", session_id="s1", key="b")
    store.raise_("c", "info", "z", session_id="s2", key="c")
    store.raise_("d", "info", "w", key="d")  # no session
    store.resolve("b")
    assert store.count_for_session("s1") == 1
    assert store.count_for_session("s2") == 1
    assert store.count_for_session("s3") == 0


def test_notice_bad_level_raises(store_root):
    store = NoticeStore(lambda: store_root)
    with pytest.raises(ValueError):
        store.raise_("x", "critical", "boom")


def test_notice_listener_fires_on_change_only(store_root):
    store = NoticeStore(lambda: store_root)
    hits: list[bool] = []
    store.add_listener(lambda: hits.append(True))
    store.raise_("k", "info", "m", key="k")
    store.raise_("k", "info", "m", key="k")  # identical → no commit, no notify
    store.raise_("k", "info", "m2", key="k")  # changed → notify
    assert len(hits) == 2


# ---------------------------------------------------------------------------
# draft store
# ---------------------------------------------------------------------------

def test_draft_defaults_and_merge(store_root):
    store = DraftStore(lambda: store_root)
    draft = store.get()
    assert (draft.subject, draft.session_tag, draft.operator) == ("", "", "")
    assert draft.updated_at_ms == 0

    store.update(subject="Alice")
    store.update(session_tag="walk", operator="bob")
    draft = store.get()
    assert (draft.subject, draft.session_tag, draft.operator) == ("Alice", "walk", "bob")
    assert draft.updated_at_ms > 0


def test_draft_clear_subject_keeps_tag_operator(store_root):
    store = DraftStore(lambda: store_root)
    store.update(subject="Alice", session_tag="walk", operator="bob")
    store.clear_subject()
    draft = store.get()
    assert draft.subject == ""
    assert draft.session_tag == "walk"
    assert draft.operator == "bob"


def test_draft_persistence_across_reload(store_root):
    store = DraftStore(lambda: store_root)
    store.update(subject="Alice", session_tag="walk", operator="bob")
    reloaded = DraftStore(lambda: store_root)
    draft = reloaded.get()
    assert (draft.subject, draft.session_tag, draft.operator) == ("Alice", "walk", "bob")
    # the file is plain JSON, atomically written
    raw = json.loads((store_root / "draft.json").read_text(encoding="utf-8"))
    assert raw["subject"] == "Alice"


# ---------------------------------------------------------------------------
# camera service factory
# ---------------------------------------------------------------------------

def test_camera_factory_modes(monkeypatch):
    monkeypatch.setenv("IMU_CAMERAS", "stub")
    monkeypatch.setenv("IMU_FAKE_CAMERAS", "3")
    svc = create_camera_service()
    assert isinstance(svc, FakeCameraService)
    assert len(svc.list_devices()) == 3

    monkeypatch.setenv("IMU_FAKE_CAMERAS", "0")
    assert len(create_camera_service().list_devices()) == 0

    monkeypatch.setenv("IMU_CAMERAS", "off")
    assert len(create_camera_service().list_devices()) == 0

    # auto (and an unknown mode, which falls back to auto) builds the real ffmpeg-backed
    # service now that the camera lane has landed. Do not enumerate: that depends on
    # which webcams the test machine happens to have.
    from master_backend.app.camera.service import RealCameraService
    for mode in ("auto", "fake", "banana"):
        monkeypatch.setenv("IMU_CAMERAS", mode)
        svc = create_camera_service()
        try:
            assert isinstance(svc, RealCameraService)
        finally:
            import asyncio
            asyncio.run(svc.close())

    monkeypatch.delenv("IMU_CAMERAS")
    monkeypatch.setenv("IMU_FAKE_CAMERAS", "not_a_number")
    monkeypatch.setenv("IMU_CAMERAS", "stub")
    assert len(create_camera_service().list_devices()) == 2


def test_camera_router_is_mounted():
    # The camera lane's /api/cameras and /api/calibration routes are present.
    router = runtime_mod.camera_router_or_none()
    assert router is not None
    paths = {r.path for r in router.routes}
    assert "/api/cameras/devices" in paths
    assert any(p.startswith("/api/calibration/") for p in paths)


# ---------------------------------------------------------------------------
# lifecycle wiring (notice sources)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wire_finalize_failure_and_late_delivery_notices(tmp_path, monkeypatch):
    with make_rig(tmp_path, monkeypatch) as rig:
        rt = create_runtime(root=tmp_path / "state", camera_service=FakeCameraService(n=0))
        unwire = wire_lifecycle(rt)
        try:
            await rig.manager._notify({
                "type": "FINALIZE_COMPLETE",
                "session_id": "s1",
                "finalize": {"steps": [
                    {"step": "close_writers", "state": "done"},
                    {"step": "validate", "state": "failed", "detail": "boom"},
                ]},
            })
            await rig.manager._notify({
                "type": "LATE_DELIVERY",
                "session_id": "s1",
                "devices": {"dev-1": {"rows_appended": 5}},
            })
            snapshot = rt.notices.snapshot()
            by_code = {n.code: n for n in snapshot}
            assert by_code["finalize_step_failed"].level == "error"
            assert "validate" in by_code["finalize_step_failed"].message
            assert "boom" in by_code["finalize_step_failed"].message
            assert by_code["late_delivery"].level == "info"
            assert "5 row(s)" in by_code["late_delivery"].message
            # only the failed step raised, and the session is now watched
            assert sum(1 for n in snapshot if n.code == "finalize_step_failed") == 1
            assert "s1" in rt.recent_sessions
        finally:
            unwire()


@pytest.mark.asyncio
async def test_wire_camera_mark_hook_notices(tmp_path, monkeypatch):
    with make_rig(tmp_path, monkeypatch) as rig:
        rt = create_runtime(root=tmp_path / "state", camera_service=FakeCameraService(n=0))
        unwire = wire_lifecycle(rt)
        try:
            await cameras_mod.cameras_mark("s1", {
                "cam_id": "cam1", "event": "track_ended", "ts_ms": 123,
                "device_id": "fake:0", "label": "Fake", "mime": "video/webm",
            })
            await cameras_mod.cameras_mark("s1", {
                "cam_id": "cam1", "event": "started", "ts_ms": 100,
                "device_id": "fake:0", "label": "Fake", "mime": "video/webm",
            })
            snapshot = rt.notices.snapshot()
            assert [n.code for n in snapshot] == ["camera_track_ended"]
            assert snapshot[0].level == "warn"
            assert snapshot[0].session_id == "s1"
        finally:
            unwire()
        assert cameras_mod._mark_hooks == []  # unwire removed the hook


def test_check_disk_raises_and_resolves(tmp_path, monkeypatch):
    rt = create_runtime(root=tmp_path / "state", camera_service=FakeCameraService(n=0))
    monkeypatch.setattr(runtime_mod, "DISK_WARN_BYTES", 10**18)
    monkeypatch.setattr(runtime_mod, "DISK_ERROR_BYTES", 1)
    check_disk(rt)
    assert [n.level for n in rt.notices.snapshot()] == ["warn"]

    monkeypatch.setattr(runtime_mod, "DISK_ERROR_BYTES", 10**18)
    check_disk(rt)
    snapshot = rt.notices.snapshot()
    assert len(snapshot) == 1  # same notice, escalated in place
    assert snapshot[0].level == "error"

    monkeypatch.setattr(runtime_mod, "DISK_WARN_BYTES", 1)
    monkeypatch.setattr(runtime_mod, "DISK_ERROR_BYTES", 1)
    check_disk(rt)
    assert rt.notices.snapshot() == []


@pytest.mark.asyncio
async def test_check_rescue_uploads_outstanding_then_resolved(tmp_path, monkeypatch):
    with make_rig(tmp_path, monkeypatch) as rig:
        rt = create_runtime(root=tmp_path / "state", camera_service=FakeCameraService(n=0))
        rt.note_session("s1")

        recovery_dir = rig.recovery_path / "s1"
        recovery_dir.mkdir(parents=True)
        info = {
            "session_id": "s1", "device_id": "dev-1", "role": "chest",
            "total_bytes": 100, "received_bytes": 40, "sha256": "",
            "complete": False, "sha256_verified": False, "done": False,
        }
        (recovery_dir / "dev-1.info.json").write_text(json.dumps(info), encoding="utf-8")

        check_rescue_uploads(rt)
        snapshot = rt.notices.snapshot()
        assert [n.code for n in snapshot] == ["rescue_outstanding"]
        assert snapshot[0].level == "warn"

        # still unverified → same notice, no duplicate
        check_rescue_uploads(rt)
        assert len(rt.notices.snapshot()) == 1

        # Completed + verified is not enough: the rows are still missing from the ZIP, so
        # the notice stays up and says exactly that.
        (recovery_dir / "dev-1.csv").write_bytes(b"x" * 100)
        info.update({"complete": True, "sha256_verified": True, "received_bytes": 100})
        (recovery_dir / "dev-1.info.json").write_text(json.dumps(info), encoding="utf-8")
        check_rescue_uploads(rt)
        snapshot = rt.notices.snapshot()
        assert [n.code for n in snapshot] == ["rescue_outstanding"]
        assert "not merged into the session ZIP yet" in snapshot[0].message

        # Once a consolidation covers the upload's role, the notice resolves.
        folder = rig.ssd_path / "Data_Riset_IMU" / "Subject_Tag"
        folder.mkdir(parents=True)
        (folder / "s1_chest_consolidated.csv").write_text("timestamp_ms\n", encoding="utf-8")
        check_rescue_uploads(rt)
        assert rt.notices.snapshot() == []


def test_create_runtime_makes_state_dirs(tmp_path):
    root = tmp_path / "deep" / "state"
    rt = create_runtime(root=root, camera_service=FakeCameraService(n=1))
    assert (root / "sessions").is_dir()
    assert rt.state_root() == root
    assert rt.hub is None and rt.post is None


def test_recent_sessions_bounded(tmp_path):
    rt = create_runtime(root=tmp_path / "state", camera_service=FakeCameraService(n=0))
    for i in range(40):
        rt.note_session(f"s{i}")
    assert len(rt.recent_sessions) == 16
    assert rt.recent_sessions[-1] == "s39"
    rt.note_session("s39")  # idempotent
    assert len(rt.recent_sessions) == 16


def test_check_disk_notice_names_the_data_root_abbreviated(tmp_path, monkeypatch):
    """L7a §3.2: the notice points at SSD_PATH (short form), never at the state dir."""
    from master_backend.app import export as export_mod
    from master_backend.app.api.readiness import short_path

    ssd = tmp_path / "mnt" / "ssd" / "Data_Riset_IMU"
    ssd.mkdir(parents=True)
    monkeypatch.setattr(export_mod, "SSD_PATH", ssd)
    monkeypatch.setattr(runtime_mod, "DISK_WARN_BYTES", 10**18)

    rt = create_runtime(root=tmp_path / "state", camera_service=FakeCameraService(n=0))
    check_disk(rt)

    notice = rt.notices.snapshot()[0]
    assert short_path(ssd) in notice.message
    assert "_state" not in notice.message
