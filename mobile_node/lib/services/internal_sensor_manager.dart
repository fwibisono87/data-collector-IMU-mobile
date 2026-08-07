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
  // Last EMITTED values, for the value-equality half of the held test above.
  double _prevEmittedAx = double.nan;
  double _prevEmittedAy = double.nan;
  double _prevEmittedAz = double.nan;
  double _prevEmittedGx = double.nan;
  double _prevEmittedGy = double.nan;
  double _prevEmittedGz = double.nan;

  static const double _gravity = 9.80665;
  static const double _radToDeg = 180.0 / pi;

  /// Sampling period requested from the platform sensor, in ms — a HINT the OS is free to
  /// round to a supported ODR.
  ///
  /// This is deliberately FASTER than the 100 Hz emit rate, and the two are independent: the
  /// Timer.periodic below emits packets at `frequency`, reading whatever the sensor last
  /// cached. Requesting faster therefore costs nothing downstream; it only makes the cached
  /// value fresher.
  ///
  /// Why it matters (2026-08-07): the 2510DRA23E is dual-sourced. Handsets with the TDK
  /// icm4n607 delivered a true 100 Hz at a 10 ms request, while those with the Bosch bmi3xy
  /// ran the physical sensor at 50 Hz and DUPLICATED each reading to satisfy the 100 Hz
  /// delivery rate — 49% of rows were byte-identical repeats. `dumpsys sensorservice` reports
  /// maxRate=400 Hz on both parts, so 50 Hz was never a hardware ceiling; the HAL was simply
  /// picking a power-optimised ODR one step below what was asked for. Asking for 200 Hz pushes
  /// it up a step. Verify per handset with the true_hz readout on the dashboard — it counts
  /// DISTINCT readings, which is the only number that reveals this.
  static const int kSensorSamplingPeriodMs = 5;

  void start({int frequency = 100}) {
    if (isRunning && _currentFrequency == frequency) return;
    if (isRunning) stop();

    _currentFrequency = frequency;
    final intervalMs = (1000 / frequency).round();
    const samplingPeriod = Duration(milliseconds: kSensorSamplingPeriodMs);

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
    // Held == this packet carries no NEW hardware reading.
    //
    // Timestamps alone are not sufficient. A HAL that upsamples (Bosch bmi3xy on this
    // handset: physical ODR 50 Hz, delivery 100 Hz) re-fires the callback with a fresh event
    // timestamp but identical values, so the timestamp test scored those as fresh and the
    // phone reported a healthy 100 Hz while half its rows were duplicates. Comparing the
    // values too is what actually detects a repeat; the timestamp test is kept because it
    // catches the case where the callback did not fire at all between emits.
    final sameTs = _lastAccTs == _prevEmittedAccTs && _lastGyroTs == _prevEmittedGyroTs;
    final sameValues =
        _lastAx == _prevEmittedAx && _lastAy == _prevEmittedAy && _lastAz == _prevEmittedAz &&
        _lastGx == _prevEmittedGx && _lastGy == _prevEmittedGy && _lastGz == _prevEmittedGz;
    final held = sameTs || sameValues;
    _prevEmittedAx = _lastAx;
    _prevEmittedAy = _lastAy;
    _prevEmittedAz = _lastAz;
    _prevEmittedGx = _lastGx;
    _prevEmittedGy = _lastGy;
    _prevEmittedGz = _lastGz;
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
