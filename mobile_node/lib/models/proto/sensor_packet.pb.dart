// Hand-written protobuf serializer matching shared_contracts/sensor_packet.proto.
// Replace with protoc-generated output after running `make proto-dart`.
import 'dart:convert';
import 'dart:typed_data';

// ignore_for_file: non_constant_identifier_names

class SensorPacketProto {
  double accX;
  double accY;
  double accZ;
  double gyroX;
  double gyroY;
  double gyroZ;
  int timestampMs;
  int sequenceNumber;
  String deviceId;
  int schemaVersion;
  int rawTimestampMs;
  int accTsMs;
  int gyroTsMs;
  int sampleKind;

  SensorPacketProto({
    this.accX = 0.0,
    this.accY = 0.0,
    this.accZ = 0.0,
    this.gyroX = 0.0,
    this.gyroY = 0.0,
    this.gyroZ = 0.0,
    this.timestampMs = 0,
    this.sequenceNumber = 0,
    this.deviceId = '',
    this.schemaVersion = 2,
    this.rawTimestampMs = 0,
    this.accTsMs = 0,
    this.gyroTsMs = 0,
    this.sampleKind = 0,
  });

  Uint8List toBytes() {
    final w = _ProtoWriter();
    w.writeFloat(1, accX);
    w.writeFloat(2, accY);
    w.writeFloat(3, accZ);
    w.writeFloat(4, gyroX);
    w.writeFloat(5, gyroY);
    w.writeFloat(6, gyroZ);
    w.writeInt64(7, timestampMs);
    w.writeVarint(8, sequenceNumber);
    w.writeString(9, deviceId);
    w.writeVarint(10, schemaVersion);
    w.writeInt64(11, rawTimestampMs);
    w.writeInt64(12, accTsMs);
    w.writeInt64(13, gyroTsMs);
    w.writeVarint(14, sampleKind);
    return w.build();
  }
}

class DeviceRegisterProto {
  String deviceId;
  String deviceRole;
  String deviceModel;
  String androidVersion;
  String appVersion;
  int schemaVersion;

  DeviceRegisterProto({
    this.deviceId = '',
    this.deviceRole = '',
    this.deviceModel = '',
    this.androidVersion = '',
    this.appVersion = '',
    this.schemaVersion = 1,
  });

  Uint8List toBytes() {
    final w = _ProtoWriter();
    w.writeString(1, deviceId);
    w.writeString(2, deviceRole);
    w.writeString(3, deviceModel);
    w.writeString(4, androidVersion);
    w.writeString(5, appVersion);
    w.writeVarint(6, schemaVersion);
    return w.build();
  }
}

class _ProtoWriter {
  final List<int> _buf = [];

  void writeFloat(int field, double value) {
    _tag(field, 5);
    final d = ByteData(4)..setFloat32(0, value, Endian.little);
    _buf.addAll(d.buffer.asUint8List());
  }

  void writeInt64(int field, int value) {
    _tag(field, 0);
    _varint(value);
  }

  void writeVarint(int field, int value) {
    _tag(field, 0);
    _varint(value);
  }

  void writeString(int field, String value) {
    if (value.isEmpty) return;
    final encoded = utf8.encode(value);
    _tag(field, 2);
    _varint(encoded.length);
    _buf.addAll(encoded);
  }

  void _tag(int field, int wireType) => _varint((field << 3) | wireType);

  void _varint(int v) {
    while (v >>> 7 != 0) {
      _buf.add((v & 0x7F) | 0x80);
      v = v >>> 7;
    }
    _buf.add(v & 0x7F);
  }

  Uint8List build() => Uint8List.fromList(_buf);
}
