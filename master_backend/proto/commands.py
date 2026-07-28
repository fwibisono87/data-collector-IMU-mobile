"""
Hand-written protobuf parser/builder matching shared_contracts/commands.proto.
Replace with protoc-generated output after running `make proto-python`.
"""
import json
import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _write_varint(value: int) -> bytes:
    buf = []
    while value > 0x7F:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value & 0x7F)
    return bytes(buf)


def _write_string(field_num: int, value: str) -> bytes:
    if not value:
        return b""
    encoded = value.encode("utf-8")
    tag = _write_varint((field_num << 3) | 2)
    return tag + _write_varint(len(encoded)) + encoded


def _write_varint_field(field_num: int, value: int) -> bytes:
    if value == 0:
        return b""
    tag = _write_varint((field_num << 3) | 0)
    return tag + _write_varint(value)


class CommandType(IntEnum):
    PING = 0
    PONG = 1
    START_SESSION = 2
    STOP_SESSION = 3
    SET_LABEL = 4
    ACK = 5
    CLOCK_SYNC = 6
    ERROR_ALERT = 7


@dataclass
class Command:
    type: CommandType = CommandType.PING
    payload: str = ""
    issued_at_ms: int = 0
    command_id: str = ""

    @classmethod
    def from_bytes(cls, data: bytes) -> "Command":
        cmd = cls()
        pos = 0
        while pos < len(data):
            tag, pos = _read_varint(data, pos)
            field_number = tag >> 3
            wire_type = tag & 0x7

            if wire_type == 0:
                val, pos = _read_varint(data, pos)
                match field_number:
                    case 1: cmd.type = CommandType(val)
                    case 3: cmd.issued_at_ms = val
            elif wire_type == 2:
                length, pos = _read_varint(data, pos)
                chunk = data[pos : pos + length]
                pos += length
                text = chunk.decode("utf-8", errors="replace")
                match field_number:
                    case 2: cmd.payload = text
                    case 4: cmd.command_id = text
            else:
                break

        return cmd

    def to_bytes(self) -> bytes:
        out = b""
        out += _write_varint_field(1, int(self.type))
        out += _write_string(2, self.payload)
        out += _write_varint_field(3, self.issued_at_ms or int(time.time() * 1000))
        out += _write_string(4, self.command_id)
        return out


def make_pong(
    command_id: str = "",
    state: str = "",
    session_id: str = "",
    late_sid: str = "",
) -> bytes:
    """PONG doubles as the authoritative session-state heartbeat (1 Hz).

    The payload is additive and optional: an older mobile build ignores it and
    behaves exactly as before. `late_sid` is non-empty only while the backend is
    still accepting late telemetry for a session that has already stopped.
    """
    payload = ""
    if state:
        payload = json.dumps(
            {"state": state, "session_id": session_id, "late_sid": late_sid}
        )
    return Command(type=CommandType.PONG, payload=payload, command_id=command_id).to_bytes()


def make_ack(command_id: str, status: str = "ok", detail: str = "") -> bytes:
    payload = json.dumps({"command_id": command_id, "status": status, "detail": detail})
    return Command(type=CommandType.ACK, payload=payload, command_id=command_id).to_bytes()


def make_error_alert(code: str, detail: str = "") -> bytes:
    payload = json.dumps({"code": code, "detail": detail})
    return Command(type=CommandType.ERROR_ALERT, payload=payload).to_bytes()


def make_clock_sync_response(command_id: str, t0_ms: int, t1_ms: int, t2_ms: int) -> bytes:
    payload = json.dumps({"t0_ms": t0_ms, "t1_ms": t1_ms, "t2_ms": t2_ms})
    return Command(
        type=CommandType.CLOCK_SYNC,
        payload=payload,
        command_id=command_id,
    ).to_bytes()
