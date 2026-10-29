"""One long-lived ffmpeg capture process per selected camera.

Process model (docs/rebuild/lanes/L5-cameras.md §2):

* a **capture worker** owns one ffmpeg child that writes MJPEG to stdout
  (``-c:v copy`` when the camera delivers MJPEG, ``-c:v mjpeg -q:v 4`` fallback);
* a **stdout pump thread** splits the byte stream into frames
  (:mod:`~master_backend.app.camera.jpeg`), stamps each with ``time.time_ns()`` and
  parses width/height from SOF0/SOF2;
* a **stderr pump thread** keeps a bounded ring buffer whose last line becomes the
  worker's ``error``;
* **subscribers** (recorder queue, preview, calibration grab) get frames through
  bounded queues. The pump thread never blocks: an overflowing subscriber loses
  frames (oldest-first or newest-first per subscriber) and they are counted in
  ``dropped_frames``.

States: ``opening`` (spawned, no frame yet), ``live``, ``lost`` (process died; a
respawn is attempted every 2 s) and ``error`` (open failed with both the MJPEG and
the native-format command; also retried every 2 s). Shutdown writes ``q`` to stdin,
waits 5 s, then kills; on Windows the child is in the Job Object from
:mod:`~master_backend.app.camera.bins`.

The fake backend (``fake:<n>`` device keys) renders
``testsrc2=size=(w+64)x(h+64):rate=fps,crop=w:h:{32+dx}:{32+dy}``;
:meth:`CaptureWorker.set_offset` changes dx/dy and restarts the worker — the
calibration tests use it to shift the scene by a known offset.
"""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque
from typing import NamedTuple

from ..api.schema import CameraDevice
from . import bins
from .bins import FFmpegNotFound, ffmpeg_exe
from .jpeg import JpegFrameSplitter

logger = logging.getLogger(__name__)

#: No frame within this many seconds of spawning means "try the native format".
OPEN_TIMEOUT_S = 4.0
#: Backoff before reconnecting a dead capture, or retrying a failed open.
RECONNECT_BACKOFF_S = 2.0
#: Bounded ffmpeg stderr ring buffer (lines).
STDERR_RING_LINES = 50
#: fps is measured over this window.
FPS_WINDOW_S = 2.0
_READ_CHUNK = 256 * 1024


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


class Frame(NamedTuple):
    """One complete JPEG frame with its arrival stamp and parsed geometry."""

    data: bytes
    arrival_ns: int
    width: int | None
    height: int | None

    @property
    def arrival_ms(self) -> int:
        return self.arrival_ns // 1_000_000


class Subscription:
    """A bounded feed of frames from one worker.

    ``drop_oldest=True`` keeps the newest frames (the recorder wants the tail of an
    overrun, not its head); ``False`` drops the arriving frame (a preview only cares
    about the next one).
    """

    def __init__(self, maxsize: int, *, drop_oldest: bool) -> None:
        self.queue: queue.Queue[Frame] = queue.Queue(maxsize=maxsize)
        self.drop_oldest = drop_oldest
        self.dropped = 0

    def get(self, timeout: float | None = None) -> Frame | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None


class CaptureWorker:
    """Capture for one camera: process supervision, frames, fan-out, state."""

    def __init__(self, cam_id: str, device: CameraDevice, *,
                 width: int | None = None, height: int | None = None,
                 fps: int | None = None) -> None:
        self.cam_id = cam_id
        self.device = device
        self.width = width or _env_int("IMU_CAM_WIDTH", 1280)
        self.height = height or _env_int("IMU_CAM_HEIGHT", 720)
        self.fps_target = fps or _env_int("IMU_CAM_FPS", 30)

        self._lock = threading.Lock()
        self._state = "opening"
        self._error: str | None = None
        self.latest: Frame | None = None
        self.fps: float | None = None
        self.dropped_frames = 0
        self.frames_received = 0

        self._subs: list[Subscription] = []
        self._stderr_ring: deque[str] = deque(maxlen=STDERR_RING_LINES)
        self._frame_window: deque[int] = deque()

        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._stopping = threading.Event()
        self._restart = threading.Event()
        # Set by set_offset, cleared only once the restarted process is live:
        # wait_live() must not see the stale "live" state of the pre-restart
        # process (the monitor only notices _restart up to 200 ms later).
        self._restart_pending = threading.Event()
        self._fake_offset: tuple[int, int] = (0, 0)
        self._thread: threading.Thread | None = None
        self._pumps: list[threading.Thread] = []
        self.command_log: list[list[str]] = []  # what we spawned, for tests/diagnostics

    # -- public state -------------------------------------------------------
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def process(self):
        """The current ffmpeg child (or None). Tests kill it to simulate unplugging."""
        with self._proc_lock:
            return self._proc

    @property
    def restart_pending(self) -> bool:
        """True between set_offset() and the restarted process going live."""
        return self._restart_pending.is_set()

    def stderr_tail(self, n: int = 10) -> list[str]:
        with self._lock:
            return list(self._stderr_ring)[-n:]

    def snapshot(self) -> dict:
        """Cached state for the API ``Camera`` model; never blocks."""
        latest = self.latest
        return {
            "state": self.state,
            "width": latest.width if latest else None,
            "height": latest.height if latest else None,
            "fps": self.fps,
            "dropped_frames": self.dropped_frames,
            "error": self.error,
        }

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"capture-{self.cam_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Shut down politely (``q``, 5 s, kill) and stop respawning."""
        self._stopping.set()
        self._restart.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=RECONNECT_BACKOFF_S + 8.0)
        with self._lock:
            self._state = "lost" if self._state == "live" else self._state

    def set_offset(self, dx: int, dy: int) -> None:
        """Move the fake camera's crop window and restart the worker (tests)."""
        self._fake_offset = (int(dx), int(dy))
        self._restart_pending.set()
        self._restart.set()

    # -- subscribers --------------------------------------------------------
    def subscribe(self, maxsize: int, *, drop_oldest: bool) -> Subscription:
        sub = Subscription(maxsize, drop_oldest=drop_oldest)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def grab_frame_since(self, trigger_ns: int, timeout: float = 0.5) -> Frame | None:
        """The first frame that arrived at or after ``trigger_ns``.

        Used for simultaneous calibration stills (CALIBRATION.md): subscribing before
        the trigger and taking the first fresh frame bounds the skew to about one
        frame interval instead of "latest frame, up to a frame old".
        """
        sub = self.subscribe(64, drop_oldest=True)
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                frame = sub.get(timeout=0.02)
                if frame is not None and frame.arrival_ns >= trigger_ns:
                    return frame
            return None
        finally:
            self.unsubscribe(sub)

    # -- internals ----------------------------------------------------------
    def _set_state(self, state: str, error: str | None = None) -> None:
        with self._lock:
            self._state = state
            if error is not None or state == "live":
                self._error = error

    def _last_stderr(self) -> str | None:
        with self._lock:
            return self._stderr_ring[-1] if self._stderr_ring else None

    def _command(self, fallback: bool) -> list[str]:
        exe = ffmpeg_exe()
        base = [exe, "-hide_banner", "-loglevel", "error"]
        w, h, fps = self.width, self.height, self.fps_target
        backend = self.device.backend
        if backend == "fake":
            dx, dy = self._fake_offset
            src = (f"testsrc2=size={w + 64}x{h + 64}:rate={fps},"
                   f"crop={w}:{h}:{32 + dx}:{32 + dy}")
            return base + ["-re", "-f", "lavfi", "-i", src,
                           "-c:v", "mjpeg", "-q:v", "4", "-f", "mjpeg", "pipe:1"]
        if backend == "dshow":
            cmd = base + ["-f", "dshow", "-rtbufsize", "256M",
                          "-video_size", f"{w}x{h}", "-framerate", str(fps)]
            if not fallback:
                cmd += ["-vcodec", "mjpeg"]
            cmd += ["-i", f"video={self.device.device_key}"]
            cmd += ["-c:v", "mjpeg", "-q:v", "4"] if fallback else ["-c:v", "copy"]
            return cmd + ["-f", "mjpeg", "pipe:1"]
        # v4l2 (a by-id symlink resolves to its /dev/videoN node)
        node = os.path.realpath(self.device.device_key)
        cmd = base + ["-f", "v4l2"]
        if not fallback:
            cmd += ["-input_format", "mjpeg"]
        cmd += ["-video_size", f"{w}x{h}", "-framerate", str(fps), "-i", node]
        cmd += ["-c:v", "mjpeg", "-q:v", "4"] if fallback else ["-c:v", "copy"]
        return cmd + ["-f", "mjpeg", "pipe:1"]

    def _run(self) -> None:
        use_fallback = False
        while not self._stopping.is_set():
            self._restart.clear()
            try:
                cmd = self._command(use_fallback)
            except FFmpegNotFound as exc:
                self._set_state("error", str(exc))
                if self._wait(RECONNECT_BACKOFF_S, restartable=False):
                    continue
                break
            try:
                proc = bins.spawn(cmd)
            except OSError as exc:
                self._set_state("error", f"could not spawn ffmpeg: {exc}")
                if self._wait(RECONNECT_BACKOFF_S):
                    continue
                break
            self.command_log.append(cmd)
            with self._proc_lock:
                self._proc = proc
            self._frame_window.clear()
            self._set_state("opening")

            got_first = threading.Event()
            pumps = self._start_pumps(proc, got_first)

            opened = self._wait_first_frame(proc, got_first)
            if opened:
                self._set_state("live")
                self._restart_pending.clear()
                use_fallback = False
                died = self._monitor(proc)
                # Quit the child *before* joining the pumps: a pump blocked in
                # read1() only returns once the process is gone, and joining
                # first would keep the pre-restart frames flowing (and updating
                # `latest`) for the whole join timeout — the fake-offset tests
                # would measure the old crop window.
                if self._stopping.is_set() or self._restart.is_set():
                    bins.request_quit(proc, timeout=2.0)
                self._join_pumps(pumps)
                if self._stopping.is_set():
                    break
                if self._restart.is_set():
                    continue
                # The process died mid-stream: lost, 2 s backoff, reconnect.
                self._set_state("lost", self._last_stderr() or "capture process exited")
                logger.warning("capture %s died (rc=%s), reconnecting", self.cam_id,
                               proc.poll())
                if self._wait(RECONNECT_BACKOFF_S):
                    continue
                break
            # Open failed (no frame within the timeout, or the process exited).
            bins.request_quit(proc, timeout=2.0)
            self._join_pumps(pumps)
            if self._stopping.is_set():
                break
            if self._restart.is_set():
                continue
            if not use_fallback and self.device.backend in ("dshow", "v4l2"):
                # Retry once with the device's native format, transcoding to MJPEG.
                logger.info("capture %s: no MJPEG frames, retrying with native format",
                            self.cam_id)
                use_fallback = True
                continue
            self._set_state("error", self._last_stderr() or "no frame from camera")
            if self._wait(RECONNECT_BACKOFF_S):
                continue
            break

        with self._proc_lock:
            self._proc = None

    def _start_pumps(self, proc, got_first: threading.Event) -> list[threading.Thread]:
        splitter = JpegFrameSplitter()

        def pump_stdout() -> None:
            try:
                while True:
                    # read1(), not read(): read() on the buffered pipe waits for
                    # the full 256 KB, which delays every frame's arrival stamp
                    # by ~2 frame times (measured: 126-143 ms calibration skew
                    # where the spec allows 43 ms). read1() returns whatever one
                    # raw pipe read produced, so stamps stay within milliseconds
                    # of the frame actually arriving.
                    chunk = proc.stdout.read1(_READ_CHUNK)
                    if not chunk:
                        break
                    for jpeg in splitter.feed(chunk):
                        got_first.set()
                        self._on_frame(jpeg.data, jpeg.width, jpeg.height)
            except (OSError, ValueError, AttributeError):
                pass

        def pump_stderr() -> None:
            try:
                for line in proc.stderr:
                    text = line.decode(errors="replace").rstrip()
                    if text:
                        with self._lock:
                            self._stderr_ring.append(text)
            except (OSError, ValueError):
                pass

        threads = [
            threading.Thread(target=pump_stdout, name=f"cap-out-{self.cam_id}",
                             daemon=True),
            threading.Thread(target=pump_stderr, name=f"cap-err-{self.cam_id}",
                             daemon=True),
        ]
        for t in threads:
            t.start()
        with self._lock:
            self._pumps = threads
        return threads

    def _join_pumps(self, pumps: list[threading.Thread]) -> None:
        for t in pumps:
            t.join(timeout=2.0)

    def _wait_first_frame(self, proc, got_first: threading.Event) -> bool:
        deadline = time.monotonic() + OPEN_TIMEOUT_S
        while time.monotonic() < deadline:
            if got_first.wait(0.05):
                return True
            if proc.poll() is not None:
                return False
            if self._stopping.is_set() or self._restart.is_set():
                return False
        return False

    def _monitor(self, proc) -> bool:
        """Wait while the process is healthy. Returns True when it died."""
        while not self._stopping.is_set() and not self._restart.is_set():
            if proc.poll() is not None:
                return True
            time.sleep(0.2)
        return False

    def _wait(self, seconds: float, *, restartable: bool = True) -> bool:
        """Sleep, cut short by stop/restart. True when the loop should continue."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stopping.is_set():
                return False
            if restartable and self._restart.is_set():
                return True
            time.sleep(0.05)
        return not self._stopping.is_set()

    def _on_frame(self, data: bytes, width: int | None, height: int | None) -> None:
        now_ns = time.time_ns()
        frame = Frame(data, now_ns, width, height)
        self.latest = frame
        self.frames_received += 1

        window = self._frame_window
        window.append(now_ns)
        cutoff = now_ns - int(FPS_WINDOW_S * 1e9)
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= 2:
            span_s = (window[-1] - window[0]) / 1e9
            # Only report once the window spans a real interval; a startup burst
            # of two frames microseconds apart is not a rate (it read 683 fps).
            if span_s >= 0.5:
                self.fps = round((len(window) - 1) / span_s, 1)

        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            self._deliver(sub, frame)

    def _deliver(self, sub: Subscription, frame: Frame) -> None:
        """Non-blocking fan-out: overflow drops for this subscriber and is counted."""
        try:
            sub.queue.put_nowait(frame)
            return
        except queue.Full:
            pass
        if sub.drop_oldest:
            try:
                sub.queue.get_nowait()
                sub.queue.put_nowait(frame)
            except (queue.Empty, queue.Full):
                pass
        sub.dropped += 1
        self.dropped_frames += 1
