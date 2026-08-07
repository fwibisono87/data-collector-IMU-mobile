"""True-sampling-rate detection: distinct acc readings vs packet counts.

Reproduces the 2026-08-07 failure where two phones streamed packets at 100 Hz while
re-delivering the same hardware reading twice (true ~50 Hz). The live gate must count
DISTINCT acc triples (note_sample), not callbacks (increment_packets).
"""
import asyncio
from types import SimpleNamespace

from master_backend.app.session_manager import DeviceInfo, SessionManager


def _dev(device_id: str = "DEV1", role: str = "waist") -> DeviceInfo:
    return DeviceInfo(
        device_id=device_id,
        device_role=role,
        device_model="test",
        app_version="1.0",
    )


def _register(sm: SessionManager, device_id: str = "DEV1", role: str = "waist") -> None:
    err = sm.register_device(
        device_id=device_id,
        role=role,
        model="test",
        app_version="1.0",
        ws=SimpleNamespace(),
    )
    assert err is None


def test_all_distinct_triples_counts_every_sample():
    sm = SessionManager()
    _register(sm)
    for i in range(100):
        sm.note_sample("DEV1", (float(i), 0.0, 0.0))
    assert sm.get_device("DEV1").acc_changes == 100


def test_half_rate_repeat_reproduces_20260807_failure():
    """Same acc triple delivered twice → only 50 distinct readings out of 100 packets."""
    sm = SessionManager()
    _register(sm)
    for i in range(50):
        triple = (float(i), 0.0, 0.0)
        sm.note_sample("DEV1", triple)
        sm.note_sample("DEV1", triple)
    dev = sm.get_device("DEV1")
    assert dev.acc_changes == 50
    assert dev.packets_received == 0  # counter separation: acc count is independent


def test_unknown_device_is_noop():
    sm = SessionManager()
    _register(sm, "DEV1")
    # Unknown device must not raise and must not touch the known device.
    sm.note_sample("NOPE", (1.0, 2.0, 3.0))
    assert sm.get_device("DEV1").acc_changes == 0


def test_counters_reset_on_start_recording_and_survive_reconnect():
    sm = SessionManager()
    _register(sm)
    for i in range(20):
        sm.note_sample("DEV1", (float(i), 0.0, 0.0))
    dev = sm.get_device("DEV1")
    assert dev.acc_changes == 20

    # start_recording resets the true-rate counters.
    sm.state = sm.state.__class__.PREFLIGHT
    asyncio.run(sm.start_recording({"subject_name": "s", "session_tag": "t", "operator": "op"}))
    assert dev.acc_changes == 0
    assert dev._acc_changes_prev_tick == 0
    assert dev.true_hz == 0.0
    assert dev.held_pct == 0.0
    assert dev.last_acc is None

    # Reconnect preserves the counters (packets_received is preserved too).
    for i in range(10):
        sm.note_sample("DEV1", (float(i), 0.0, 0.0))
    assert dev.acc_changes == 10

    _register(sm, "DEV1")
    dev2 = sm.get_device("DEV1")
    assert dev2.acc_changes == 10
    assert dev2.last_acc == (9.0, 0.0, 0.0)


# ── Rate tick: the derived per-second figures the preflight gate reads ────────

def test_tick_rates_reports_half_rate_for_held_samples():
    """A device delivering 100 packets/s from a 50 Hz sensor must read true_hz=50.

    This is the 2026-08-07 signature: rate_hz looks perfectly healthy at 100 while half
    the rows are byte-identical repeats of the previous reading.
    """
    sm = SessionManager()
    _register(sm, "DEV1", "chest")
    dev = sm.get_device("DEV1")

    for i in range(100):
        sm.increment_packets("DEV1")
        # Value changes only every second packet — the OS re-delivering a stale sample.
        sm.note_sample("DEV1", (i // 2, 0.0, 1.0))

    sm._tick_rates(dev)
    assert dev.rate_hz == 100.0, "packet counter should still look healthy"
    assert dev.true_hz == 50.0, "distinct-reading counter must expose the real rate"
    assert dev.held_pct == 50.0


def test_tick_rates_reports_full_rate_for_distinct_samples():
    sm = SessionManager()
    _register(sm, "DEV1", "chest")
    dev = sm.get_device("DEV1")

    for i in range(100):
        sm.increment_packets("DEV1")
        sm.note_sample("DEV1", (i, 0.0, 1.0))

    sm._tick_rates(dev)
    assert dev.rate_hz == 100.0
    assert dev.true_hz == 100.0
    assert dev.held_pct == 0.0


def test_tick_rates_is_a_delta_not_a_total():
    """Consecutive ticks must each report one second's worth, not cumulative counts."""
    sm = SessionManager()
    _register(sm, "DEV1", "chest")
    dev = sm.get_device("DEV1")

    for i in range(100):
        sm.increment_packets("DEV1")
        sm.note_sample("DEV1", (i, 0.0, 1.0))
    sm._tick_rates(dev)
    assert dev.true_hz == 100.0

    for i in range(100, 140):
        sm.increment_packets("DEV1")
        sm.note_sample("DEV1", (i, 0.0, 1.0))
    sm._tick_rates(dev)
    assert dev.true_hz == 40.0, "second tick must report only the new interval"


def test_tick_rates_handles_zero_traffic():
    """A silent device must not divide by zero."""
    sm = SessionManager()
    _register(sm, "DEV1", "chest")
    dev = sm.get_device("DEV1")
    sm._tick_rates(dev)
    assert dev.rate_hz == 0.0
    assert dev.true_hz == 0.0
    assert dev.held_pct == 0.0
