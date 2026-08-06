import 'dart:async';
import 'dart:math';
import 'package:flutter/foundation.dart';
import '../models/sensor_packet.dart';
import 'sensor_source.dart';

/// Owns the accelerometer + gyroscope acquisition and emits a [SensorPacket] on
/// every ticker interval.
///
/// Acquisition goes through a [SensorSource] seam so that a native adapter can
/// be substituted for sensors_plus without touching callers. Because Dart
/// singletons are per-isolate, an [InternalSensorManager] instantiated in the
/// foreground-task engine is independent of the one in the UI isolate.
class InternalSensorManager {
  InternalSensorManager._(this._source);

  static final InternalSensorManager _instance =
      InternalSensorManager._(const SensorsPlusSource());
  factory InternalSensorManager() => _instance;

  /// Construct an instance bound to an explicit [SensorSource]. Used by the
  /// task engine if a device-specific native adapter is required.
  factory InternalSensorManager.withSource(SensorSource source) =>
      InternalSensorManager._(source);

  final SensorSource _source;

  final _streamController = StreamController<SensorPacket>.broadcast();
  Stream<SensorPacket> get dataStream => _streamController.stream;

  double _lastAx = 0, _lastAy = 0, _lastAz = 0;
  double _lastGx = 0, _lastGy = 0, _lastGz = 0;
  DateTime? _lastAccTs;
  DateTime? _lastGyroTs;

  // Raw per-sensor liveness counters: incremented inside the platform stream
  // callbacks. A positive delta over a window proves real hardware events
  // arrived.
  int _accEventCount = 0;
  int _gyroEventCount = 0;
  int _lastAccEventAtMs = 0;
  int _lastGyroEventAtMs = 0;

  // Per-window sampling statistics: expected vs observed Hz, computed from real
  // sensor event timestamps (not from the ticker).
  DateTime? _accWindowStart;
  DateTime? _gyroWindowStart;
  int _accWindowCount = 0;
  int _gyroWindowCount = 0;
  double _accObservedHz = 0;
  double _gyroObservedHz = 0;

  StreamSubscription? _accSub;
  StreamSubscription? _gyroSub;
  Timer? _ticker;
  bool isRunning = false;
  int _currentFrequency = 100;
  DateTime? _prevEmittedAccTs;
  DateTime? _prevEmittedGyroTs;

  static const double _gravity = 9.80665;
  static const double _radToDeg = 180.0 / pi;

  /// Sampling period requested from the platform sensor, in ms. The OS treats this as a HINT:
  /// some handsets clamp to a slower supported rate, which is what produces held samples.
  /// Lower this (e.g. 5) to probe a device's true maximum rate.
  static const int kSensorSamplingPeriodMs = 10;

  void start({int frequency = 100}) {
    if (isRunning && _currentFrequency == frequency) return;
    if (isRunning) stop();

    _currentFrequency = frequency;
    final intervalMs = (1000 / frequency).round();
    final samplingPeriod = Duration(milliseconds: kSensorSamplingPeriodMs);

    _accSub = _source.accelerometer(samplingPeriod: samplingPeriod).listen(
      (e) {
        _lastAx = e.x;
        _lastAy = e.y;
        _lastAz = e.z;
        _lastAccTs = e.timestamp;
        _accEventCount++;
        _lastAccEventAtMs = DateTime.now().millisecondsSinceEpoch;
        _accWindowStart ??= e.timestamp;
        _accWindowCount++;
      },
      onError: (Object e) {
        debugPrint('InternalSensorManager: accelerometer stream error: $e');
      },
    );

    _gyroSub = _source.gyroscope(samplingPeriod: samplingPeriod).listen(
      (e) {
        _lastGx = e.x;
        _lastGy = e.y;
        _lastGz = e.z;
        _lastGyroTs = e.timestamp;
        _gyroEventCount++;
        _lastGyroEventAtMs = DateTime.now().millisecondsSinceEpoch;
        _gyroWindowStart ??= e.timestamp;
        _gyroWindowCount++;
      },
      onError: (Object e) {
        debugPrint('InternalSensorManager: gyroscope stream error: $e');
      },
    );

    _ticker = Timer.periodic(Duration(milliseconds: intervalMs), (_) {
      _updateObservedHz();
      _emitPacket();
    });

    isRunning = true;
  }

  void stop() {
    _accSub?.cancel();
    _gyroSub?.cancel();
    _ticker?.cancel();
    _accSub = null;
    _gyroSub = null;
    _ticker = null;
    isRunning = false;
    _accWindowStart = null;
    _gyroWindowStart = null;
    _accWindowCount = 0;
    _gyroWindowCount = 0;
    _accObservedHz = 0;
    _gyroObservedHz = 0;
    _prevEmittedAccTs = null;
    _prevEmittedGyroTs = null;
  }

  // Computes observed Hz from the accumulated sensor-event deltas, so the
  // reported statistic reflects real event timing rather than the ticker.
  void _updateObservedHz() {
    final now = DateTime.now();
    if (_accWindowStart != null && _accWindowCount > 0) {
      final secs = now.difference(_accWindowStart!).inMilliseconds / 1000.0;
      if (secs >= 1.0) {
        _accObservedHz = _accWindowCount / secs;
        _accWindowStart = now;
        _accWindowCount = 0;
      }
    }
    if (_gyroWindowStart != null && _gyroWindowCount > 0) {
      final secs = now.difference(_gyroWindowStart!).inMilliseconds / 1000.0;
      if (secs >= 1.0) {
        _gyroObservedHz = _gyroWindowCount / secs;
        _gyroWindowStart = now;
        _gyroWindowCount = 0;
      }
    }
  }

  // Returns the raw vector magnitude (g units) for preflight sanity check.
  double get currentAccMagnitude {
    final ax = _lastAx / _gravity;
    final ay = _lastAy / _gravity;
    final az = _lastAz / _gravity;
    return sqrt(ax * ax + ay * ay + az * az);
  }

  double get currentGyroMagnitude {
    final gx = _lastGx * _radToDeg;
    final gy = _lastGy * _radToDeg;
    final gz = _lastGz * _radToDeg;
    return sqrt(gx * gx + gy * gy + gz * gz);
  }

  int get accEventCount => _accEventCount;
  int get gyroEventCount => _gyroEventCount;
  int get lastAccEventAtMs => _lastAccEventAtMs;
  int get lastGyroEventAtMs => _lastGyroEventAtMs;
  DateTime? get lastAccTs => _lastAccTs;
  DateTime? get lastGyroTs => _lastGyroTs;
  double get accObservedHz => _accObservedHz;
  double get gyroObservedHz => _gyroObservedHz;
  double get currentFrequency => _currentFrequency.toDouble();

  void _emitPacket() {
    final held = _lastAccTs == _prevEmittedAccTs && _lastGyroTs == _prevEmittedGyroTs;
    _prevEmittedAccTs = _lastAccTs;
    _prevEmittedGyroTs = _lastGyroTs;
    _streamController.add(SensorPacket(
      accX: _lastAx / _gravity,
      accY: _lastAy / _gravity,
      accZ: _lastAz / _gravity,
      gyroX: _lastGx * _radToDeg,
      gyroY: _lastGy * _radToDeg,
      gyroZ: _lastGz * _radToDeg,
      timestamp: DateTime.now(),
      accTs: _lastAccTs,
      gyroTs: _lastGyroTs,
      isHeld: held,
    ));
  }
}
