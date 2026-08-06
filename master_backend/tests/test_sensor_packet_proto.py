"""Tests for the hand-written sensor_packet parser/serializer and CSV formatting.

The Dart writer (mobile_node/lib/models/proto/sensor_packet.pb.dart) mirrors the
_ProtoWriter encoding documented here: tag = (field << 3) | wiretype, varint
little-endian 7-bit groups, float32 little-endian for wiretype 5. We can't run
Dart, so we hand-encode the same bytes the Dart writer would produce and assert
the Python parser reads them back correctly — plus the v1 backward-compat path.
"""
import struct

import pytest

from master_backend.app.io_manager import _format_row
from master_backend.app.csv_schema import parse_row
from master_backend.proto.sensor_packet import SensorPacket


class Encoder:
    """Mirror of the Dart _ProtoWriter."""

    def __init__(self):
        self._buf = bytearray()

    def _varint(self, v):
        while v & ~0x7F:
            self._buf.append((v & 0x7F) | 0x80)
            v >>= 7
        self._buf.append(v & 0x7F)

    def _tag(self, field, wire_type):
        self._varint((field << 3) | wire_type)

    def float(self, field, value):
        self._tag(field, 5)
        self._buf += struct.pack("<f", value)

    def int64(self, field, value):
        self._tag(field, 0)
        self._varint(value)

    def varint(self, field, value):
        self._tag(field, 0)
        self._varint(value)

    def string(self, field, value):
        if not value:
            return
        self._tag(field, 2)
        enc = value.encode("utf-8")
        self._varint(len(enc))
        self._buf += enc

    def build(self):
        return bytes(self._buf)


def _encode_v2():
    e = Encoder()
    e.float(1, 1.25)
    e.float(2, -2.5)
    e.float(3, 3.75)
    e.float(4, 10.0)
    e.float(5, -20.0)
    e.float(6, 30.0)
    e.int64(7, 1700000000123)
    e.int64(8, 42)
    e.string(9, "device-abc")
    e.varint(10, 2)
    e.int64(11, 1700000000000)
    e.int64(12, 1700000000100)
    e.int64(13, 1700000000200)
    e.varint(14, 0)
    return e.build()


def _encode_v1():
    e = Encoder()
    e.float(1, 1.0)
    e.float(2, 2.0)
    e.float(3, 3.0)
    e.float(4, 4.0)
    e.float(5, 5.0)
    e.float(6, 6.0)
    e.int64(7, 1700000000000)
    e.int64(8, 1)
    e.string(9, "old-device")
    e.varint(10, 1)
    e.int64(11, 1699999999000)
    return e.build()


def test_round_trip_all_fields():
    data = _encode_v2()
    pkt = SensorPacket.from_bytes(data)
    assert pkt.acc_x == pytest.approx(1.25)
    assert pkt.acc_y == pytest.approx(-2.5)
    assert pkt.acc_z == pytest.approx(3.75)
    assert pkt.gyro_x == pytest.approx(10.0)
    assert pkt.gyro_y == pytest.approx(-20.0)
    assert pkt.gyro_z == pytest.approx(30.0)
    assert pkt.timestamp_ms == 1700000000123
    assert pkt.sequence_number == 42
    assert pkt.device_id == "device-abc"
    assert pkt.schema_version == 2
    assert pkt.raw_timestamp_ms == 1700000000000
    assert pkt.acc_ts_ms == 1700000000100
    assert pkt.gyro_ts_ms == 1700000000200
    assert pkt.sample_kind == 0


def test_backward_compat_v1_packet():
    data = _encode_v1()
    pkt = SensorPacket.from_bytes(data)
    assert pkt.schema_version == 1
    assert pkt.raw_timestamp_ms == 1699999999000
    assert pkt.acc_ts_ms == 0
    assert pkt.gyro_ts_ms == 0
    assert pkt.sample_kind == 0
    assert pkt.device_id == "old-device"


def test_sample_kind_one_round_trip():
    e = Encoder()
    e.int64(7, 1700000000123)
    e.varint(14, 1)
    pkt = SensorPacket.from_bytes(e.build())
    assert pkt.sample_kind == 1
    assert pkt.timestamp_ms == 1700000000123


def _v2_packet(**overrides):
    fields = dict(
        timestamp_ms=1700000000123,
        acc_x=0.1,
        acc_y=0.2,
        acc_z=0.3,
        gyro_x=1.0,
        gyro_y=2.0,
        gyro_z=3.0,
        sequence_number=7,
        device_id="dev",
        acc_ts_ms=0,
        gyro_ts_ms=0,
        sample_kind=0,
    )
    fields.update(overrides)
    return SensorPacket(**fields)


def test_format_row_v2_empty_ts_and_sk():
    row = _format_row(_v2_packet(), 3, "run")
    fields = row.rstrip("\n").split(",")
    assert len(fields) == 14
    assert fields[0] == "1700000000123"
    assert fields[7] == "3"
    assert fields[8] == "run"
    assert fields[9] == "7"
    assert fields[10] == "dev"
    assert fields[11] == ""
    assert fields[12] == ""
    assert fields[13] == "0"


def test_format_row_v2_populated_ts():
    acc_ts = 1700000000100
    gyro_ts = 1700000000200
    row = _format_row(
        _v2_packet(acc_ts_ms=acc_ts, gyro_ts_ms=gyro_ts, sample_kind=1),
        0,
        "0",
    )
    fields = row.rstrip("\n").split(",")
    assert len(fields) == 14
    assert fields[11] == str(acc_ts)
    assert fields[12] == str(gyro_ts)
    assert fields[13] == "1"


def test_format_row_parses_via_csv_schema():
    pkt = _v2_packet(acc_ts_ms=1700000000100, gyro_ts_ms=1700000000200, sample_kind=1)
    row = _format_row(pkt, 3, "run")
    fields = parse_row(row)
    assert fields is not None
    assert len(fields) == 14
    assert fields[0] == "1700000000123"
    assert fields[1] == "0.100000"
    assert fields[2] == "0.200000"
    assert fields[3] == "0.300000"
    assert fields[4] == "1.000000"
    assert fields[5] == "2.000000"
    assert fields[6] == "3.000000"
    assert fields[7] == "3"
    assert fields[8] == "run"
    assert fields[9] == "7"
    assert fields[10] == "dev"
    assert fields[11] == "1700000000100"
    assert fields[12] == "1700000000200"
    assert fields[13] == "1"
