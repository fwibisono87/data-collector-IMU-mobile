import 'dart:async';
import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/services.dart';

/// Audible + haptic connectivity alarm.
///
/// The phone is strapped to the subject, several metres from the operator; a coloured dot
/// in the app bar is not a signal anyone notices mid-trial. The alarm loops until the
/// connection is restored or the operator silences it (plan T13).
class AlertService {
  static final AlertService _i = AlertService._();
  factory AlertService() => _i;
  AlertService._();

  final _player = AudioPlayer();
  bool _alarming = false;
  bool _silenced = false;
  Timer? _haptic;

  bool get isAlarming => _alarming;
  bool get isSilenced => _silenced;

  Future<void> _configure() async {
    // Alarm usage so the tone is audible even with media volume low / DND media exempt.
    await _player.setAudioContext(const AudioContext(
      android: AudioContextAndroid(
        isSpeakerphoneOn: true,
        stayAwake: true,
        contentType: AndroidContentType.sonification,
        usageType: AndroidUsageType.alarm,
        audioFocus: AndroidAudioFocus.gainTransientMayDuck,
      ),
    ));
  }

  Future<void> startAlarm() async {
    if (_alarming) return;
    _alarming = true;
    _haptic = Timer.periodic(const Duration(seconds: 2), (_) => HapticFeedback.heavyImpact());
    if (_silenced) return;
    try {
      await _configure();
      await _player.setReleaseMode(ReleaseMode.loop);
      await _player.setVolume(1.0);
      await _player.play(AssetSource('sounds/alert.wav'));
    } catch (_) {
      // Audio unavailable (emulator, no audio focus): haptics + UI still alert.
    }
  }

  Future<void> stopAlarm({bool chime = true}) async {
    _haptic?.cancel();
    _haptic = null;
    if (!_alarming) return;
    _alarming = false;
    final wasSilenced = _silenced;
    _silenced = false;                    // re-arm for the next drop
    try {
      await _player.stop();
      if (chime && !wasSilenced) {
        await _player.setReleaseMode(ReleaseMode.stop);
        await _player.play(AssetSource('sounds/ok.wav'));
      }
    } catch (_) {}
  }

  Future<void> silence() async {          // operator acknowledges; haptics stop too
    _silenced = true;
    _haptic?.cancel();
    try {
      await _player.stop();
    } catch (_) {}
  }
}
