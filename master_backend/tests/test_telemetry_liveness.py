import json
from master_backend.app.session_manager import DeviceInfo, SessionManager, SessionState
from master_backend.proto.commands import Command, CommandType, make_pong


def test_telemetry_disconnect_is_not_reported_as_control_offline():
    manager = SessionManager()
    manager.state = SessionState.RECORDING
    device = DeviceInfo("device-1", "chest", "phone", "2.2.0")
    manager._devices[device.device_id] = device

    manager.note_telemetry_disconnect(device.device_id)

    assert device.offline_intervals == []
    assert len(device.telemetry_gaps) == 1
    assert device.telemetry_gaps[0]["end_ms"] is None

    manager.increment_packets(device.device_id)
    assert device.telemetry_gaps[0]["end_ms"] is not None


def test_pong_can_carry_device_telemetry_progress():
    raw = make_pong(
        "ping-1",
        state="RECORDING",
        session_id="session-1",
        telemetry_packets=123,
        telemetry_age_ms=4500,
    )
    command = Command.from_bytes(raw)

    assert command.type == CommandType.PONG
    payload = json.loads(command.payload)
    assert payload["telemetry_packets"] == 123
    assert payload["telemetry_age_ms"] == 4500
