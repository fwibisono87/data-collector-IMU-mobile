import 'dart:convert';

// NTP-lite clock synchronization (CLAUDE.md §5).
class ClockSyncService {
  static final ClockSyncService _instance = ClockSyncService._internal();
  factory ClockSyncService() => _instance;
  ClockSyncService._internal();

  int _clockOffsetMs = 0;
  int _lastRttMs = 0;
  bool _synced = false;

  int get clockOffsetMs => _clockOffsetMs;
  int get lastRttMs => _lastRttMs;
  bool get isSynced => _synced;

  // Corrected current time in ms (aligned to laptop clock).
  int get nowMs => DateTime.now().millisecondsSinceEpoch + _clockOffsetMs;

  // Called by WebSocketClient to complete one sync round.
  // Returns offset in ms for this round, or null if invalid.
  int? processResponse({
    required int t0Ms,
    required int t1Ms,
    required int t2Ms,
    required int t3Ms,
  }) {
    final rtt = (t3Ms - t0Ms) - (t2Ms - t1Ms);
    if (rtt < 0) return null;
    _lastRttMs = rtt;
    final offset = ((t1Ms - t0Ms) + (t2Ms - t3Ms)) ~/ 2;
    return offset;
  }

  // Compute median offset from multiple samples and apply it.
  void applyOffsets(List<int> offsets) {
    if (offsets.isEmpty) return;
    final sorted = List<int>.from(offsets)..sort();
    final mid = sorted.length ~/ 2;
    _clockOffsetMs = sorted.length.isOdd
        ? sorted[mid]
        : (sorted[mid - 1] + sorted[mid]) ~/ 2;
    _synced = true;
  }

  void reset() {
    _clockOffsetMs = 0;
    _synced = false;
  }

  // Build a CLOCK_SYNC command payload.
  static String buildPayload(int t0Ms) =>
      jsonEncode({'t0_ms': t0Ms});

  // Parse a CLOCK_SYNC response payload.
  static Map<String, int>? parsePayload(String payload) {
    try {
      final m = jsonDecode(payload) as Map<String, dynamic>;
      return {
        't0_ms': (m['t0_ms'] as num).toInt(),
        't1_ms': (m['t1_ms'] as num).toInt(),
        't2_ms': (m['t2_ms'] as num).toInt(),
      };
    } catch (_) {
      return null;
    }
  }
}
