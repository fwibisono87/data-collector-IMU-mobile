"""Unit tests for the dshow stderr parser (spec §1 fixtures).

Fixtures are written out as raw text: two identical "USB Camera" devices with
different alternative names, an integrated camera, a virtual camera, audio
devices interleaved between them, and both ffmpeg output styles.
"""
from __future__ import annotations

from master_backend.app.camera.discovery import parse_dshow_devices

# New style: one line per device with the kind suffix, alternative name on the
# next line. Audio devices are interleaved and must be skipped.
NEW_STYLE = r"""ffmpeg version 6.0 Copyright (c) 2000-2023 the FFmpeg developers
[dshow @ 0000021e8c5f2c00] "USB Camera" (video)
[dshow @ 0000021e8c5f2c00]   Alternative name "@device_pnp_\\?\usb#vid_5348&pid_9832#5&2ab2f9c8&0&2#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
[dshow @ 0000021e8c5f2c00] "Microphone (USB Camera)" (audio)
[dshow @ 0000021e8c5f2c00]   Alternative name "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{7b3a2c11-0001-0000}"
[dshow @ 0000021e8c5f2c00] "USB Camera" (video)
[dshow @ 0000021e8c5f2c00]   Alternative name "@device_pnp_\\?\usb#vid_5348&pid_9832#5&2ab2f9c8&1&7#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
[dshow @ 0000021e8c5f2c00] "Integrated Camera" (video)
[dshow @ 0000021e8c5f2c00]   Alternative name "@device_pnp_\\?\usb#vid_0bda&pid_5510#200901010001#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
[dshow @ 0000021e8c5f2c00] "Stereo Mix (Realtek(R) Audio)" (audio)
[dshow @ 0000021e8c5f2c00]   Alternative name "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{7b3a2c11-0002-0000}"
[dshow @ 0000021e8c5f2c00] "OBS Virtual Camera" (video)
[dshow @ 0000021e8c5f2c00]   Alternative name "@device_sw_{d0b2b815-9567-4f34-8f4e-a7b1b1777b7b}"
[dshow @ 0000021e8c5f2c00] "Some Capture Filter" (none)
[dshow @ 0000021e8c5f2c00]   Alternative name "@device_sw_{00000000-0000-0000-0000-000000000000}"
"""

# Old style: section headers decide what is video and what is audio.
OLD_STYLE = r"""ffmpeg version n4.4 Copyright (c) 2000-2021 the FFmpeg developers
[dshow @ 0x000000000259e3c0] DirectShow video devices (some may be both video and audio devices)
[dshow @ 0x000000000259e3c0]  "Integrated Camera"
[dshow @ 0x000000000259e3c0]     Alternative name "@device_pnp_\\?\usb#vid_0bda&pid_5510#200901010001#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
[dshow @ 0x000000000259e3c0]  "USB Camera"
[dshow @ 0x000000000259e3c0]     Alternative name "@device_pnp_\\?\usb#vid_5348&pid_9832#5&2ab2f9c8&0&2#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
[dshow @ 0x000000000259e3c0] DirectShow audio devices (some may be both video and audio devices)
[dshow @ 0x000000000259e3c0]  "Microphone (Realtek(R) Audio)"
[dshow @ 0x000000000259e3c0]     Alternative name "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{7b3a2c11-0003-0000}"
[dshow @ 0x000000000259e3c0]  "Stereo Mix (Realtek(R) Audio)"
[dshow @ 0x000000000259e3c0]     Alternative name "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{7b3a2c11-0004-0000}"
"""


def test_new_style_video_only_in_order():
    devices = parse_dshow_devices(NEW_STYLE)
    assert [d.label for d in devices] == [
        "USB Camera", "USB Camera", "Integrated Camera", "OBS Virtual Camera"]
    assert all(d.backend == "dshow" for d in devices)
    # Audio and (none) entries never appear, not even as keys.
    keys = " ".join(d.device_key for d in devices)
    assert "wave_" not in keys
    assert "00000000-0000-0000" not in keys


def test_identical_labels_get_distinct_keys():
    devices = parse_dshow_devices(NEW_STYLE)
    usb = [d for d in devices if d.label == "USB Camera"]
    assert len(usb) == 2
    assert usb[0].device_key != usb[1].device_key
    assert usb[0].device_key.startswith("@device_pnp_")
    assert usb[1].device_key.startswith("@device_pnp_")


def test_alternative_name_is_the_key():
    devices = parse_dshow_devices(NEW_STYLE)
    obs = next(d for d in devices if d.label == "OBS Virtual Camera")
    assert obs.device_key == "@device_sw_{d0b2b815-9567-4f34-8f4e-a7b1b1777b7b}"


def test_old_style_sections():
    devices = parse_dshow_devices(OLD_STYLE)
    assert [d.label for d in devices] == ["Integrated Camera", "USB Camera"]
    assert all(d.device_key.startswith("@device_pnp_") for d in devices)
    assert all("wave_" not in d.device_key for d in devices)


def test_device_without_alternative_name_falls_back_to_label():
    stderr = '[dshow @ 0x0000021e8c5f2c00] "Nameless Cam" (video)\n'
    devices = parse_dshow_devices(stderr)
    assert len(devices) == 1
    assert devices[0].device_key == "Nameless Cam"
    assert devices[0].label == "Nameless Cam"


def test_empty_and_junk_stderr():
    assert parse_dshow_devices("") == []
    assert parse_dshow_devices("not ffmpeg output at all\n") == []
