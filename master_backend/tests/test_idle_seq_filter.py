"""Idle telemetry filter (rebuild SPEC §6.3): counted for liveness, never written.

Phones stream telemetry from the moment they connect so the preflight true-rate gate
has data before START. Those idle packets carry ``sequence_number >= IDLE_SEQ_BASE``
(2**40); session packets restart at 0,1,2,… They must feed the rate/liveness
accounting in every state but must never reach a session CSV, the late-delivery CSV,
the dedup store, a writer or the first-packet marker — idle traffic is not research
data. These tests drive ``ws_handler._ingest_packet``, the real per-packet path
extracted from ``telemetry_ws``.
"""
from __future__ import annotations

import pytest

from conftest import FakeWs, packet, start_session
from master_backend.app import ws_handler
from master_backend.app.csv_schema import IDLE_SEQ_BASE
from master_backend.app.session_manager import SessionManager, SessionState

BASE_TS = 1_700_000_000_000
FOLDER = "Subject_Tag"          # start_session() records Subject/Tag/Op


def idle_packet(device_id: str, i: int):
    """One idle-stream packet: sequence in the idle range, distinct acc reading.

    The acc offset keeps idle readings distinct from the session packets' so
    note_sample counts every one of them.
    """
    return packet(device_id, IDLE_SEQ_BASE + i, BASE_TS + i * 10, acc=100.0 + i)


def data_rows(path) -> list[list[str]]:
    """CSV data rows (skipping the metadata line and the header)."""
    return [line.split(",") for line in path.read_text().splitlines()[2:] if line]


@pytest.mark.asyncio
async def test_idle_packets_are_counted_but_never_written_during_recording(rig):
    sid = await start_session(rig)
    dev_id = "dev-chest"
    for i in range(10):
        await ws_handler._ingest_packet(packet(dev_id, i, BASE_TS + i * 10, acc=float(i)))
    for i in range(5):
        await ws_handler._ingest_packet(idle_packet(dev_id, i))
    # The same idle sequence again: idle packets are never deduplicated against, so a
    # repeat still counts (a repeated session packet would be dropped by the dedup store).
    await ws_handler._ingest_packet(idle_packet(dev_id, 4))

    dev = rig.manager.get_device(dev_id)
    assert dev.packets_received == 16       # 10 session + 6 idle, none dropped
    assert dev.acc_changes == 15            # the repeated idle reading is a held sample

    # The rate accounting the preflight gate reads is driven by idle traffic too.
    dev._packets_prev_tick = 0
    dev._acc_changes_prev_tick = 0
    SessionManager._tick_rates(dev)
    assert dev.rate_hz == 16.0
    assert dev.true_hz == 15.0

    await rig.manager.request_stop("test_stop")
    await rig.manager.wait_for_finalize()

    csv_path = rig.ssd_path / "Data_Riset_IMU" / FOLDER / f"{sid}_chest_sensor_data.csv"
    rows = data_rows(csv_path)
    assert len(rows) == 10                              # session packets only
    assert [int(r[9]) for r in rows] == list(range(10))
    assert all(int(r[9]) < IDLE_SEQ_BASE for r in rows)


@pytest.mark.asyncio
async def test_idle_packets_never_open_a_writer_or_mark_first_packet(rig):
    await start_session(rig)
    # A device that joins after START has no writer; only session packets may create one.
    rig.manager.register_device(
        device_id="dev-waist", role="waist", model="Pixel", app_version="2.0.0",
        ws=FakeWs(),
    )
    for i in range(3):
        await ws_handler._ingest_packet(idle_packet("dev-waist", i))
    assert not rig.io.has_writer("dev-waist")
    assert rig.manager.get_device("dev-waist").first_packet_ts is None

    # The first session packet opens the writer and marks the arrival, as before.
    await ws_handler._ingest_packet(packet("dev-waist", 0, BASE_TS, acc=1.0))
    assert rig.io.has_writer("dev-waist")
    assert rig.manager.get_device("dev-waist").first_packet_ts == BASE_TS

    await rig.manager.request_stop("test_stop")
    await rig.manager.wait_for_finalize()


@pytest.mark.asyncio
async def test_idle_packets_are_never_written_to_the_late_csv(rig):
    sid = await start_session(rig)
    dev_id = "dev-chest"
    for i in range(5):
        await ws_handler._ingest_packet(packet(dev_id, i, BASE_TS + i * 10, acc=float(i)))
    await rig.manager.request_stop("test_stop")
    await rig.manager.wait_for_finalize()
    assert rig.manager.state == SessionState.IDLE
    assert rig.io.late_session_id == sid        # the post-STOP grace window is armed

    packets_before = rig.manager.get_device(dev_id).packets_received
    # The phone flushes its buffered tail… while its idle stream keeps running.
    await ws_handler._ingest_packet(packet(dev_id, 5, BASE_TS + 50, acc=5.0))
    await ws_handler._ingest_packet(idle_packet(dev_id, 0))
    await ws_handler._ingest_packet(idle_packet(dev_id, 1))
    assert rig.manager.get_device(dev_id).packets_received == packets_before + 3

    summary = await rig.io.finalize_late(force=True)
    assert summary["devices"][dev_id]["rows_appended"] == 1

    late_csv = (rig.ssd_path / "Data_Riset_IMU" / FOLDER
                / f"{sid}_chest_sensor_data_late.csv")
    rows = data_rows(late_csv)
    assert len(rows) == 1                       # the buffered tail only
    assert int(rows[0][9]) == 5
