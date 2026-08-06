"""
Hand-written protobuf parser matching shared_contracts/sensor_packet.proto.
Replace with protoc-generated output after running `make proto-python`.
"""
import struct
from dataclasses import dataclass, field


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


@dataclass
class SensorPacket:
    acc_x: float = 0.0
    acc_y: float = 0.0
    acc_z: float = 0.0
    gyro_x: float = 0.0
    gyro_y: float = 0.0
    gyro_z: float = 0.0
    timestamp_ms: int = 0
    sequence_number: int = 0
    device_id: str = ""
    schema_version: int = 1
    raw_timestamp_ms: int = 0
    acc_ts_ms: int = 0
    gyro_ts_ms: int = 0
    sample_kind: int = 0

    @classmethod
    def from_bytes(cls, data: bytes) -> "SensorPacket":
        pkt = cls()
        pos = 0
        while pos < len(data):
            tag, pos = _read_varint(data, pos)
            field_number = tag >> 3
            wire_type = tag & 0x7

            if wire_type == 5:  # 32-bit (float)
                val = struct.unpack_from("<f", data, pos)[0]
                pos += 4
                match field_number:
                    case 1: pkt.acc_x = val
                    case 2: pkt.acc_y = val
                    case 3: pkt.acc_z = val
                    case 4: pkt.gyro_x = val
                    case 5: pkt.gyro_y = val
                    case 6: pkt.gyro_z = val

            elif wire_type == 0:  # varint
                val, pos = _read_varint(data, pos)
                match field_number:
                    case 7: pkt.timestamp_ms = val
                    case 8: pkt.sequence_number = val
                    case 10: pkt.schema_version = val
                    case 11: pkt.raw_timestamp_ms = val
                    case 12: pkt.acc_ts_ms = val
                    case 13: pkt.gyro_ts_ms = val
                    case 14: pkt.sample_kind = val

            elif wire_type == 2:  # length-delimited
                length, pos = _read_varint(data, pos)
                chunk = data[pos : pos + length]
                pos += length
                if field_number == 9:
                    pkt.device_id = chunk.decode("utf-8", errors="replace")

            else:
                break  # unknown wire type — stop parsing

        return pkt


@dataclass
class DeviceRegister:
    device_id: str = ""
    device_role: str = ""
    device_model: str = ""
    android_version: str = ""
    app_version: str = ""
    schema_version: int = 1

    @classmethod
    def from_bytes(cls, data: bytes) -> "DeviceRegister":
        reg = cls()
        pos = 0
        while pos < len(data):
            tag, pos = _read_varint(data, pos)
            field_number = tag >> 3
            wire_type = tag & 0x7

            if wire_type == 2:
                length, pos = _read_varint(data, pos)
                chunk = data[pos : pos + length]
                pos += length
                text = chunk.decode("utf-8", errors="replace")
                match field_number:
                    case 1: reg.device_id = text
                    case 2: reg.device_role = text
                    case 3: reg.device_model = text
                    case 4: reg.android_version = text
                    case 5: reg.app_version = text
            elif wire_type == 0:
                val, pos = _read_varint(data, pos)
                if field_number == 6:
                    reg.schema_version = val
            else:
                break

        return reg

    @property
    def is_valid(self) -> bool:
        return bool(self.device_id and self.device_role)
