"""Idle telemetry filter (rebuild SPEC §6.3, refined by the lead's review of 7c751b0).

Phones stream telemetry from the moment they connect so the preflight true-rate gate
has data before START. Those idle packets carry ``sequence_number >= IDLE_SEQ_BASE``
(2**40); session packets restart at 0,1,2,…

While NOT recording, idle packets feed the rate/liveness accounting (that is what the
preflight gate reads). While RECORDING they must not touch any session counter:
``packets_received``/``acc_changes``/``last_packet_at`` end up in the research file
integrity_report.json, so an idle packet still in flight at START — or from a phone
that missed START — would inflate a research value. During RECORDING an idle packet
only sets ``DeviceInfo.idle_last_at`` ("connected, but not recording"). In no state
do idle packets reach a session CSV, the late CSV, the dedup store, a writer or the
first-packet marker. The display-only ``_latest_samples`` cache (the live chart) is
updated for every packet in every state.

These tests drive ``ws_handler._ingest_packet``, the real per-packet path extracted
from ``telemetry_ws``.
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

    The acc offset keeps idle readings distinct from the session packets' so any
    accidental note_sample call would be visible in acc_changes.
    """
    return packet(device_id, IDLE_SEQ_BASE + i, BASE_TS + i * 10, acc=100.0 + i)


def data_rows(path) -> list[list[str]]:
    """CSV data rows (skipping the metadata line and the header)."""
    return [line.split(",") for line in path.read_text().splitlines()[2:] if line]


@pytest.mark.asyncio
async def test_idle_packets_never_touch_session_counters_during_recording(rig):
    sid = await start_session(rig)
    dev_id = "dev-chest"
    for i in range(10):
        await ws_handler._ingest_packet(packet(dev_id, i, BASE_TS + i * 10, acc=float(i)))

    dev = rig.manager.get_device(dev_id)
    assert (dev.packets_received, dev.acc_changes) == (10, 10)
    assert dev.idle_last_at == 0.0
    packets_before = dev.packets_received
    changes_before = dev.acc_changes
    last_packet_before = dev.last_packet_at

    for i in range(5):
        await ws_handler._ingest_packet(idle_packet(dev_id, i))
    # The same idle sequence again: idle packets are never deduplicated against, and
    # every one of them must stay invisible to the research counters.
    await ws_handler._ingest_packet(idle_packet(dev_id, 4))

    assert dev.packets_received == packets_before   # integrity_report.json value
    assert dev.acc_changes == changes_before        # integrity_report.json value
    assert dev.last_packet_at == last_packet_before  # telemetry_age stays session-only
    assert dev.idle_last_at > 0.0                   # but the phone is visibly alive

    # The rate accounting now sees session traffic only.
    dev._packets_prev_tick = 0
    dev._acc_changes_prev_tick = 0
    SessionManager._tick_rates(dev)
    assert dev.rate_hz == 10.0
    assert dev.true_hz == 10.0

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
    dev = rig.manager.get_device("dev-waist")
    assert not rig.io.has_writer("dev-waist")
    assert dev.first_packet_ts is None
    assert (dev.packets_received, dev.acc_changes) == (0, 0)  # a phone that missed
    assert dev.idle_last_at > 0.0                             # START: connected, not recording

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
    # Outside RECORDING the liveness accounting keeps counting both (preflight gate).
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


@pytest.mark.asyncio
async def test_live_sample_cache_updates_during_recording_idle(rig):
    """The live chart is display-only: idle packets keep it moving mid-session."""
    await start_session(rig)
    ws_handler._latest_samples.clear()
    await ws_handler._ingest_packet(idle_packet("dev-chest", 2))
    cached = ws_handler._latest_samples["dev-chest"]
    assert cached["ts"] == BASE_TS + 20
    assert cached["acc"] == [102.0, 102.0, 102.0]
    await rig.manager.request_stop("test_stop")
    await rig.manager.wait_for_finalize()


@pytest.mark.asyncio
async def test_live_sample_cache_updates_while_idle(rig):
    """Before START the Setup view's chart must show the phone's idle stream."""
    rig.manager.register_device(
        device_id="dev-chest", role="chest", model="Pixel", app_version="2.0.0",
        ws=FakeWs(),
    )
    ws_handler._latest_samples.clear()
    for i in range(3):
        await ws_handler._ingest_packet(idle_packet("dev-chest", i))
    dev = rig.manager.get_device("dev-chest")
    assert dev.packets_received == 3      # preflight true_hz still fed while idle
    assert dev.acc_changes == 3
    cached = ws_handler._latest_samples["dev-chest"]
    assert cached["ts"] == BASE_TS + 20
    assert cached["gyro"] == [102.0, 102.0, 102.0]
