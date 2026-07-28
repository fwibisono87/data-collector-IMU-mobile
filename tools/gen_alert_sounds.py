"""
Generates the phone's connectivity-alarm sounds (connectivity_robustness_plan.md T13).
Run once from repo root: python tools/gen_alert_sounds.py
Commit the resulting mobile_node/assets/sounds/*.wav files.
"""
import math
import os
import struct
import wave

SR = 22050


def tone(f, ms, amp=0.35):
    n = int(SR * ms / 1000)
    return [
        int(amp * 32767 * math.sin(2 * math.pi * f * i / SR) * min(1, i / 200, (n - i) / 200))
        for i in range(n)
    ]


def sil(ms):
    return [0] * int(SR * ms / 1000)


def save(name, samples):
    out_dir = os.path.join("mobile_node", "assets", "sounds")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(struct.pack("<h", max(-32768, min(32767, v))) for v in samples))
    print(f"wrote {path} ({len(samples) / SR:.2f}s)")


if __name__ == "__main__":
    save("alert.wav", tone(880, 150) + sil(80) + tone(660, 150) + sil(700))   # loops as an alarm
    save("ok.wav", tone(660, 90) + sil(40) + tone(990, 140))                  # back online
