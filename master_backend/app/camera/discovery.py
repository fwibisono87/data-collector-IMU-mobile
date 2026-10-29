r"""Camera device discovery.

``list_devices()`` returns the API :class:`CameraDevice` list for the host:

* **Windows (dshow)** — parse the stderr of
  ``ffmpeg -hide_banner -list_devices true -f dshow -i dummy``. Both output styles are
  supported: the new one (``"Name" (video)`` followed by an ``Alternative name``
  line) and the old one (``DirectShow video devices`` / ``DirectShow audio devices``
  section headers, names without a suffix). The *alternative name*
  (``@device_pnp_\\?\usb#vid_…``) is the stable ``device_key``; two identical webcams
  share their friendly name, so the friendly name is only the ``label``.
* **Linux (v4l2)** — ``/dev/v4l/by-id/*-video-index0`` symlinks when present (the
  by-id path is the device key), falling back to ``/dev/video*`` nodes that answer
  ``ffmpeg -f v4l2 -list_formats all`` with a capture format list.
* **Fake** — ``IMU_CAMERAS=fake`` with ``IMU_FAKE_CAMERAS=n`` gives ``fake:0`` …
  ``fake:n-1``; the capture worker turns those into ``lavfi testsrc2`` processes.

Enumeration spawns a subprocess on Windows and probes device nodes on Linux, so the
result is cached for one second (the API contract: "re-enumerates (≤ 1/s)").
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..api.schema import CameraDevice
from .bins import FFmpegNotFound, ffmpeg_exe

logger = logging.getLogger(__name__)

CACHE_TTL_S = 1.0
_LIST_DEVICES_TIMEOUT_S = 10
_PROBE_TIMEOUT_S = 5
_DEFAULT_FAKE_COUNT = 2

#: ``[dshow @ 0x000001f2b3c4d5e0] `` prefix on every line of the listing.
_DSHOW_PREFIX = re.compile(r"^\[dshow @ (?:0x)?[0-9a-fA-F]+\]\s?")
_DSHOW_DEVICE = re.compile(r'^\s*"(?P<label>.+?)"(?:\s+\((?P<kind>video|audio|none)\))?\s*$')
_DSHOW_ALTERNATIVE = re.compile(r'^\s*Alternative name\s+"(?P<name>.*)"\s*$')

_cache_lock = threading.Lock()
_cache: tuple[float, tuple, list[CameraDevice]] | None = None


def invalidate_cache() -> None:
    """Drop the cached enumeration (tests, device hot-plug)."""
    global _cache
    with _cache_lock:
        _cache = None


def list_devices(force: bool = False) -> list[CameraDevice]:
    """The cameras the host exposes, cached for at most one second. Never raises."""
    global _cache
    config = _config_fingerprint()
    now = time.monotonic()
    with _cache_lock:
        if not force and _cache is not None:
            cached_at, cached_config, cached = _cache
            if cached_config == config and now - cached_at < CACHE_TTL_S:
                return list(cached)
    try:
        devices = _enumerate()
    except Exception:
        logger.exception("camera enumeration failed")
        devices = []
    with _cache_lock:
        _cache = (now, config, list(devices))
    return devices


def _config_fingerprint() -> tuple:
    return (
        os.getenv("IMU_CAMERAS", ""),
        os.getenv("IMU_FAKE_CAMERAS", ""),
    )


def _enumerate() -> list[CameraDevice]:
    mode = os.getenv("IMU_CAMERAS", "").strip().lower()
    if mode == "fake":
        return fake_devices()
    if sys.platform == "win32":
        return dshow_devices()
    return v4l2_devices()


# ---------------------------------------------------------------------------
# fake backend
# ---------------------------------------------------------------------------
def fake_count() -> int:
    raw = os.getenv("IMU_FAKE_CAMERAS", "").strip()
    try:
        return max(0, int(raw)) if raw else _DEFAULT_FAKE_COUNT
    except ValueError:
        return _DEFAULT_FAKE_COUNT


def fake_devices() -> list[CameraDevice]:
    return [
        CameraDevice(device_key=f"fake:{i}", label=f"Fake camera {i}", backend="fake")
        for i in range(fake_count())
    ]


# ---------------------------------------------------------------------------
# Windows / dshow
# ---------------------------------------------------------------------------
def dshow_devices() -> list[CameraDevice]:
    """Run the dshow listing and parse it. Empty list when ffmpeg is missing."""
    try:
        exe = ffmpeg_exe()
    except FFmpegNotFound:
        return []
    try:
        proc = subprocess.run(
            [exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=_LIST_DEVICES_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.exception("dshow device listing failed")
        return []
    # ffmpeg exits non-zero for `-i dummy`; the listing is on stderr either way.
    return parse_dshow_devices(proc.stderr)


def parse_dshow_devices(stderr: str) -> list[CameraDevice]:
    """Parse ``ffmpeg -list_devices true -f dshow -i dummy`` stderr into devices.

    Handles the new per-line style (``"Name" (video)``) and the old section style
    (``DirectShow video devices`` / ``DirectShow audio devices``). ``(audio)`` and
    ``(none)`` entries are skipped; ``device_key`` is the alternative name when one
    follows, else the friendly name (better a colliding key than no device).
    """
    devices: list[CameraDevice] = []
    pending_label: str | None = None
    in_audio_section = False

    def flush_pending() -> None:
        nonlocal pending_label
        if pending_label is not None:
            devices.append(CameraDevice(
                device_key=pending_label, label=pending_label, backend="dshow",
            ))
            pending_label = None

    for line in stderr.splitlines():
        stripped = _DSHOW_PREFIX.sub("", line, count=1)
        if "DirectShow video devices" in stripped:
            flush_pending()
            in_audio_section = False
            continue
        if "DirectShow audio devices" in stripped:
            flush_pending()
            in_audio_section = True
            continue

        alt = _DSHOW_ALTERNATIVE.match(stripped)
        if alt:
            if pending_label is not None:
                devices.append(CameraDevice(
                    device_key=alt.group("name"), label=pending_label, backend="dshow",
                ))
                pending_label = None
            continue

        device = _DSHOW_DEVICE.match(stripped)
        if device:
            flush_pending()
            kind = device.group("kind")
            if kind is None:
                # Old style: the section header decides.
                if not in_audio_section:
                    pending_label = device.group("label")
            elif kind == "video":
                pending_label = device.group("label")
            # audio / none entries are skipped.
    flush_pending()
    return devices


# ---------------------------------------------------------------------------
# Linux / v4l2
# ---------------------------------------------------------------------------
def v4l2_devices() -> list[CameraDevice]:
    devices: list[CameraDevice] = []
    seen_nodes: set[str] = set()

    by_id = Path("/dev/v4l/by-id")
    if by_id.is_dir():
        try:
            links = sorted(by_id.glob("*-video-index0"))
        except OSError:
            links = []
        for link in links:
            try:
                node = str(link.resolve())
            except OSError:
                continue
            if not Path(node).exists() or node in seen_nodes:
                continue
            if _v4l2_can_capture(node):
                seen_nodes.add(node)
                devices.append(CameraDevice(
                    device_key=str(link),
                    label=_label_from_by_id(link.name),
                    backend="v4l2",
                ))

    for node_path in sorted(Path("/dev").glob("video*")):
        node = str(node_path)
        if node in seen_nodes or not node_path.is_char_device():
            continue
        if _v4l2_can_capture(node):
            seen_nodes.add(node)
            devices.append(CameraDevice(
                device_key=node, label=f"Video {node_path.name.removeprefix('video')}",
                backend="v4l2",
            ))
    return devices


def _label_from_by_id(name: str) -> str:
    """``usb-Webcam_01-video-index0`` → ``Webcam 01`` (best effort, readable)."""
    label = re.sub(r"^-video-index\d+$", "", name)
    for prefix in ("usb-", "pci-"):
        if label.startswith(prefix):
            label = label[len(prefix):]
            break
    return label.replace("_", " ").replace("-", " ").strip() or name


def _v4l2_can_capture(node: str) -> bool:
    """True when ``ffmpeg -f v4l2 -list_formats all`` reports capture formats.

    Metadata nodes (very common as ``/dev/video1`` next to a webcam) fail the ioctl
    and print no format table.
    """
    try:
        exe = ffmpeg_exe()
    except FFmpegNotFound:
        return False
    try:
        proc = subprocess.run(
            [exe, "-hide_banner", "-f", "v4l2", "-list_formats", "all", "-i", node],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    stderr = proc.stderr
    return ("Compressed:" in stderr or "Raw" in stderr) and "Inappropriate ioctl" not in stderr
