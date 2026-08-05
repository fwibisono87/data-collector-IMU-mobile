import 'package:sensors_plus/sensors_plus.dart';

/// A single accelerometer/gyroscope reading carrying the platform (sensor)
/// timestamp captured by the OS when the sample fired.
class ImuEvent {
  final double x;
  final double y;
  final double z;

  /// Hardware sensor timestamp (microseconds since epoch), from sensors_plus.
  final DateTime timestamp;

  const ImuEvent(this.x, this.y, this.z, this.timestamp);
}

/// Platform seam for IMU acquisition.
///
/// This is the ONLY place sensors_plus (or a future native adapter) is touched.
/// Every other component (InternalSensorManager, the pipeline, the UI) talks to
/// this interface. If sensors_plus turns out not to deliver events inside the
/// foreground-task engine on a given device/OEM, replace the single
/// [SensorsPlusSource] implementation with a native channel adapter — nothing
/// else in the codebase changes.
abstract class SensorSource {
  Stream<ImuEvent> accelerometer({Duration samplingPeriod});
  Stream<ImuEvent> gyroscope({Duration samplingPeriod});
}

/// Default implementation backed by the sensors_plus plugin.
class SensorsPlusSource implements SensorSource {
  const SensorsPlusSource();

  @override
  Stream<ImuEvent> accelerometer({
    Duration samplingPeriod = const Duration(milliseconds: 10),
  }) {
    return accelerometerEventStream(samplingPeriod: samplingPeriod)
        .map((e) => ImuEvent(e.x, e.y, e.z, e.timestamp));
  }

  @override
  Stream<ImuEvent> gyroscope({
    Duration samplingPeriod = const Duration(milliseconds: 10),
  }) {
    return gyroscopeEventStream(samplingPeriod: samplingPeriod)
        .map((e) => ImuEvent(e.x, e.y, e.z, e.timestamp));
  }
}
