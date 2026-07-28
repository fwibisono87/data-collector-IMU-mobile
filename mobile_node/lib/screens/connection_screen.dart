import 'package:flutter/material.dart';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import '../services/device_id_service.dart';
import '../services/foreground_service_handler.dart';
import '../services/websocket_client.dart';
import 'preflight_screen.dart';

const List<String> _roles = [
  'chest', 'waist', 'thigh_left', 'thigh_right',
  'ankle_left', 'ankle_right', 'wrist_left', 'wrist_right',
];

class ConnectionScreen extends StatefulWidget {
  const ConnectionScreen({super.key});

  @override
  State<ConnectionScreen> createState() => _ConnectionScreenState();
}

class _ConnectionScreenState extends State<ConnectionScreen> {
  final _ipController = TextEditingController();
  String _selectedRole = 'chest';
  bool _connecting = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
    // Fire-and-forget: shows the "allow background / notification" system dialogs once,
    // up front. Idempotent, never blocks the UI, does not affect connect().
    ForegroundServiceHandler().ensurePermissions();
  }

  Future<void> _load() async {
    final ip = await DeviceIdService().getLastServerIp();
    final role = await DeviceIdService().getDeviceRole();
    setState(() {
      _ipController.text = ip;
      _selectedRole = _roles.contains(role) ? role : 'chest';
    });
  }

  Future<void> _connect() async {
    final ip = _ipController.text.trim();
    if (ip.isEmpty) {
      setState(() => _error = 'Enter server IP');
      return;
    }
    setState(() {
      _connecting = true;
      _error = null;
    });

    await DeviceIdService().setDeviceRole(_selectedRole);
    final ok = await WebSocketClient().connect(ip);

    if (!mounted) return;
    if (ok) {
      Navigator.pushReplacement(
        context,
        MaterialPageRoute(builder: (_) => const PreflightScreen()),
      );
    } else {
      setState(() {
        _connecting = false;
        _error = WebSocketClient().lastConnectError ?? 'Could not connect to $ip:8000';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF1A1A2E),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Icon(Icons.sensors, size: 64, color: Colors.deepPurpleAccent),
              const SizedBox(height: 16),
              const Text(
                'IMU Telemetry Node',
                textAlign: TextAlign.center,
                style: TextStyle(
                  fontSize: 24,
                  fontWeight: FontWeight.bold,
                  color: Colors.white,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                'v2.0 · ${_selectedRole.toUpperCase()}',
                textAlign: TextAlign.center,
                style: const TextStyle(color: Colors.white54),
              ),
              const SizedBox(height: 40),

              // Role selector
              DropdownButtonFormField<String>(
                value: _selectedRole,
                dropdownColor: const Color(0xFF16213E),
                style: const TextStyle(color: Colors.white),
                decoration: _inputDecoration('Device Role'),
                items: _roles
                    .map((r) => DropdownMenuItem(
                          value: r,
                          child: Text(r, style: const TextStyle(color: Colors.white)),
                        ))
                    .toList(),
                onChanged: _connecting
                    ? null
                    : (v) => setState(() => _selectedRole = v!),
              ),
              const SizedBox(height: 16),

              // Server IP
              TextField(
                controller: _ipController,
                enabled: !_connecting,
                keyboardType: TextInputType.number,
                style: const TextStyle(color: Colors.white),
                decoration: _inputDecoration('Backend IP (e.g. 192.168.1.100)'),
              ),
              const SizedBox(height: 8),

              if (_error != null)
                Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: Text(
                    _error!,
                    style: const TextStyle(color: Colors.redAccent),
                  ),
                ),

              ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: Colors.deepPurpleAccent,
                  padding: const EdgeInsets.symmetric(vertical: 16),
                  shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(8)),
                ),
                onPressed: _connecting ? null : _connect,
                child: _connecting
                    ? const SizedBox(
                        height: 20,
                        width: 20,
                        child: CircularProgressIndicator(
                            color: Colors.white, strokeWidth: 2),
                      )
                    : const Text('CONNECT',
                        style: TextStyle(
                            fontSize: 16, fontWeight: FontWeight.bold)),
              ),
              const SizedBox(height: 12),
              TextButton(
                onPressed: () =>
                    FlutterForegroundTask.openIgnoreBatteryOptimizationSettings(),
                child: const Text(
                  'Phone keeps disconnecting? Tap to fix background limits',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: Colors.white38, fontSize: 12),
                ),
              ),
              const Text(
                'On Xiaomi/Redmi also enable Autostart + set Battery to No restrictions (see setup card).',
                textAlign: TextAlign.center,
                style: TextStyle(color: Colors.white24, fontSize: 11),

              // Third-party open-source license attribution (MIT/BSD compliance).
              TextButton.icon(
                onPressed: () => showLicensePage(
                  context: context,
                  applicationName: 'IMU Telemetry Node',
                  applicationVersion: 'v2.0.0',
                  applicationLegalese: '© 2026 IMU Telemetry Team',
                ),
                icon: const Icon(Icons.description_outlined,
                    size: 16, color: Colors.white38),
                label: const Text('Lisensi Pihak Ketiga',
                    style: TextStyle(color: Colors.white38, fontSize: 12)),
              ),
            ],
          ),
        ),
      ),
    );
  }

  InputDecoration _inputDecoration(String label) => InputDecoration(
        labelText: label,
        labelStyle: const TextStyle(color: Colors.white54),
        enabledBorder: OutlineInputBorder(
          borderSide: const BorderSide(color: Colors.white24),
          borderRadius: BorderRadius.circular(8),
        ),
        focusedBorder: OutlineInputBorder(
          borderSide: const BorderSide(color: Colors.deepPurpleAccent),
          borderRadius: BorderRadius.circular(8),
        ),
        filled: true,
        fillColor: const Color(0xFF16213E),
      );

  @override
  void dispose() {
    _ipController.dispose();
    super.dispose();
  }
}
