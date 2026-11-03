"""L7a §1: consolidation runs inside the post-session job, before the ZIP.

The old flow consolidated only when the operator pressed "Pull & consolidate" in the
end-session modal, so a session that was not whole ended up with a ZIP that held the
pre-merge CSVs and no ``consolidation.json`` at all. These tests pin the replacement:
the ``consolidate`` step decides with the modal's own rules (ported to
``export.consolidation_decision``), waits the way the modal waited, produces exactly the
bytes the legacy endpoint produces, and re-runs when data arrives after the first ZIP.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import zipfile
from pathlib import Path
from urllib.parse import urlencode

import pytest
from starlette.requests import Request

from master_backend.app.api import post_session as post_module
from master_backend.app.api.post_session import PostSessionJob
from master_backend.app.api.runtime import create_runtime
from master_backend.app.camera.fake import FakeCameraService
from master_backend.app.csv_schema import CSV_HEADER_V2
from master_backend.app.export import consolidation_decision, export_consolidate
from master_backend.app.upload import upload_csv

from conftest import start_session, stream

HEADER = CSV_HEADER_V2


# ── helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture
def job(rig, monkeypatch):
    """A post-session job whose phone wait is short enough for a test to watch."""
    monkeypatch.setattr(post_module, "CONSOLIDATE_WAIT_S", 2.0)
    monkeypatch.setattr(post_module, "CONSOLIDATE_POLL_S", 0.02)
    rt = create_runtime(camera_service=FakeCameraService(0))
    return PostSessionJob(rt)


def session_folder(rig, subject: str = "Subject", tag: str = "Tag") -> Path:
    return rig.ssd_path / "Data_Riset_IMU" / f"{subject}_{tag}"


def _rescue_csv(session_id: str, device_id: str, role: str, *,
                first_seq: int, rows: int) -> bytes:
    lines = []
    for i in range(rows):
        seq = first_seq + i
        ts = 1_700_000_000_000 + seq * 10
        lines.append(
            f"{ts},0.5,0.5,0.5,1.0,1.0,1.0,0,0,{seq},{device_id},{ts},{ts - 2},0"
        )
    header = (
        f"# session_id={session_id},role={role},device_id={device_id},"
        f"subject=Subject,session_tag=Tag,operator=Op,"
        f"start_epoch_ms=1700000000000,source=local_node,schema_version=2\n"
    )
    return (header + HEADER + "\n".join(lines) + "\n").encode()


async def send_chunk(session_id: str, device_id: str, role: str, chunk: bytes, *,
                     offset: int, total: int, complete: bool = False,
                     sha256: str = "") -> dict:
    """Drive the real upload endpoint with one chunk of a (possibly multi-part) transfer."""
    query = urlencode({
        "device_id": device_id, "session_id": session_id, "role": role,
        "subject": "Subject", "session_tag": "Tag", "operator": "Op",
    }).encode()
    headers = [
        (b"x-offset", str(offset).encode()),
        (b"x-total", str(total).encode()),
        (b"x-complete", b"1" if complete else b"0"),
    ]
    if complete:
        headers.append((b"x-sha256", sha256.encode()))
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": chunk, "more_body": False}

    request = Request(
        {"type": "http", "method": "POST", "path": "/upload/csv",
         "query_string": query, "headers": headers},
        receive,
    )
    response = await upload_csv(request)
    return json.loads(response.body)


async def upload_rescue(session_id: str, device_id: str, role: str, body: bytes) -> dict:
    """Drive the real upload endpoint with one complete, hash-verified chunk."""
    return await send_chunk(
        session_id, device_id, role, body,
        offset=0, total=len(body), complete=True,
        sha256=hashlib.sha256(body).hexdigest(),
    )


async def settle(job: PostSessionJob, session_id: str, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        task = job._tasks.get(session_id)  # noqa: SLF001 - test inspects the job
        if task is None or task.done():
            if session_id not in job._running:  # noqa: SLF001
                return
        await asyncio.sleep(0.02)
    raise AssertionError(f"post-session job for {session_id} did not settle")


async def stop_and_settle(rig, job: PostSessionJob) -> str:
    session_id = await start_session(rig, devices=(("dev-chest", "chest"),))
    await stream(rig, "dev-chest", 40)
    await rig.manager.request_stop("operator_stop")
    await job.on_stopped(session_id)
    await settle(job, session_id)
    return session_id


def zipped_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()


def consolidation_artifacts(folder: Path, session_id: str) -> dict[str, bytes]:
    names = [
        f"{session_id}_consolidated.csv",
        f"{session_id}_chest_consolidated.csv",
        f"{session_id}_consolidation.json",
        f"{session_id}_consolidated_validation.json",
    ]
    return {name: (folder / name).read_bytes() for name in names if (folder / name).is_file()}


# ── the ported staleness table (EndSessionModal.tsx:616-628) ────────────────


def _manifest(**over) -> dict:
    base = {
        "session_id": "1753000000000",
        "found": True,
        "whole": False,
        "late_pending": False,
        "recovery_pending": False,
        "uploads_in_progress": False,
        "files": [{"name": "x_sensor_data.csv", "kind": "main", "size": 10}],
        "recovery": [],
        "late_summary": None,
    }
    base.update(over)
    return base


def _consolidated_file() -> dict:
    return {"name": "x_consolidated.csv", "kind": "consolidated", "size": 10}


def _verified_recovery() -> dict:
    return {
        "device_id": "dev-waist", "role": "waist", "complete": True,
        "sha256_verified": True, "csv_exists": True, "csv_size": 100,
    }


def test_staleness_table_matches_the_old_modal_rules():
    """Row per input combination the old UI distinguished (canConsolidate etc.)."""
    cases = [
        # (name, manifest, expected state, needed)
        ("whole session", _manifest(whole=True), "not_needed", False),
        # hasConsolidatedData false → canConsolidate true; a verified upload is material,
        # and the manifest marks the not-yet-folded upload as recovery_pending.
        ("first run with a rescue upload",
         _manifest(recovery=[_verified_recovery()], recovery_pending=True),
         "pending_phone_data", True),
        # Nothing consolidated and nothing to merge: the modal showed the button, but
        # merging would only rewrite the main CSVs, so the job skips it.
        ("partial session, nothing to merge", _manifest(), "current", False),
        # Consolidated and nothing pending → exportReady; never needs a merge again.
        ("current after the merge",
         _manifest(files=[{"name": "x_consolidated.csv", "kind": "consolidated",
                           "size": 10},
                          {"name": "x_chest_sensor_data.csv", "kind": "main",
                           "size": 10}]),
         "current", False),
        # late_pending makes hasPendingPhoneData true → canConsolidate true.
        ("late rows pending",
         _manifest(files=[_consolidated_file()], late_pending=True,
                   late_summary={"devices": {"dev-chest": {"rows_appended": 1204}}}),
         "pending_phone_data", True),
        # recovery_pending alone is enough (verified upload newer than the merge).
        ("rescue upload newer than the merge",
         _manifest(files=[_consolidated_file()], recovery_pending=True,
                   recovery=[_verified_recovery()]),
         "pending_phone_data", True),
        # A phone still sending, nothing verified yet: wait, then skip (no material).
        ("upload in flight", _manifest(files=[_consolidated_file()],
                                       uploads_in_progress=True),
         "pending_phone_data", False),
        # An unverified upload is never a consolidation source.
        ("unverified rescue upload",
         _manifest(recovery=[{**_verified_recovery(), "sha256_verified": False}]),
         "current", False),
    ]
    for name, manifest, state, needed in cases:
        decision = consolidation_decision(manifest)
        assert decision["state"] == state, (name, decision)
        assert decision["needed"] is needed, (name, decision)


def test_staleness_table_ports_has_pending_phone_data_and_can_consolidate():
    """The two booleans behind the old disabled state, checked one by one."""
    for field in ("late_pending", "recovery_pending", "uploads_in_progress"):
        decision = consolidation_decision(_manifest(files=[_consolidated_file()], **{field: True}))
        assert decision["has_pending_phone_data"] is True, field
        assert decision["can_consolidate"] is True, field
    current = consolidation_decision(_manifest(files=[_consolidated_file()]))
    assert current["has_consolidated_data"] is True
    assert current["has_pending_phone_data"] is False
    assert current["can_consolidate"] is False
    # canConsolidate's left half: no consolidated file at all → always re-offerable.
    assert consolidation_decision(_manifest())["can_consolidate"] is True


# ── whole sessions ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_whole_session_skips_consolidation_and_leaves_the_zip_alone(rig, job):
    session_id = await start_session(rig, devices=(("dev-chest", "chest"),))
    await stream(rig, "dev-chest", 40)
    await rig.manager.request_stop("operator_stop")
    await rig.manager.wait_for_finalize()
    folder = session_folder(rig)
    # The validator's own verdict is not the subject here; a PASS report with no late or
    # rescue data is what "whole" means (the modal only offered the button when not whole).
    (folder / f"{session_id}_integrity_report.json").write_text(
        json.dumps({"status": "PASS", "devices": []}), encoding="utf-8"
    )

    await job.on_stopped(session_id)
    await settle(job, session_id)

    record = job.record(session_id)
    assert record["steps"]["consolidate"]["status"] == "skipped"
    assert record["steps"]["consolidate"]["detail"] == "session is whole"
    assert record["steps"]["package"]["status"] == "done"
    names = zipped_names(Path(record["zip_path"]))
    assert not [n for n in names if "consolidated" in n or "consolidation" in n]
    assert not consolidation_artifacts(folder, session_id)


# ── the rescue upload that arrives in time ──────────────────────────────────


@pytest.mark.asyncio
async def test_rescue_upload_within_the_wait_is_merged_byte_identically(rig, job, monkeypatch):
    """A verified upload inside the wait window ends up in the ZIP, byte for byte.

    The expected bytes are produced by calling the legacy consolidate endpoint directly
    afterwards, in the same frozen clock, so this pins "the job calls the existing code"
    rather than "the job produced something plausible".
    """
    session_id = await start_session(rig, devices=(("dev-chest", "chest"),))
    await stream(rig, "dev-chest", 40)
    await rig.manager.request_stop("operator_stop")
    await rig.manager.wait_for_finalize()
    folder = session_folder(rig)
    body = _rescue_csv(session_id, "dev-chest", "chest", first_seq=40, rows=25)

    # Freeze the wall clock only: asyncio's loop clock is time.monotonic, which must keep
    # running or the wait itself could not sleep. `consolidated_at_ms` is the one field
    # in the artifacts that depends on the wall clock.
    monkeypatch.setattr(time, "time", lambda: 1_700_000_500.0)

    # The phone starts streaming before the job reaches the consolidate step: the first
    # half of the file is on disk, so the manifest says `uploads_in_progress` and the
    # wait has a transfer to keep it open for. The final chunk completes the transfer
    # while the job is polling.
    split = len(body) // 2
    started = await send_chunk(
        session_id, "dev-chest", "chest", body[:split], offset=0, total=len(body)
    )
    assert started["complete"] is False
    assert started["received_bytes"] == split
    notice_key = f"rescue_outstanding:{session_id}:dev-chest"
    job.rt.notices.raise_(
        code="rescue_outstanding",
        level="warn",
        message=f"Phone rescue upload for session {session_id} is still receiving.",
        session_id=session_id,
        key=notice_key,
    )

    async def finish_soon() -> None:
        await asyncio.sleep(0.01)
        result = await send_chunk(
            session_id, "dev-chest", "chest", body[split:],
            offset=split, total=len(body), complete=True,
            sha256=hashlib.sha256(body).hexdigest(),
        )
        assert result["sha256_verified"] is True

    finisher = asyncio.create_task(finish_soon())
    try:
        await job.on_stopped(session_id)
        await settle(job, session_id)
    finally:
        await finisher

    record = job.record(session_id)
    consolidate = record["steps"]["consolidate"]
    assert consolidate["status"] == "done", consolidate
    assert "merged 1 rescue upload" in consolidate["detail"]

    artifacts = consolidation_artifacts(folder, session_id)
    assert set(artifacts) == {
        f"{session_id}_consolidated.csv",
        f"{session_id}_chest_consolidated.csv",
        f"{session_id}_consolidation.json",
        f"{session_id}_consolidated_validation.json",
    }
    names = zipped_names(Path(record["zip_path"]))
    for name in artifacts:
        assert f"data/{name}" in names
    assert "data/recovery/dev-chest.csv" in names

    # Legacy path in the same folder, same frozen clock: identical bytes.
    for name in artifacts:
        (folder / name).unlink()
    await export_consolidate(session_id)
    assert consolidation_artifacts(folder, session_id) == artifacts

    # The notice that nagged about the outstanding upload is cleared.
    assert not [
        n for n in job.rt.notices.snapshot()
        if n.code == "rescue_outstanding" and n.session_id == session_id
    ]


# ── data after the first package ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_after_packaging_reconsolidates_and_repackages(rig, job):
    session_id = await start_session(rig, devices=(("dev-chest", "chest"),))
    await stream(rig, "dev-chest", 40)
    await rig.manager.request_stop("operator_stop")
    await job.on_stopped(session_id)
    await settle(job, session_id)

    first = job.record(session_id)
    assert first["steps"]["consolidate"]["status"] == "skipped"
    first_zip = Path(first["zip_path"])
    assert "data/recovery/dev-chest.csv" not in zipped_names(first_zip)

    body = _rescue_csv(session_id, "dev-chest", "chest", first_seq=40, rows=25)
    uploaded = await upload_rescue(session_id, "dev-chest", "chest", body)
    assert uploaded["sha256_verified"] is True

    # The upload hook is what production wires; it debounces and re-runs the chain.
    job.schedule_package(session_id, delay=0.05)
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        record = job.record(session_id) or {}
        if (record.get("steps", {}).get("consolidate", {}).get("status") == "done"
                and session_id not in job._running):  # noqa: SLF001
            break
        await asyncio.sleep(0.05)

    record = job.record(session_id)
    assert record["steps"]["consolidate"]["status"] == "done", record["steps"]
    names = zipped_names(Path(record["zip_path"]))
    assert f"data/{session_id}_consolidated.csv" in names
    assert f"data/{session_id}_consolidation.json" in names
    assert "data/recovery/dev-chest.csv" in names
    assert record["zip_path"] == first["zip_path"]        # replaced in place