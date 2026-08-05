import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'pipeline_controller.dart';

// Owns the foreground service and keeps its notification alive.
//
// The actual IMU acquisition + telemetry pipeline runs inside the task engine
// isolate via [PipelineController] — NOT in the UI. The UI facade ([TaskBridge])
// only forwards commands (connect/disconnect) and renders mirrored state.
//
// OEM NOTE: On Xiaomi / OPPO / Samsung, Android battery optimization can kill
// foreground services regardless of this declaration. Each phone must have
// battery optimization disabled for this app manually before first use.
class ForegroundServiceHandler {
  static final ForegroundServiceHandler _instance =
      ForegroundServiceHandler._internal();
  factory ForegroundServiceHandler() => _instance;
  ForegroundServiceHandler._internal();

  static void initOptions() {
    FlutterForegroundTask.init(
      androidNotificationOptions: AndroidNotificationOptions(
        channelId: 'imu_telemetry',
        channelName: 'IMU Telemetry Service',
        channelDescription: 'Active during IMU data recording sessions',
        channelImportance: NotificationChannelImportance.HIGH,
        priority: NotificationPriority.HIGH,
      ),
      iosNotificationOptions: const IOSNotificationOptions(
        showNotification: false,
      ),
      foregroundTaskOptions: ForegroundTaskOptions(
        eventAction: ForegroundTaskEventAction.repeat(5000),
        autoRunOnBoot: true,
        allowWakeLock: true,
        allowWifiLock: true,
      ),
    );
  }

  Future<void> start() async {
    if (await FlutterForegroundTask.isRunningService) return;
    await FlutterForegroundTask.startService(
      notificationTitle: 'IMU Telemetry',
      notificationText: 'Connected — standby',
      callback: _startCallback,
    );
  }

  // Asks Android for the notification grant and the battery-optimization exemption —
  // the two biggest levers against OEM (MIUI/HyperOS) background-killing the foreground
  // service. Idempotent: safe to call repeatedly, only prompts when not already granted.
  Future<void> ensurePermissions() async {
    final np = await FlutterForegroundTask.checkNotificationPermission();
    if (np != NotificationPermission.granted) {
      await FlutterForegroundTask.requestNotificationPermission();
    }
    if (!await FlutterForegroundTask.isIgnoringBatteryOptimizations) {
      await FlutterForegroundTask.requestIgnoreBatteryOptimization();
    }
  }

  void updateStatus(String text) {
    FlutterForegroundTask.updateService(notificationText: text);
  }

  Future<void> stop() async {
    await FlutterForegroundTask.stopService();
  }

  void updateNotification(int sent, int buffered) {
    final text = buffered > 0
        ? 'Recording: $sent sent, $buffered buffered'
        : 'Recording: $sent packets sent';
    FlutterForegroundTask.updateService(notificationText: text);
  }
}

@pragma('vm:entry-point')
void _startCallback() {
  FlutterForegroundTask.setTaskHandler(_ImuTaskHandler());
}

class _ImuTaskHandler extends TaskHandler {
  final PipelineController _controller = PipelineController();

  @override
  Future<void> onStart(DateTime timestamp, TaskStarter starter) async {
    // Subscribes to the pipeline, advertises task_ready, and auto-reconnects
    // from persisted state so a restarted engine recovers without the UI.
    await _controller.init();
  }

  @override
  void onRepeatEvent(DateTime timestamp) {
    _controller.onTick();
  }

  @override
  void onReceiveData(Object data) {
    _controller.onCommand(data);
  }

  @override
  Future<void> onDestroy(DateTime timestamp) async {
    await _controller.shutdown();
  }
}
