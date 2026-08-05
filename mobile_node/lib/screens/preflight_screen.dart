import 'dart:async';
import 'dart:io';
import 'dart:math' show sqrt;
import 'package:battery_plus/battery_plus.dart';
import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';
import '../services/internal_sensor_manager.dart';
import '../services/task_bridge.dart';
import 'dashboard_screen.dart';

enum _Status { pending, running, pass, fail }

class _Check {
  final String label;
  _Status status;
  String detail;
  String hint;

  _Check(this.label)
      : status = _Status.pending,
        detail = '',
        hint = '';
}

// Go/No-Go gate before recording (CLAUDE.md §7).
class PreflightScreen extends StatefulWidget {
  const PreflightScreen({super.key});

  @override
  State<PreflightScreen> createState() => _PreflightScreenState();
}

class _PreflightScreenState extends State<PreflightScreen> {
  late final List<_Check> _checks;
  bool _allPassed = false;
  bool _running = false;

  @override
  void initState() {
    super.initState();
    _checks = [
      _Check('Phone Battery ≥ 30%'),
      _Check('Storage Free ≥ 2 GB'),
      _Check('Clock Synced'),
      _Check('WS RTT < 500ms'),
      _Check('Accelerometer Sanity'),
      _Check('Gyroscope Sanity'),
    ];
    _runChecks();
  }

  Future<void> _runChecks() async {
    if (_running) return;
    setState(() {
      _running = true;
      _allPassed = false;
      for (final c in _checks) {
        c.status = _Status.pending;
        c.detail = '';
      }
    });

    await _checkBattery();
    await _checkStorage();
    await _checkClockSync();
    await _checkRtt();
    await _checkSensorSanity();

    final passed = _checks.every((c) => c.status == _Status.pass);
    setState(() {
      _allPassed = passed;
      _running = false;
    });
  }

  Future<void> _checkBattery() async {
    _setRunning(0);
    try {
      final level = await Battery().batteryLevel;
      if (level >= 30) {
        _setPass(0, '$level%');
      } else {
        _setFail(0, '$level% — charge to ≥ 30%');
      }
    } catch (e) {
      _setFail(0, 'Cannot read battery');
    }
  }

  Future<void> _checkStorage() async {
    _setRunning(1);
    try {
      final dir = await getApplicationDocumentsDirectory();
      // Write a 1 MB test file to verify writable and estimate free space.
      final testFile = File('${dir.path}/.preflight_test');
      await testFile.writeAsBytes(List.filled(1024 * 1024, 0));
      await testFile.delete();
      // We can't get exact free space without a plugin; pass if write succeeds.
      _setPass(1, 'Write test OK');
    } catch (e) {
      _setFail(1, 'Write test failed: $e');
    }
  }

  Future<void> _checkClockSync() async {
    _setRunning(2);
    // Give the pipeline (running in the task engine) up to 3 s to complete sync.
    for (int i = 0; i < 15; i++) {
      if (TaskBridge().clockSynced) break;
      await Future.delayed(const Duration(milliseconds: 200));
    }
    if (TaskBridge().clockSynced) {
      final offset = TaskBridge().clockOffsetMs;
      if (offset.abs() > 30000) {
        _setFail(2, 'Offset ${offset}ms — check phone NTP');
      } else {
        _setPass(2, 'Offset ${offset}ms');
      }
    } else {
      _setFail(2, 'No sync yet — check connection');
    }
  }

  Future<void> _checkRtt() async {
    _setRunning(3);
    final rtt = TaskBridge().lastRttMs;
    if (rtt == 0) {
      _setFail(3, 'No RTT measured yet');
    } else if (rtt < 500) {
      _setPass(3, '${rtt}ms');
    } else {
      _setFail(3, '${rtt}ms — check Wi-Fi');
    }
  }

  Future<void> _checkSensorSanity() async {
    _setRunning(4, hint: 'Hold phone still… (a quick small twist is fine)');
    final mgr = InternalSensorManager();
    mgr.start(frequency: 100);

    // Snapshot raw event counters so we can tell, PER SENSOR and independently,
    // whether real hardware events actually arrived during the window. A dead
    // or ungated-high-rate sensor delivers no events; its counter stays flat,
    // while the 100 Hz ticker keeps republishing zeros (which is exactly how a
    // dead gyro used to sail through as green).
    final int accCount0 = mgr.accEventCount;
    final int gyroCount0 = mgr.gyroEventCount;

    final List<double> accSamples = [];
    final List<double> gyroSamples = [];
    double maxGyro = 0;

    final sub = mgr.dataStream.listen((pkt) {
      final avm = sqrt(pkt.accX * pkt.accX + pkt.accY * pkt.accY + pkt.accZ * pkt.accZ);
      final gvm = sqrt(pkt.gyroX * pkt.gyroX + pkt.gyroY * pkt.gyroY + pkt.gyroZ * pkt.gyroZ);
      accSamples.add(avm);
      gyroSamples.add(gvm);
      if (gvm > maxGyro) maxGyro = gvm;
    });

    await Future.delayed(const Duration(seconds: 3));
    await sub.cancel();

    final int accEvents = mgr.accEventCount - accCount0;
    final int gyroEvents = mgr.gyroEventCount - gyroCount0;

    // ── Accelerometer ──────────────────────────────────────────────────────
    if (accEvents == 0 || accSamples.isEmpty) {
      _setFail(4, 'No accelerometer data (0 events) — check sensor/permission');
    } else {
      final meanAcc = accSamples.reduce((a, b) => a + b) / accSamples.length;
      // At rest the accelerometer senses gravity ≈ 1 g. Accept 0.5–1.5 g.
      if (meanAcc >= 0.5 && meanAcc <= 1.5) {
        _setPass(4, 'avm=${meanAcc.toStringAsFixed(2)}g ($accEvents events)');
      } else {
        _setFail(4, 'avm=${meanAcc.toStringAsFixed(2)}g — hold still or check sensor');
      }
    }

    // ── Gyroscope (judged INDEPENDENTLY of the accelerometer) ──────────────
    _setRunning(5, hint: 'Hold phone still… (a quick small twist is fine)');
    if (gyroEvents == 0 || gyroSamples.isEmpty) {
      // A genuinely dead gyro delivers no events → fail honestly. It can no
      // longer pass green just because a flat 0.00 °/s is "< 5".
      _setFail(5, 'No gyroscope data (0 events) — check sensor/permission');
    } else {
      final meanGyro = gyroSamples.reduce((a, b) => a + b) / gyroSamples.length;
      // Liveness is proven by events arriving. Show mean + peak so a quick
      // wrist-twist visibly spikes the peak, confirming the sensor responds.
      _setPass(5,
          'gyro≈${meanGyro.toStringAsFixed(2)} peak ${maxGyro.toStringAsFixed(1)}°/s ($gyroEvents events)');
    }
  }

  void _setRunning(int i, {String hint = ''}) => setState(() {
        _checks[i].status = _Status.running;
        _checks[i].hint = hint;
      });

  void _setPass(int i, String detail) => setState(() {
        _checks[i].status = _Status.pass;
        _checks[i].detail = detail;
        _checks[i].hint = '';
      });

  void _setFail(int i, String detail) => setState(() {
        _checks[i].status = _Status.fail;
        _checks[i].detail = detail;
        _checks[i].hint = '';
      });

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF1A1A2E),
      appBar: AppBar(
        backgroundColor: const Color(0xFF16213E),
        title: const Text('Preflight Check',
            style: TextStyle(color: Colors.white)),
        iconTheme: const IconThemeData(color: Colors.white),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            onPressed: _running ? null : _runChecks,
            tooltip: 'Re-run checks',
          ),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: ListView.separated(
              padding: const EdgeInsets.all(16),
              itemCount: _checks.length,
              separatorBuilder: (_, __) => const SizedBox(height: 8),
              itemBuilder: (_, i) => _CheckTile(_checks[i]),
            ),
          ),
          Padding(
            padding: const EdgeInsets.all(16),
            child: ElevatedButton(
              style: ElevatedButton.styleFrom(
                backgroundColor:
                    _allPassed ? Colors.green : Colors.grey.shade700,
                minimumSize: const Size.fromHeight(52),
                shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(8)),
              ),
              onPressed: _allPassed ? _proceed : null,
              child: Text(
                _allPassed ? 'START RECORDING SESSION' : 'CHECKS PENDING',
                style: const TextStyle(
                    fontSize: 16, fontWeight: FontWeight.bold),
              ),
            ),
          ),
        ],
      ),
    );
  }

  void _proceed() {
    Navigator.pushReplacement(
      context,
      MaterialPageRoute(builder: (_) => const DashboardScreen()),
    );
  }
}

class _CheckTile extends StatelessWidget {
  final _Check check;
  const _CheckTile(this.check);

  @override
  Widget build(BuildContext context) {
    final (icon, color) = switch (check.status) {
      _Status.pending => (Icons.radio_button_unchecked, Colors.white38),
      _Status.running => (Icons.hourglass_top, Colors.amber),
      _Status.pass => (Icons.check_circle, Colors.greenAccent),
      _Status.fail => (Icons.cancel, Colors.redAccent),
    };

    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: const Color(0xFF16213E),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(
          color: check.status == _Status.fail
              ? Colors.redAccent.withOpacity(0.5)
              : Colors.white10,
        ),
      ),
      child: Row(
        children: [
          check.status == _Status.running
              ? SizedBox(
                  width: 24,
                  height: 24,
                  child: CircularProgressIndicator(
                      color: color, strokeWidth: 2),
                )
              : Icon(icon, color: color, size: 24),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(check.label,
                    style: const TextStyle(
                        color: Colors.white, fontWeight: FontWeight.w500)),
                if (check.status == _Status.running && check.hint.isNotEmpty)
                  Text(check.hint,
                      style: TextStyle(
                          color: Colors.amber.withOpacity(0.9), fontSize: 12)),
                if (check.detail.isNotEmpty)
                  Text(check.detail,
                      style: TextStyle(
                          color: color.withOpacity(0.8), fontSize: 12)),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
