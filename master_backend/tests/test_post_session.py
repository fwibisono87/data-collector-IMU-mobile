"""Post-session job (L4 spec §4): camera step, package step, debounce and resume.

The job is deliberately separate from FinalizeJob, so nothing here touches the frozen
ledger format; these tests drive it directly against the shared in-process rig.
"""
from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import pytest

from master_backend.app.api import post_session as post_module
from master_backend.app.api.post_session import (
    PostBusy,
    PostSessionJob,
    _part_index,
    _video_files,
    session_progress,
)
from master_backend.app.api.runtime import create_runtime
from master_backend.app.api.schema import CameraSelectionItem
from master_backend.app.camera.fake import FakeCameraService

from conftest import start_session, stream

SID = "1700000000000"


class StepClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


async def settle(job: PostSessionJob, session_id: str, *, timeout: float = 20.0) -> None:
    """Wait for the background chain this job started for ``session_id``."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        task = job._tasks.get(session_id)
        if task is not None and task.done():
            return
        if task is None and session_id not in job._running:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"post-session job for {session_id} did not settle")


def camera_runtime(rig, n: int = 2, clock: StepClock | None = None):
    service = FakeCameraService(n, clock=clock or StepClock())
    rt = create_runtime(camera_service=service)
    job = PostSessionJob(rt)
    return service, rt, job


def zip_path_for(session_id: str, folder: Path) -> Path:
    found = list(folder.glob(f"*_{session_id}.zip"))
    assert len(found) == 1, [p.name for p in folder.iterdir()]
    return found[0]


def zip_entries(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


@pytest.mark.asyncio
async def test_stop_runs_cameras_then_packages_videos_and_data(rig):
    service, rt, job = camera_runtime(rig, 2)
    service.set_selection([
        CameraSelectionItem(device_key="fake:0"),
        CameraSelectionItem(device_key="fake:1"),
    ])
    session_id = await start_session(rig, devices=(("dev-chest", "chest"),))
    await stream(rig, "dev-chest", 30)
    folder = rig.ssd_path / "Data_Riset_IMU" / "Subject_Tag"
    service._clock.value += 1                                 # noqa: SLF001 - test clock
    await service.start_recording(session_id, folder)
    service.fail("cam2")
    service._clock.value += 6.5                               # noqa: SLF001

    await rig.manager.request_stop("operator_stop")
    await job.on_stopped(session_id)                          # same call the REST stop makes
    await settle(job, session_id)

    assert service.is_recording() is False
    record = job.record(session_id)
    steps = record["steps"]
    assert steps["cameras"]["status"] == "done"
    assert steps["package"]["status"] == "done"
    assert steps["cameras"]["started_at_ms"] is not None
    assert steps["cameras"]["ended_at_ms"] >= steps["cameras"]["started_at_ms"]

    result_file = folder / "_extras" / f"{session_id}_camera_result.json"
    result = json.loads(result_file.read_text(encoding="utf-8"))
    assert [r["cam_id"] for r in result["recordings"]] == ["cam1", "cam2"]
    assert [Path(p).name for p in result["recordings"][1]["files"]] == [
        f"{session_id}_cam2_video_sync.webm",
        f"{session_id}_cam2_video_sync_part2.webm",
    ]

    archive = Path(record["zip_path"])
    assert archive.is_file()
    assert archive.name == f"Subject_Tag_{session_id}.zip"
    names = zip_entries(archive)
    assert names[:3] == [
        f"videos/{session_id}_cam1_video_sync.webm",
        f"videos/{session_id}_cam2_video_sync.webm",
        f"videos/{session_id}_cam2_video_sync_part2.webm",
    ]
    assert names[-3:] == ["data/manifest.json", "cameras.json", "video_report.json"]
    assert "missed_cameras.txt" not in names          # every selected camera produced files
    # The CSV the session actually recorded is in the deliverable.
    assert f"data/{session_id}_chest_sensor_data.csv" in names

    # The progress view is FinalizeJob's steps followed by cameras and package.
    progress = session_progress(job, session_id)
    assert [s["name"] for s in progress["steps"]][-2:] == ["cameras", "package"]
    finalize_names = [s["name"] for s in progress["steps"][:-2]]
    assert finalize_names == ["close_writers", "validate", "bundle"]


@pytest.mark.asyncio
async def test_cameras_step_is_skipped_when_nothing_was_recording(rig):
    _service, _rt, job = camera_runtime(rig, 2)
    session_id = await start_session(rig)
    await stream(rig, "dev-chest", 10)
    await rig.manager.request_stop("operator_stop")
    await job.on_stopped(session_id)
    await settle(job, session_id)

    record = job.record(session_id)
    assert record["steps"]["cameras"]["status"] == "skipped"
    assert record["steps"]["cameras"]["detail"] == "no camera recording"
    assert record["steps"]["package"]["status"] == "done"
    names = zip_entries(Path(record["zip_path"]))
    assert names[-2:] == ["cameras.json", "video_report.json"]
    assert not any(name.startswith("videos/") for name in names)


@pytest.mark.asyncio
async def test_package_refuses_concurrent_runs_with_busy(rig, monkeypatch):
    _service, _rt, job = camera_runtime(rig, 1)
    session_id = await start_session(rig)
    await stream(rig, "dev-chest", 5)
    await rig.manager.request_stop("operator_stop")

    started = asyncio.Event()
    release = asyncio.Event()
    original = post_module.build_session_zip

    def slow_build(*args, **kwargs):
        started.set()
        # Blocking call inside to_thread; wait for the test to attempt the second run.
        while not release.is_set():
            import time as _time

            _time.sleep(0.01)
        return original(*args, **kwargs)

    monkeypatch.setattr(post_module, "build_session_zip", slow_build)
    first = asyncio.create_task(job.package(session_id))
    await asyncio.wait_for(started.wait(), timeout=10)
    with pytest.raises(PostBusy):
        await job.package(session_id)
    release.set()
    await asyncio.wait_for(first, timeout=20)


@pytest.mark.asyncio
async def test_debounced_repackager_rebuilds_a_deleted_zip(rig):
    _service, _rt, job = camera_runtime(rig, 1)
    session_id = await start_session(rig)
    await stream(rig, "dev-chest", 5)
    await rig.manager.request_stop("operator_stop")
    await job.on_stopped(session_id)
    await settle(job, session_id)

    archive = Path(job.record(session_id)["zip_path"])
    archive.unlink()
    job.schedule_package(session_id, delay=0.05)
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        if archive.is_file() and session_id not in job._running:
            break
        await asyncio.sleep(0.05)
    assert archive.is_file()
    assert job.record(session_id)["steps"]["package"]["status"] == "done"


@pytest.mark.asyncio
async def test_late_delivery_and_verified_upload_hooks_schedule_a_repackager(rig, monkeypatch):
    _service, _rt, job = camera_runtime(rig, 1)
    scheduled: list[str] = []
    monkeypatch.setattr(job, "schedule_package", lambda sid, delay=0.0: scheduled.append(sid))

    await job._on_lifecycle({"type": "LATE_DELIVERY", "session_id": SID})  # noqa: SLF001
    await job._on_lifecycle({"type": "FINALIZE_PROGRESS", "session_id": SID})  # noqa: SLF001
    job._on_upload(SID, {"complete": True, "sha256_verified": True})  # noqa: SLF001
    job._on_upload(SID, {"complete": True, "sha256_verified": False})  # noqa: SLF001
    job._on_upload(SID, {"complete": False})  # noqa: SLF001

    assert scheduled == [SID, SID]


@pytest.mark.asyncio
async def test_start_wires_and_unwires_both_hook_sources(rig):
    from master_backend.app import upload as upload_module

    _service, _rt, job = camera_runtime(rig, 1)
    manager = rig.manager
    before_observers = list(manager._observers)
    before_hooks = list(upload_module._upload_hooks)
    job.start()
    try:
        assert job._on_lifecycle in manager._observers
        assert job._on_upload in upload_module._upload_hooks
    finally:
        await job.close()
    assert manager._observers == before_observers
    assert upload_module._upload_hooks == before_hooks


@pytest.mark.asyncio
async def test_resume_after_restart_rebuilds_cameras_from_disk_and_packages(rig):
    """The backend died after finalize but before the post job ran (or mid-package)."""
    session_id = await start_session(rig)
    await stream(rig, "dev-chest", 20)
    folder = rig.ssd_path / "Data_Riset_IMU" / "Subject_Tag"
    await rig.manager.request_stop("operator_stop")
    await rig.manager.wait_for_finalize()
    assert (rig.manager.get_recovery_session(session_id) or {}).get("terminal")

    # Video + anchors were written before the crash; the stop result was not.
    service = FakeCameraService(1, clock=StepClock())
    service.set_selection([CameraSelectionItem(device_key="fake:0")])
    await service.start_recording(session_id, folder)
    service._clock.value += 4.0                               # noqa: SLF001
    await service.stop_recording(session_id)
    assert list(folder.glob(f"{session_id}_cam1_video_sync.webm"))

    # Fresh process: a new Runtime and job, nothing in memory.
    from master_backend.app import session_manager as sm_module
    from master_backend.app.api import runtime as runtime_mod
    fresh_rt = runtime_mod.create_runtime(camera_service=FakeCameraService(1))
    fresh = PostSessionJob(fresh_rt)
    await fresh.resume_pending([session_id])

    deadline = asyncio.get_running_loop().time() + 60
    while asyncio.get_running_loop().time() < deadline:
        record = fresh.record(session_id)
        if record and record["steps"]["package"]["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.05)

    record = fresh.record(session_id)
    assert record["steps"]["cameras"]["status"] == "done"
    assert record["steps"]["package"]["status"] == "done", record
    archive = Path(record["zip_path"])
    names = zip_entries(archive)
    assert f"videos/{session_id}_cam1_video_sync.webm" in names
    report = json.loads(zipfile.ZipFile(archive).read("video_report.json"))
    # FAKEWEBM is not decodable, so the recovered file is reported honestly, not dropped.
    assert report["videos"][0]["ok"] is False
    assert report["videos"][0]["error"]
    assert report["healthy"] is False


@pytest.mark.asyncio
async def test_resume_only_touches_unfinished_sessions(rig):
    _service, _rt, job = camera_runtime(rig, 1)
    session_id = await start_session(rig)
    await stream(rig, "dev-chest", 5)
    await rig.manager.request_stop("operator_stop")
    await job.on_stopped(session_id)
    await settle(job, session_id)

    fresh = PostSessionJob(job.rt)
    await fresh.resume_pending([session_id])
    assert fresh._tasks == {}


def test_video_files_are_in_part_order_and_ignore_other_files(tmp_path):
    for suffix in ("_part10", "_part3", "", "_part2"):
        (tmp_path / f"{SID}_cam1_video_sync{suffix}.webm").write_bytes(b"x")
    (tmp_path / f"{SID}_cam2_video_sync.webm").write_bytes(b"x")
    (tmp_path / f"{SID}_cam1_video_sync.webm.tmp").write_bytes(b"x")

    names = [p.name for p in _video_files(tmp_path, SID, "cam1")]
    assert names == [
        f"{SID}_cam1_video_sync.webm",
        f"{SID}_cam1_video_sync_part2.webm",
        f"{SID}_cam1_video_sync_part3.webm",
        f"{SID}_cam1_video_sync_part10.webm",
    ]
    assert _part_index(tmp_path / f"{SID}_cam1_video_sync.webm") == 0
    assert _part_index(tmp_path / f"{SID}_cam1_video_sync_part2.webm") == 2


@pytest.mark.asyncio
async def test_camera_failure_is_recorded_and_does_not_lose_the_video(rig):
    service, _rt, job = camera_runtime(rig, 1)
    service.set_selection([CameraSelectionItem(device_key="fake:0")])
    session_id = await start_session(rig)
    await stream(rig, "dev-chest", 5)
    folder = rig.ssd_path / "Data_Riset_IMU" / "Subject_Tag"
    await service.start_recording(session_id, folder)
    await rig.manager.request_stop("operator_stop")

    async def explode(_sid):
        raise RuntimeError("capture process already gone")

    service.stop_recording = explode                        # type: ignore[method-assign]
    await job.on_stopped(session_id)
    await settle(job, session_id)

    record = job.record(session_id)
    assert record["steps"]["cameras"]["status"] == "failed"
    assert "capture process already gone" in record["steps"]["cameras"]["detail"]
    # The anchors + file on disk still produced a package.
    assert record["steps"]["package"]["status"] == "done"
    assert f"videos/{session_id}_cam1_video_sync.webm" in zip_entries(Path(record["zip_path"]))
    assert rig.sm_module.session_manager  # rig sanity