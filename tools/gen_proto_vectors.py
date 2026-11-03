#!/usr/bin/env python3
"""Generate the cross-language protobuf test vectors used by the Kotlin phone app.

The phone app (``mobile_node``) hand-writes its own protobuf codec, so its bytes must be
checked against the backend's codec rather than against another Kotlin test. This script
produces ``mobile_node/app/src/test/resources/proto_vectors.json``: a set of representative
messages encoded here and *decoded* with ``master_backend/proto`` — the same module the
backend uses to read phone telemetry. The Kotlin test then asserts that it produces exactly
these bytes from these fields and that its own decoder returns exactly these fields back.

Coverage (docs/rebuild/PHONE.md → Tests):
  * every message type (Command, SensorPacket, DeviceRegister) round-trips;
  * negative int64 (canonical 10-byte two's complement varint);
  * a large uint64 at/above IDLE_SEQ_BASE, plus UINT64_MAX;
  * unicode strings;
  * all 14 SensorPacket fields set;
  * trailing unknown fields, which every decoder must skip;
  * a ``float6`` table pinning Python's ``f"{v:.6f}"`` rendering of exact doubles — the
    rescue CSV's 6-decimal formatter (Dart ``toStringAsFixed(6)`` parity) must match it.

Why the encoder lives here instead of in ``master_backend/proto``: that module only
*decodes* SensorPacket/DeviceRegister (the backend never originates them), and its
``_write_varint`` does not mask negative values, so it emits a single byte for -1 instead of
the canonical 10-byte two's complement form. Every vector is therefore encoded with the
canonical proto3 rules below and then verified with the backend's real decoders; for Command
vectors with a positive ``issued_at_ms`` the bytes are additionally asserted to be identical
to ``master_backend.proto.commands.Command.to_bytes()``, so the two encoders are proven to
agree on everything the backend actually sends.

Usage (from the repo root):
    python3 tools/gen_proto_vectors.py [-o mobile_node/app/src/test/resources/proto_vectors.json]
"""
from __future__ import annotations

import argparse
import json
import random
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from master_backend.proto.commands import Command, CommandType  # noqa: E402
from master_backend.proto.sensor_packet import DeviceRegister, SensorPacket  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "mobile_node" / "app" / "src" / "test" / "resources" / "proto_vectors.json"

# Must match core/SessionLogic.kt (IDLE_SEQ_BASE) and docs/rebuild/SPEC.md §6.3.
IDLE_SEQ_BASE = 1 << 40
UINT64_MAX = (1 << 64) - 1
_MASK64 = UINT64_MAX

SENSOR_PACKET_FIELDS = (
    "acc_x", "acc_y", "acc_z", "gyro_x", "gyro_y", "gyro_z",
    "timestamp_ms", "sequence_number", "device_id", "schema_version",
    "raw_timestamp_ms", "acc_ts_ms", "gyro_ts_ms", "sample_kind",
)
COMMAND_FIELDS = ("type", "payload", "issued_at_ms", "command_id")
DEVICE_REGISTER_FIELDS = (
    "device_id", "device_role", "device_model", "android_version",
    "app_version", "schema_version",
)


# ── proto3 encoder (defaults omitted, canonical varints) ───────────────────────

def _varint(value: int) -> bytes:
    """Unsigned varint of the 64-bit two's complement pattern of ``value``.

    Masking first is what makes negative int64 canonical: -1 becomes ten bytes
    (0xFF * 9 + 0x01), which is what every conforming protobuf implementation writes
    and what the Kotlin codec writes too.
    """
    v = value & _MASK64
    out = bytearray()
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _f32(field: int, value: float) -> bytes:
    if value == 0.0:  # proto3 default; -0.0 == 0.0, so it is omitted as well
        return b""
    return _tag(field, 5) + struct.pack("<f", value)


def _int(field: int, value: int) -> bytes:
    if value == 0:
        return b""
    return _tag(field, 0) + _varint(value)


def _string(field: int, value: str) -> bytes:
    if not value:
        return b""
    raw = value.encode("utf-8")
    return _tag(field, 2) + _varint(len(raw)) + raw


def _f32v(value: float) -> float:
    """Round to the float32 the wire actually carries, so JSON and bytes agree."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


def encode_sensor_packet(f: dict) -> bytes:
    out = b""
    out += _f32(1, f["acc_x"])
    out += _f32(2, f["acc_y"])
    out += _f32(3, f["acc_z"])
    out += _f32(4, f["gyro_x"])
    out += _f32(5, f["gyro_y"])
    out += _f32(6, f["gyro_z"])
    out += _int(7, f["timestamp_ms"])
    out += _int(8, f["sequence_number"])
    out += _string(9, f["device_id"])
    out += _int(10, f["schema_version"])
    out += _int(11, f["raw_timestamp_ms"])
    out += _int(12, f["acc_ts_ms"])
    out += _int(13, f["gyro_ts_ms"])
    out += _int(14, f["sample_kind"])
    return out


def encode_command(f: dict) -> bytes:
    out = b""
    out += _int(1, f["type"])
    out += _string(2, f["payload"])
    out += _int(3, f["issued_at_ms"])
    out += _string(4, f["command_id"])
    return out


def encode_device_register(f: dict) -> bytes:
    out = b""
    out += _string(1, f["device_id"])
    out += _string(2, f["device_role"])
    out += _string(3, f["device_model"])
    out += _string(4, f["android_version"])
    out += _string(5, f["app_version"])
    out += _int(6, f["schema_version"])
    return out


_ENCODERS = {
    "SensorPacket": encode_sensor_packet,
    "Command": encode_command,
    "DeviceRegister": encode_device_register,
}


# ── decode verification with the backend's own codec ──────────────────────────

def _signed(value: int) -> int:
    """Read a 64-bit pattern as signed, so negative int64 compares correctly."""
    return value - (1 << 64) if value >= (1 << 63) else value


def verify_with_backend(message: str, fields: dict, raw: bytes) -> None:
    """Decode ``raw`` with master_backend.proto and compare against ``fields``."""
    if message == "SensorPacket":
        got = SensorPacket.from_bytes(raw)
        pairs = (
            [(k, _f32v(getattr(got, k))) for k in ("acc_x", "acc_y", "acc_z", "gyro_x", "gyro_y", "gyro_z")]
            + [(k, _signed(getattr(got, k))) for k in
               ("timestamp_ms", "sequence_number", "raw_timestamp_ms", "acc_ts_ms", "gyro_ts_ms")]
            + [(k, getattr(got, k)) for k in ("device_id", "schema_version", "sample_kind")]
        )
    elif message == "Command":
        got = Command.from_bytes(raw)
        pairs = (
            [("type", int(got.type))]
            + [("issued_at_ms", _signed(got.issued_at_ms))]
            + [(k, getattr(got, k)) for k in ("payload", "command_id")]
        )
    elif message == "DeviceRegister":
        got = DeviceRegister.from_bytes(raw)
        pairs = [(k, getattr(got, k)) for k in DEVICE_REGISTER_FIELDS]
    else:  # pragma: no cover - guarded by the vector table below
        raise AssertionError(f"unknown message {message}")

    for key, decoded in pairs:
        expected = _signed(fields[key]) if isinstance(fields[key], int) else fields[key]
        if isinstance(expected, float):
            expected = _f32v(expected)
        assert decoded == expected, (
            f"{message}.{key}: backend decoded {decoded!r} but the vector says {expected!r}"
        )


def verify_against_backend_encoder(fields: dict, raw: bytes) -> None:
    """Command vectors with issued_at_ms > 0 must match the backend's own encoder byte-for-byte.

    ``Command.to_bytes()`` substitutes the current time for a zero issued_at_ms, so only
    positive values are deterministic enough to compare.
    """
    expected = Command(
        type=CommandType(fields["type"]),
        payload=fields["payload"],
        issued_at_ms=fields["issued_at_ms"],
        command_id=fields["command_id"],
    ).to_bytes()
    assert expected == raw, f"Command encoder mismatch:\n  ours    {raw.hex()}\n  backend {expected.hex()}"


# ── the vector table ──────────────────────────────────────────────────────────

_DEVICE_ID = "6f1c1a4e-9c3b-4d2a-8f0e-2b7d5c1a9e40"
_SESSION_ID = "20261029T091500-sesi-01"
_TS = 1761800000123


def _vectors() -> list[dict]:
    all_fields_packet = {
        "acc_x": _f32v(0.123456),
        "acc_y": _f32v(-0.987654),
        "acc_z": _f32v(0.000001),
        "gyro_x": _f32v(12.5),
        "gyro_y": _f32v(-359.999),
        "gyro_z": _f32v(0.001),
        "timestamp_ms": _TS,
        "sequence_number": 1234567,
        "device_id": "dev-αβ-🎯-" + _DEVICE_ID[:8],
        "schema_version": 2,
        "raw_timestamp_ms": _TS - 7,
        "acc_ts_ms": _TS - 3,
        "gyro_ts_ms": _TS - 4,
        "sample_kind": 1,
    }
    negative_packet = {
        "acc_x": _f32v(-1.5),
        "acc_y": 0.0,
        "acc_z": _f32v(9.80665),
        "gyro_x": _f32v(-0.0),
        "gyro_y": 0.0,
        "gyro_z": 0.0,
        "timestamp_ms": -1,
        "sequence_number": 7,
        "device_id": _DEVICE_ID,
        "schema_version": 2,
        "raw_timestamp_ms": -_TS,
        "acc_ts_ms": -5,
        "gyro_ts_ms": -6,
        "sample_kind": 0,
    }
    idle_packet = {
        "acc_x": _f32v(0.02),
        "acc_y": _f32v(-0.01),
        "acc_z": _f32v(1.0),
        "gyro_x": 0.0,
        "gyro_y": 0.0,
        "gyro_z": 0.0,
        "timestamp_ms": _TS,
        "sequence_number": IDLE_SEQ_BASE + 4242,
        "device_id": _DEVICE_ID,
        "schema_version": 2,
        "raw_timestamp_ms": _TS,
        "acc_ts_ms": 0,
        "gyro_ts_ms": 0,
        "sample_kind": 0,
    }
    max_seq_packet = dict(idle_packet, sequence_number=UINT64_MAX)
    unknown_tail_packet = dict(all_fields_packet, sample_kind=0)

    vectors = [
        {
            "name": "command_ping",
            "message": "Command",
            "fields": {"type": 0, "payload": "", "issued_at_ms": _TS, "command_id": ""},
        },
        {
            "name": "command_pong_state_payload",
            "message": "Command",
            "fields": {
                "type": 1,
                "payload": json.dumps({
                    "state": "RECORDING",
                    "session_id": _SESSION_ID,
                    "late_sid": "",
                    "telemetry_packets": 12345,
                    "telemetry_age_ms": 120,
                }, separators=(",", ":")),
                "issued_at_ms": _TS + 456,
                "command_id": "b1e2c3d4-0000-4000-8000-000000000001",
            },
        },
        {
            "name": "command_start_session_unicode",
            "message": "Command",
            "fields": {
                "type": 2,
                "payload": json.dumps({
                    "session_id": _SESSION_ID,
                    "scheduled_start_ms": _TS + 5000,
                    "subject": "Súbjek-éﻝ",
                    "session_tag": "jalan 🚶",
                    "operator": "Üniversitas Indonesia",
                }, separators=(",", ":")),
                "issued_at_ms": _TS + 1,
                "command_id": "b1e2c3d4-0000-4000-8000-000000000002",
            },
        },
        {
            "name": "command_clock_sync_negative",
            "message": "Command",
            "fields": {
                "type": 6,
                "payload": json.dumps({"t0_ms": -12345}, separators=(",", ":")),
                "issued_at_ms": -987654321,
                "command_id": "b1e2c3d4-0000-4000-8000-000000000003",
            },
        },
        {
            "name": "command_error_alert_role_collision",
            "message": "Command",
            "fields": {
                "type": 7,
                "payload": json.dumps({
                    "code": "ROLE_COLLISION",
                    "detail": "Role 'chest' already used by device 6f1c1a4e",
                }, separators=(",", ":")),
                "issued_at_ms": _TS + 2,
                "command_id": "",
            },
        },
        {
            "name": "command_unknown_fields",
            "message": "Command",
            "fields": {"type": 3, "payload": "{}", "issued_at_ms": _TS + 3,
                       "command_id": "cmd-unknown"},
            # field 15 varint, 16 string, 17 fixed32, 18 fixed64 — all unknown to every
            # decoder in this repo; they must be skipped without disturbing fields 1-4.
            "trailing_unknown_hex": (
                _tag(15, 0) + _varint(300)
                + _string(16, "extra")
                + _tag(17, 5) + struct.pack("<f", 2.5)
                + _tag(18, 1) + struct.pack("<q", -77)
            ).hex(),
        },
        {
            "name": "sensor_packet_all_fields",
            "message": "SensorPacket",
            "fields": all_fields_packet,
        },
        {
            "name": "sensor_packet_negative_int64",
            "message": "SensorPacket",
            "fields": negative_packet,
        },
        {
            "name": "sensor_packet_idle_sequence",
            "message": "SensorPacket",
            "fields": idle_packet,
        },
        {
            "name": "sensor_packet_max_uint64_sequence",
            "message": "SensorPacket",
            "fields": max_seq_packet,
        },
        {
            "name": "sensor_packet_unknown_fields",
            "message": "SensorPacket",
            "fields": unknown_tail_packet,
            "trailing_unknown_hex": (
                _tag(15, 0) + _varint(1)
                + _string(16, "future")
                + _tag(17, 5) + struct.pack("<f", 1.25)
                + _tag(18, 1) + struct.pack("<q", 99)
            ).hex(),
        },
        {
            "name": "device_register_full",
            "message": "DeviceRegister",
            "fields": {
                "device_id": _DEVICE_ID,
                "device_role": "thigh_left",
                "device_model": "SM-A566B 📱",
                "android_version": "15",
                "app_version": "3.0.0",
                "schema_version": 1,
            },
        },
    ]

    out = []
    for v in vectors:
        message = v["message"]
        fields = {k: v["fields"][k] for k in _field_order(message)}
        raw = _ENCODERS[message](fields)
        raw += bytes.fromhex(v.get("trailing_unknown_hex", ""))
        verify_with_backend(message, fields, raw)
        if message == "Command" and fields["issued_at_ms"] > 0:
            verify_against_backend_encoder(fields, _ENCODERS[message](fields))
        entry = {
            "name": v["name"],
            "message": message,
            "hex": raw.hex(),
            "fields": _json_fields(fields),
        }
        if "trailing_unknown_hex" in v:
            entry["trailing_unknown_hex"] = v["trailing_unknown_hex"]
        out.append(entry)
    return out


def _field_order(message: str) -> tuple:
    return {
        "SensorPacket": SENSOR_PACKET_FIELDS,
        "Command": COMMAND_FIELDS,
        "DeviceRegister": DEVICE_REGISTER_FIELDS,
    }[message]


# ── CSV %.6f formatting table ────────────────────────────────────────────────

def _float6_table() -> list[dict]:
    """`value` → `text` for Python's f"{v:.6f}", the rounding the rescue CSV must match.

    Dart's toStringAsFixed(6) and Python round the EXACT binary value; Java's
    String.format("%.6f") rounds the shortest decimal representation instead and differs
    on ties and near-zero magnitudes, so the Kotlin side uses BigDecimal and this table
    pins the agreement. Values are written with repr() and round-trip exactly through
    JSON; -0.0 keeps its sign in the JSON text.
    """
    fixed = [
        5e-7, 1.0000005, 0.1234565, 2.6745455, -1e-9, -0.0, 0.0,
        1 / 3, -2.5e-7, 9.80665 / 9.80665, 123.4567895,
    ]
    rng = random.Random(20261030)
    rand = [rng.uniform(-300.0, 300.0) for _ in range(200)]
    return [{"value": v, "text": f"{v:.6f}"} for v in fixed + rand]


def _json_fields(fields: dict) -> dict:
    """Emit floats with %.9g: the shortest decimal that round-trips through float32."""
    out = {}
    for k, v in fields.items():
        out[k] = float(f"{v:.9g}") if isinstance(v, float) else v
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUT),
                        help=f"destination JSON (default: {DEFAULT_OUT.relative_to(REPO_ROOT)})")
    args = parser.parse_args()

    vectors = _vectors()
    doc = {
        "generated_by": "tools/gen_proto_vectors.py",
        "note": (
            "Cross-language protobuf vectors. `hex` is the canonical proto3 encoding of "
            "`fields`; it was decoded with master_backend/proto before being written here. "
            "Regenerate with `python3 tools/gen_proto_vectors.py` — never edit by hand. "
            "Floats are float32-rounded and written with %.9g; read them as float32. "
            "sequence_number and other uint64 fields may exceed Long.MAX_VALUE and must be "
            "parsed as unsigned 64-bit. `float6` maps exact doubles (repr-round-tripped) to "
            "Python f'{v:.6f}' text — the rescue CSV formatter must produce identical strings."
        ),
        "idle_seq_base": IDLE_SEQ_BASE,
        "vectors": vectors,
        "float6": _float6_table(),
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(vectors)} vectors to {out_path}")
    for v in vectors:
        print(f"  {v['name']:38s} {v['message']:15s} {len(v['hex']) // 2:3d} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
