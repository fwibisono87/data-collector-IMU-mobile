"""End-to-end specification for the session finalize path.

WHY this file exists
--------------------
Nothing in the repository drove a full START -> stream -> STOP -> finalize -> manifest ->
bundle cycle and asserted on the outcome. `tools/device_simulator.py` can inject every
interesting fault but is operator-driven, and `tools/soak_monitor.py` deliberately never
sends START or STOP. So every hardening pass was verified by hand on real hardware, which
is why fixes kept landing without converging.

These tests exercise the real SessionManager, IoManager, FinalizeJob, IntegrityValidator
and export layer against a temporary SSD path. They are the executable contract for what
"the session was saved" means.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

# The `rig` fixture and the packet/stream helpers live in conftest.py so the output
# contract suite can drive the identical harness.
from conftest import FakeWs, packet, start_session, stream  # noqa: F401


# ── The contract ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_cycle_produces_every_artifact(rig):
    """START -> stream -> STOP -> finalize must leave a complete, terminal session."""
    session_id = await start_session(rig)
    await stream(rig, "dev-chest", 300)

    report = await rig.manager.stop_recording("operator_stop")

    assert rig.manager.state.value == "IDLE"
    assert report["session_id"] == session_id

    folder = rig.ssd_path / "Data_Riset_IMU" / "Subject_Tag"
    csv = folder / f"{session_id}_chest_sensor_data.csv"
    assert csv.exists(), "main CSV was not written"
    assert len(csv.read_text().splitlines()) == 302   # metadata + header + 300 rows

    # The validator's own artifacts.
    assert (folder / f"{session_id}_integrity_report.json").exists()
    assert (folder / f"{session_id}_connectivity.json").exists()

    # The durability backstop: a complete archive exists without the browser doing anything.
    bundle = folder / f"{session_id}_bundle.zip"
    assert bundle.exists(), "finalize did not write the server-side bundle"
    assert bundle.stat().st_size > 0

    # And the lifecycle record is terminal.
    ledger = json.loads((rig.ssd_path / ".sessions" / f"{session_id}.state.json").read_text())
    assert ledger["terminal"] is True
    assert ledger["state"] == "IDLE"
    assert [s["state"] for s in ledger["finalize"]["steps"]] == ["done", "done", "done"]


@pytest.mark.asyncio
async def test_finalization_is_observable(rig):
    """Every transition and every step must reach an observer.

    The dashboard used to sit on RECORDING for the whole close/validate span because
    transitions were written to the audit log and nowhere else.
    """
    rig.events.clear()
    await start_session(rig)
    await stream(rig, "dev-chest", 50)
    await rig.manager.stop_recording()

    transitions = [e["to"] for e in rig.events if e["type"] == "STATE_TRANSITION"]
    assert "FINALIZING" in transitions
    assert "VALIDATING" in transitions, "VALIDATING was never broadcast"
    assert transitions[-1] == "IDLE"

    progress = [e for e in rig.events if e["type"] == "FINALIZE_PROGRESS"]
    assert progress, "no per-step progress was emitted"
    running = {
        s["step"] for e in progress for s in e["steps"] if s["state"] == "running"
    }
    assert {"close_writers", "validate"} <= running

    assert any(e["type"] == "FINALIZE_COMPLETE" for e in rig.events)


@pytest.mark.asyncio
async def test_stop_returns_before_the_work_finishes(rig):
    """request_stop is the fast path: it must not wait for close/validate/bundle."""
    await start_session(rig)
    await stream(rig, "dev-chest", 50)

    await rig.manager.request_stop("operator_stop")
    # The session has left RECORDING immediately...
    assert rig.manager.state.value in ("FINALIZING", "VALIDATING")
    # ...and the job is still to come.
    snapshot = rig.manager.finalize_snapshot
    assert snapshot is not None
    await rig.manager.wait_for_finalize()
    assert rig.manager.state.value == "IDLE"


@pytest.mark.asyncio
async def test_second_stop_is_refused_and_keeps_the_report(rig):
    """A repeated STOP used to be acknowledged as success with an empty report."""
    from master_backend.app.session_manager import IllegalTransition

    await start_session(rig)
    await stream(rig, "dev-chest", 50)
    first = await rig.manager.stop_recording()
    assert first.get("session_id")

    with pytest.raises(IllegalTransition):
        await rig.manager.request_stop("operator_stop")

    # The compatibility wrapper returns empty rather than raising, but must never
    # overwrite the real verdict.
    assert await rig.manager.stop_recording() == {}
    assert rig.manager._last_report == first


@pytest.mark.asyncio
async def test_a_failing_step_still_produces_a_terminal_session(rig, monkeypatch):
    """One broken step must not cost the operator the rest of finalization."""
    from master_backend.app import finalize_job

    async def boom(self):
        raise OSError("simulated validator failure")

    monkeypatch.setattr(finalize_job.FinalizeJob, "_step_validate", boom)

    session_id = await start_session(rig)
    await stream(rig, "dev-chest", 50)
    report = await rig.manager.stop_recording()

    assert rig.manager.state.value == "IDLE"
    assert report["status"] == "FAIL"
    assert "validate" in rig.manager.finalize_snapshot["failed"]

    folder = rig.ssd_path / "Data_Riset_IMU" / "Subject_Tag"
    # The CSV was still closed, and the bundle step still ran.
    assert (folder / f"{session_id}_chest_sensor_data.csv").exists()
    assert (folder / f"{session_id}_bundle.zip").exists()
    ledger = json.loads((rig.ssd_path / ".sessions" / f"{session_id}.state.json").read_text())
    assert ledger["terminal"] is True


@pytest.mark.asyncio
async def test_finalize_resumes_instead_of_redoing_completed_steps(rig):
    """A job re-run from persisted state must skip what already succeeded."""
    from master_backend.app.finalize_job import FinalizeContext, FinalizeJob

    calls: list[str] = []

    class Counting(FinalizeJob):
        async def _step_close_writers(self):
            calls.append("close")

        async def _step_validate(self):
            calls.append("validate")

        async def _step_bundle(self):
            calls.append("bundle")

    ctx = FinalizeContext(session_id="s1")
    await Counting(ctx).run()
    assert calls == ["close", "validate", "bundle"]

    # Replay with the first two already done — only the last should run.
    calls.clear()
    resumed = Counting(ctx, {
        "close_writers": {"state": "done", "detail": ""},
        "validate": {"state": "done", "detail": ""},
    })
    await resumed.run()
    assert calls == ["bundle"]


@pytest.mark.asyncio
async def test_multi_device_session_writes_one_csv_per_role(rig):
    session_id = await start_session(
        rig, devices=(("dev-chest", "chest"), ("dev-wrist", "wrist")),
    )
    await stream(rig, "dev-chest", 120)
    await stream(rig, "dev-wrist", 120)
    report = await rig.manager.stop_recording()

    folder = rig.ssd_path / "Data_Riset_IMU" / "Subject_Tag"
    assert (folder / f"{session_id}_chest_sensor_data.csv").exists()
    assert (folder / f"{session_id}_wrist_sensor_data.csv").exists()
    assert len(report["devices"]) == 2


@pytest.mark.asyncio
async def test_out_of_order_rows_are_sorted_before_the_digest(rig):
    """A mid-session reconnect replays buffered packets; the CSV must still be monotonic."""
    session_id = await start_session(rig)
    base = 1_700_000_000_000
    await stream(rig, "dev-chest", 50, base_ts=base + 100_000, start_seq=100)
    # The buffered tail arrives late, with older timestamps.
    await stream(rig, "dev-chest", 50, base_ts=base, start_seq=0)
    await rig.manager.stop_recording()

    csv = (rig.ssd_path / "Data_Riset_IMU" / "Subject_Tag"
           / f"{session_id}_chest_sensor_data.csv")
    timestamps = [
        int(line.split(",", 1)[0])
        for line in csv.read_text().splitlines()[2:]
        if line
    ]
    assert timestamps == sorted(timestamps), "finalize left the CSV out of order"


@pytest.mark.asyncio
async def test_a_new_session_never_inherits_the_previous_writers(rig):
    """open_session must retire stale handles (they used to leak across sessions)."""
    first = await start_session(rig)
    await stream(rig, "dev-chest", 20)
    # End the session by a route that does not close writers, as abort-from-ERROR did.
    rig.io._session_open = False
    rig.manager.state = rig.sm_module.SessionState.IDLE
    if rig.manager._offline_check_task:
        rig.manager._offline_check_task.cancel()
        rig.manager._offline_check_task = None
    assert rig.io._writers, "precondition: a writer is still held"

    second = await start_session(rig)
    assert second != first
    await stream(rig, "dev-chest", 20)
    results = await rig.io.close_session()

    for result in results.values():
        assert second in Path(result["path"]).name, (
            "close_session reported an artifact belonging to the previous session"
        )


@pytest.mark.asyncio
async def test_dedup_outlives_stop_so_the_late_window_cannot_duplicate(rig):
    """Dedup state belongs to the delivery window, not to the stop."""
    from master_backend.app.dedup_store import dedup

    session_id = await start_session(rig)
    await stream(rig, "dev-chest", 10)
    dedup.add("dev-chest", session_id, 5)

    await rig.manager.stop_recording()
    assert dedup.is_duplicate("dev-chest", session_id, 5), (
        "dedup was cleared at STOP while the late window was still open"
    )

    await rig.io.finalize_late(force=True)
    assert not dedup.is_duplicate("dev-chest", session_id, 5)


@pytest.mark.asyncio
async def test_a_failed_late_summary_does_not_block_the_next_session(rig, monkeypatch):
    """One bad sidecar write used to leave the late window armed and break every START."""
    await start_session(rig)
    await stream(rig, "dev-chest", 10)
    await rig.manager.stop_recording()

    # Give the late window a writer so finalize_late attempts the summary write.
    await rig.io.write_late(packet("dev-chest", 999, 1_700_000_099_999), "chest")

    real_write_text = Path.write_text

    def explode(self, *args, **kwargs):
        if self.name.endswith("_late_delivery.json"):
            raise OSError("simulated SSD failure")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", explode)
    await rig.io.finalize_late(force=True)
    monkeypatch.undo()

    assert rig.io._late_session_id == "", "late window stayed armed after a failed summary"
    # The next session must start normally.
    second = await start_session(rig)
    assert rig.manager.state.value == "RECORDING"
    assert second


@pytest.mark.asyncio
async def test_manifest_reports_transfers_that_have_not_verified(rig):
    """A phone still sending must be visible; recovery_pending alone cannot see it."""
    session_id = await start_session(rig)
    await stream(rig, "dev-chest", 30)
    await rig.manager.stop_recording()

    recovery_dir = rig.export_module._recovery_dir(session_id)
    recovery_dir.mkdir(parents=True, exist_ok=True)
    (recovery_dir / "dev_wrist.info.json").write_text(json.dumps({
        "session_id": session_id, "device_id": "dev-wrist", "role": "wrist",
        "state": "receiving", "received_bytes": 1024, "total_bytes": 8192,
        "complete": False, "sha256_verified": False,
    }))

    manifest = await rig.export_module.export_manifest(session_id)
    assert manifest["uploads_in_progress"] is True
    assert manifest["transfers_in_progress"][0]["role"] == "wrist"
    assert manifest["transfers_in_progress"][0]["state"] == "receiving"


@pytest.mark.asyncio
async def test_ledger_write_is_durable_and_atomic(rig):
    """The crash-recovery index must never be left partial by its own writer."""
    session_id = await start_session(rig)
    await rig.manager.stop_recording()
    path = rig.ssd_path / ".sessions" / f"{session_id}.state.json"
    assert path.exists()
    assert json.loads(path.read_text())["session_id"] == session_id
    # No temporary file survives a successful write.
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.asyncio
async def test_crash_recovery_runs_the_same_finalize_job(rig):
    """A session interrupted by a process death is finalized by the ordinary steps.

    Startup used to run a second, divergent implementation of finalization, so a fix in
    one path silently did not apply to the other. Recovery now drives the same
    FinalizeJob — which means an interrupted session also gets validated and bundled.
    """
    session_id = await start_session(rig)
    await stream(rig, "dev-chest", 200)

    # Simulate SIGKILL: the CSV handle is released with its bytes on disk, and the ledger
    # is left mid-RECORDING with no terminal marker.
    for writer in list(rig.io._writers.values()):
        await writer.close(sort=False)
    rig.io._writers.clear()
    rig.io._session_open = False
    if rig.manager._offline_check_task:
        rig.manager._offline_check_task.cancel()
        rig.manager._offline_check_task = None
    rig.manager.state = rig.sm_module.SessionState.IDLE
    rig.manager.session_id = ""
    rig.io._session_id = ""

    record = rig.manager._ledger.read(session_id)
    assert record["state"] == "RECORDING" and not record.get("terminal")

    await rig.manager.recover_interrupted_sessions()

    recovered = rig.manager._ledger.read(session_id)
    assert recovered["terminal"] is True
    assert recovered["recovered_after_backend_restart"] is True
    assert recovered["stop_reason"] == "backend_restart_recovery"
    # The same three steps as a live stop, all attempted.
    assert [s["step"] for s in recovered["finalize"]["steps"]] == [
        "close_writers", "validate", "bundle",
    ]
    assert recovered["integrity_report"]["session_id"] == session_id
    # And an interrupted session is bundled too, which the old recovery path never did.
    folder = rig.ssd_path / "Data_Riset_IMU" / "Subject_Tag"
    assert (folder / f"{session_id}_bundle.zip").exists()


@pytest.mark.asyncio
async def test_recovery_does_not_redo_a_step_that_already_finished(rig):
    """Resume, not restart: a persisted 'done' step is skipped on the next startup."""
    session_id = await start_session(rig)
    await stream(rig, "dev-chest", 50)
    for writer in list(rig.io._writers.values()):
        await writer.close(sort=False)
    rig.io._writers.clear()
    rig.io._session_open = False
    if rig.manager._offline_check_task:
        rig.manager._offline_check_task.cancel()
        rig.manager._offline_check_task = None
    rig.manager.state = rig.sm_module.SessionState.IDLE
    rig.manager.session_id = ""
    rig.io._session_id = ""

    # Pretend the crash happened after close_writers had completed.
    record = rig.manager._ledger.read(session_id)
    record["finalize"] = {"steps": [{"step": "close_writers", "state": "done", "detail": ""}]}
    rig.manager._ledger.write(session_id, record)

    collected = []
    original = rig.manager._collect_recovery_artifacts

    def spy(rec):
        collected.append(rec.get("session_id"))
        return original(rec)

    rig.manager.__class__._collect_recovery_artifacts = staticmethod(spy)
    try:
        await rig.manager.recover_interrupted_sessions()
    finally:
        rig.manager.__class__._collect_recovery_artifacts = staticmethod(original)

    assert collected == [], "close_writers was re-run despite being marked done"
    recovered = rig.manager._ledger.read(session_id)
    steps = {s["step"]: s["state"] for s in recovered["finalize"]["steps"]}
    assert steps["close_writers"] == "done"
    assert steps["validate"] in ("done", "failed")
