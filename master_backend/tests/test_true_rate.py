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
