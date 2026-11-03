"""The backend must exit on SIGTERM even while a camera preview is streaming.

Previews are endless MJPEG responses. uvicorn waited for them forever, so the lifespan
never closed the camera service: the backend and its ffmpeg children stayed alive after
being told to stop (seen in end-to-end testing). run.py bounds the graceful shutdown.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow
REPO = Path(__file__).resolve().parents[3]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _children(pid: int) -> list[int]:
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    return [int(x) for x in out.split()]


def test_sigterm_with_open_preview_exits_and_reaps_ffmpeg(tmp_path):
    port = _free_port()
    env = {**os.environ, "PORT": str(port), "SSD_PATH": str(tmp_path / "ssd"),
           "RESCUE_PATH": str(tmp_path / "rescue"), "RECOVERY_PATH": str(tmp_path / "rec"),
           "IMU_CAMERAS": "fake", "IMU_FAKE_CAMERAS": "1"}
    proc = subprocess.Popen([sys.executable, str(REPO / "master_backend" / "run.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{base}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.25)
        req = urllib.request.Request(
            f"{base}/api/cameras/selection", method="PUT",
            data=b'{"cameras":[{"device_key":"fake:0"}]}',
            headers={"content-type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        stream = urllib.request.urlopen(f"{base}/api/cameras/cam1/preview.mjpg?w=320&fps=5",
                                        timeout=10)
        assert stream.read(2048)  # the preview is really streaming
        ffmpeg_children = _children(proc.pid)
        assert ffmpeg_children, "camera capture should be running"

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
        time.sleep(0.5)
        alive = [p for p in ffmpeg_children if Path(f"/proc/{p}").exists()]
        assert alive == [], f"ffmpeg children survived shutdown: {alive}"
    finally:
        if proc.poll() is None:
            proc.kill()
