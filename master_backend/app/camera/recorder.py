"""Recording: one VP8/VP9 encoder ffmpeg per camera, anchors, frames.csv, parts.

Process model (docs/rebuild/lanes/L5-cameras.md §3): a :class:`CameraRecorder` per
selected camera subscribes to its capture worker (bounded queue, 2 s of frames,
overflow drops the oldest and is counted) and owns a **writer thread** that feeds the
current encoder's stdin plus a **monitor thread** that watches worker and encoder
health.

* The encoder command is the spec's, with two additions the spec's own assertions
  require: ``-framerate <fps>`` on the MJPEG input (otherwise the demuxer assumes
  25 fps and the scheduler silently drops every frame arriving early for that grid —
  a 30 fps camera lost ~17 % of its frames in tests) and ``-fps_mode vfr`` (the
  browser's MediaRecorder files were VFR too; wallclock timestamps pass through
  untouched, so ``durationMs`` stays real time and frames.csv matches the file).
* A capture worker that goes ``lost`` mid-recording closes the current encoder
  cleanly (Duration + Cues), records a ``track_ended`` event and anchor, and opens
  ``_part2`` when the worker is live again; an encoder crash is a ``write_error``
  event and the same rollover.
* Anchors go through ``master_backend.app.cameras.cameras_mark`` on a dedicated
  event-loop thread (:class:`AnchorBridge`) so writer/monitor threads can write them
  at any time, including between the session core's awaits.

Shutdown closes every encoder's stdin and waits up to 15 s so the WebM muxer writes
Duration and Cues; an encoder that had to be killed is remuxed in place
(``ffmpeg -i f -c copy tmp.webm``), then every file is probed with ffprobe and
reported as a :class:`VideoFileReport`. ``stop_recording`` never raises for a single
camera's failure.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import io
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from .. import cameras as cameras_module
from . import bins, webm
from .bins import ffmpeg_exe
from .capture import CaptureWorker, Frame
from .interface import CameraEvent, CameraRecording, CameraStopResult, VideoFileReport

logger = logging.getLogger(__name__)

#: How much encoder stdin input failure is tolerated before the encoder is killed.
_ENCODER_CLOSE_TIMEOUT_S = 15.0
#: Total budget for stopping every camera (spec: must return within 30 s for 5).
STOP_ALL_BUDGET_S = 25.0

# One thread per encoder. Measured on 600 frames of 720p MJPEG (ffmpeg -benchmark):
# VP9 with libvpx's default threading + row-mt cost 13-14 CPU-s, single-threaded 6.4 CPU-s
# for the same output size: thread spin-up outweighs the parallelism at 720p, and five
# cameras already run five encoders in parallel. At the old setting a 30-minute 5-camera
# session kept only ~81 % of frames on a loaded machine. VP8 needs cpu-used 16 to be
# fast at all (cpu-used 8 with default threads took 128 CPU-s), which is why it looked
# unusable in the first benchmark.
_CODEC_TABLE = {
    "vp9": {
        "opts": ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8",
                 "-row-mt", "0", "-threads", "1", "-b:v", "2500k", "-g", "60"],
        "mime": "video/webm;codecs=vp9",
    },
    "vp8": {
        "opts": ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "16",
                 "-threads", "1", "-b:v", "2500k", "-g", "60"],
        "mime": "video/webm;codecs=vp8",
    },
}


def codec_config(name: str | None = None) -> dict:
    """Codec opts + mime for ``IMU_VIDEO_CODEC`` (default vp9)."""
    key = (name or os.getenv("IMU_VIDEO_CODEC", "") or "vp9").strip().lower()
    return _CODEC_TABLE.get(key, _CODEC_TABLE["vp9"]) | {"name": key
                                                         if key in _CODEC_TABLE else "vp9"}


def encoder_command(out_path: Path, codec: dict, fps: int) -> list[str]:
    return [
        ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
        "-f", "mjpeg", "-framerate", str(fps), "-use_wallclock_as_timestamps", "1",
        "-i", "pipe:0", "-fps_mode", "vfr",
        *codec["opts"], "-an", "-f", "webm", str(out_path),
    ]


# ---------------------------------------------------------------------------
# anchor bridge
# ---------------------------------------------------------------------------
class AnchorBridge:
    """A private event loop on its own thread for ``cameras_mark`` calls.

    Recorder threads must be able to persist anchors the moment they happen (the
    whole point of server-side anchors), but ``cameras_mark`` is async and the
    caller's loop may be between awaits — or closed, as in tests that drive the
    service with ``asyncio.run``. One long-lived loop keeps its per-session locks
    consistent.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever,
                                        name="camera-anchor-loop", daemon=True)
        self._thread.start()
        self._closed = False

    def submit(self, coro) -> concurrent.futures.Future:
        if self._closed:
            coro.close()
            raise RuntimeError("anchor bridge is closed")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def asubmit(self, coro):
        """Await a coroutine running on the bridge loop from any other loop."""
        return await asyncio.wrap_future(self.submit(coro))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        async def _cancel_pending() -> None:
            for task in asyncio.all_tasks(self._loop):
                if task is not asyncio.current_task():
                    task.cancel()

        try:
            asyncio.run_coroutine_threadsafe(_cancel_pending(), self._loop).result(
                timeout=2.0)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        self._loop.close()


async def _mark_anchor(session_id: str, *, cam_id: str, event: str, ts_ms: int,
                       device_key: str, label: str, mime: str) -> None:
    """One ``cameras.json`` anchor. Never raises: an anchor failure must not kill
    a recording — it is logged and, at worst, shows up as a missing mark."""
    try:
        await cameras_module.cameras_mark(session_id, {
            "cam_id": cam_id, "event": event, "ts_ms": ts_ms,
            "device_id": device_key, "label": label, "mime": mime,
        })
    except Exception:
        logger.exception("could not write camera anchor %s/%s", session_id, event)


# ---------------------------------------------------------------------------
# encoder
# ---------------------------------------------------------------------------
class Encoder:
    """One ``ffmpeg … -f webm`` child writing one part file."""

    def __init__(self, path: Path, codec: dict, fps: int, part: int) -> None:
        self.path = path
        self.part = part
        self.codec = codec
        self.command = encoder_command(path, codec, fps)
        self.proc = bins.spawn(self.command)
        self.frames_written = 0
        self.first_frame_ms: int | None = None
        self.last_frame_ms: int | None = None
        self.died = False           # process exited before we closed it
        self.killed = False         # we had to kill it (file needs a remux)
        self.closing = False        # set before close(): writer must not feed anymore
        self.exit_code: int | None = None

    def write(self, frame: Frame) -> bool:
        """Feed one frame. False when the encoder is gone (died flag set)."""
        if self.died or self.closing:
            return False
        try:
            self.proc.stdin.write(frame.data)
            # spawn() gives stdin a 1 MB buffer; without an explicit flush the
            # encoder sees data in ~1 MB bursts (tens of seconds of video), a
            # slow codec stalls the writer until the subscription queue drops
            # frames, and a SIGKILLed encoder loses everything buffered.
            self.proc.stdin.flush()
        except (OSError, ValueError):
            self._mark_died()
            return False
        if self.proc.poll() is not None:
            self._mark_died()
            return False
        self.frames_written += 1
        if self.first_frame_ms is None:
            self.first_frame_ms = frame.arrival_ms
        self.last_frame_ms = frame.arrival_ms
        return True

    def _mark_died(self) -> None:
        self.died = True
        self.exit_code = self.proc.poll()

    def close(self) -> None:
        """Close stdin and wait so the muxer writes Duration and Cues; kill after."""
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=_ENCODER_CLOSE_TIMEOUT_S)
        except Exception:
            logger.warning("encoder %s did not finish, killing", self.path.name)
            try:
                self.proc.kill()
                self.proc.wait(timeout=2.0)
            except Exception:
                pass
            self.killed = True
        self.exit_code = self.proc.poll()

    def abort(self) -> None:
        """Kill without finalising (service close aborts recordings)."""
        self.killed = True
        try:
            self.proc.kill()
        except OSError:
            pass


class FramesCsv:
    """``_extras/<sid>_<camId>_frames.csv``: per-frame arrival times, flushed 1/s."""

    FLUSH_INTERVAL_S = 1.0

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "w", newline="", encoding="utf-8")
        self._fh.write("part,frame_index,arrival_epoch_ms\n")
        self._buf = io.StringIO()
        self._rows = 0
        self._last_flush = time.monotonic()

    def add(self, part: int, frame_index: int, arrival_ms: int) -> None:
        self._buf.write(f"{part},{frame_index},{arrival_ms}\n")
        self._rows += 1
        if time.monotonic() - self._last_flush >= self.FLUSH_INTERVAL_S:
            self.flush()

    def flush(self) -> None:
        data = self._buf.getvalue()
        if data:
            self._buf = io.StringIO()
            self._fh.write(data)
            self._fh.flush()
        self._last_flush = time.monotonic()

    def close(self) -> None:
        self.flush()
        try:
            self._fh.close()
        except OSError:
            pass

    @property
    def rows(self) -> int:
        return self._rows


# ---------------------------------------------------------------------------
# per-camera recorder
# ---------------------------------------------------------------------------
class CameraInfo:
    """What the recorder needs to know about one selected camera."""

    def __init__(self, cam_id: str, device_key: str, label: str) -> None:
        self.cam_id = cam_id
        self.device_key = device_key
        self.label = label


class CameraRecorder:
    """Subscription + writer thread + monitor thread + parts for one camera."""

    MONITOR_INTERVAL_S = 0.2

    def __init__(self, session_id: str, folder: Path, info: CameraInfo,
                 worker: CaptureWorker, bridge: AnchorBridge, codec: dict) -> None:
        self.session_id = session_id
        self.folder = folder
        self.info = info
        self.worker = worker
        self.bridge = bridge
        self.codec = codec
        self.mime = codec["mime"]

        self.events: list[CameraEvent] = []
        self.parts: list[Encoder] = []
        self._lock = threading.Lock()
        self._current: Encoder | None = None
        self._stopping = threading.Event()
        self._lost_latched = False
        self._started_marked = False
        self._next_frame_index = 0
        self._frames_written_total = 0
        self._anchor_futures: list[concurrent.futures.Future] = []

        queue_frames = max(4, worker.fps_target * 2)  # 2 s worth, per spec
        self.sub = worker.subscribe(queue_frames, drop_oldest=True)
        extras = folder / "_extras"
        self.frames_csv = FramesCsv(
            extras / f"{session_id}_{info.cam_id}_frames.csv")
        self._writer = threading.Thread(target=self._write_loop,
                                        name=f"rec-{info.cam_id}", daemon=True)
        self._monitor = threading.Thread(target=self._monitor_loop,
                                         name=f"recmon-{info.cam_id}", daemon=True)

    # -- start/stop ---------------------------------------------------------
    def start(self) -> None:
        self._open_part()
        self._writer.start()
        self._monitor.start()

    def stop(self) -> None:
        """Stop consuming, close the current encoder cleanly, join threads."""
        self._stopping.set()
        self._writer.join(timeout=3.0)
        self._monitor.join(timeout=3.0)
        with self._lock:
            current, self._current = self._current, None
        if current is not None and not current.died:
            current.close()
        self.worker.unsubscribe(self.sub)
        self.frames_csv.close()

    def abort(self) -> None:
        """Kill everything without anchors or clean close (service shutdown)."""
        self._stopping.set()
        with self._lock:
            current, self._current = self._current, None
        if current is not None:
            current.abort()
        self.worker.unsubscribe(self.sub)
        self.frames_csv.close()

    def wait_first_frame(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._started_marked:
                    return True
            time.sleep(0.02)
        with self._lock:
            return self._started_marked

    # -- parts --------------------------------------------------------------
    def _part_path(self, part: int) -> Path:
        suffix = "" if part == 1 else f"_part{part}"
        return self.folder / f"{self.session_id}_{self.info.cam_id}_video_sync{suffix}.webm"

    def _open_part(self) -> Encoder | None:
        part = len(self.parts) + 1
        try:
            encoder = Encoder(self._part_path(part), self.codec,
                              self.worker.fps_target, part)
        except OSError as exc:
            logger.exception("could not spawn encoder for %s", self.info.cam_id)
            self._record_event("write_error", int(time.time() * 1000),
                               detail=f"encoder spawn failed: {exc}")
            return None
        with self._lock:
            self.parts.append(encoder)
            self._current = encoder
        return encoder

    # -- threads ------------------------------------------------------------
    def _write_loop(self) -> None:
        while not self._stopping.is_set():
            frame = self.sub.get(timeout=0.2)
            if frame is None:
                continue
            with self._lock:
                encoder = self._current
                stopping = self._stopping.is_set()
            if stopping or encoder is None:
                continue  # frames after stop are dropped; rollover waits for monitor
            if not encoder.write(frame):
                if encoder.closing or self._stopping.is_set():
                    continue  # rollover in progress; the frame is simply not written
                self._on_encoder_died(encoder)
                continue
            self._frames_written_total += 1
            self.frames_csv.add(encoder.part, self._next_frame_index,
                                frame.arrival_ms)
            self._next_frame_index += 1
            if not self._started_marked:
                self._started_marked = True
                self._on_first_frame(encoder, frame)

    def _on_first_frame(self, encoder: Encoder, frame: Frame) -> None:
        at_ms = frame.arrival_ms
        self._record_event("started", at_ms)
        for event in ("started", "flash"):
            self._submit_anchor(event, at_ms)

    def _on_encoder_died(self, encoder: Encoder) -> None:
        with self._lock:
            if self._current is encoder:
                self._current = None
        if self._stopping.is_set():
            return
        detail = f"encoder exited with code {encoder.exit_code}"
        logger.warning("encoder for %s died: %s", self.info.cam_id, detail)
        self._record_event("write_error", int(time.time() * 1000), detail=detail)
        self._submit_anchor("write_error", int(time.time() * 1000))
        # Same rollover as a camera dropout: the next part starts as soon as the
        # worker is (still) live.
        if self.worker.state == "live":
            self._open_part()

    def _monitor_loop(self) -> None:
        while not self._stopping.is_set():
            time.sleep(self.MONITOR_INTERVAL_S)
            if self._stopping.is_set():
                break
            state = self.worker.state
            if state != "live" and not self._lost_latched:
                self._lost_latched = True
                self._on_camera_lost()
            elif state == "live" and self._lost_latched:
                self._lost_latched = False
                with self._lock:
                    needs_part = self._current is None and not self._stopping.is_set()
                if needs_part:
                    self._open_part()

    def _on_camera_lost(self) -> None:
        at_ms = int(time.time() * 1000)
        self._record_event("track_ended", at_ms, detail="capture process lost")
        self._submit_anchor("track_ended", at_ms)
        with self._lock:
            encoder, self._current = self._current, None
            if encoder is not None:
                encoder.closing = True
        if encoder is not None and not encoder.died:
            # Close cleanly on a separate thread: stdin close + bounded wait so the
            # part already on disk stays seekable while frames keep being dropped.
            threading.Thread(target=encoder.close, daemon=True,
                             name=f"encclose-{self.info.cam_id}").start()

    # -- events/anchors -------------------------------------------------------
    def _record_event(self, kind: str, at_ms: int, detail: str | None = None) -> None:
        event = CameraEvent(
            key=f"{self.session_id}__{self.info.cam_id}__{at_ms}__"
                f"{uuid.uuid4().hex[:8]}",
            sessionId=self.session_id,
            camId=self.info.cam_id,
            type=kind,
            atMs=at_ms,
            detail=detail,
        )
        self.events.append(event)

    def _submit_anchor(self, event: str, ts_ms: int) -> None:
        try:
            future = self.bridge.submit(_mark_anchor(
                self.session_id, cam_id=self.info.cam_id, event=event, ts_ms=ts_ms,
                device_key=self.info.device_key, label=self.info.label,
                mime=self.mime))
            self._anchor_futures.append(future)
        except RuntimeError:
            logger.warning("anchor bridge closed; %s anchor lost", event)

    def wait_anchors(self, timeout: float = 5.0) -> None:
        """Wait until every submitted anchor write has finished (best effort)."""
        deadline = time.monotonic() + timeout
        for future in self._anchor_futures:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                future.result(timeout=remaining)
            except Exception:
                pass
        self._anchor_futures.clear()

    # -- finalisation ---------------------------------------------------------
    @property
    def started_at_ms(self) -> int:
        for encoder in self.parts:
            if encoder.first_frame_ms is not None:
                return encoder.first_frame_ms
        return 0

    @property
    def stopped_at_ms(self) -> int:
        for encoder in reversed(self.parts):
            if encoder.last_frame_ms is not None:
                return encoder.last_frame_ms
        return 0

    def usable_parts(self) -> list[Encoder]:
        """Parts that actually recorded frames, in order."""
        return [e for e in self.parts if e.frames_written > 0]

    def finalize_parts(self) -> list[VideoFileReport]:
        """Remux killed encoders, probe everything, build the frozen report shape."""
        reports: list[VideoFileReport] = []
        for encoder in self.parts:
            if encoder not in self.usable_parts():
                continue
            path = encoder.path
            error: str | None = None
            duration_ms = webm.probe_duration_ms(path)
            if duration_ms is None and (encoder.killed or encoder.died):
                if webm.remux(path) or webm.remux(path, genpts=True):
                    duration_ms = webm.probe_duration_ms(path)
                else:
                    error = "remux failed; file kept as recorded"
            elif duration_ms is None:
                # Clean exit but no duration: remux anyway, the data is there.
                if webm.remux(path):
                    duration_ms = webm.probe_duration_ms(path)
                if duration_ms is None:
                    error = error or "no duration in container"
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
                error = error or "file missing"
            reports.append(VideoFileReport(
                path=f"videos/{path.name}",
                camId=self.info.cam_id,
                ok=duration_ms is not None and error is None,
                bytesWritten=size,
                durationMs=duration_ms,
                ebmlHeaders=webm.count_ebml_headers(path),
                error=error,
            ))
        return reports


# ---------------------------------------------------------------------------
# session recorder
# ---------------------------------------------------------------------------
class SessionRecorder:
    """Every camera recorder of one session; the stop result builder."""

    def __init__(self, session_id: str, folder: Path,
                 cameras: list[tuple[CameraInfo, CaptureWorker]],
                 bridge: AnchorBridge, codec: dict) -> None:
        self.session_id = session_id
        self.folder = folder
        self.codec = codec
        self.cameras = list(cameras)
        self.recorders: dict[str, CameraRecorder] = {}
        folder.mkdir(parents=True, exist_ok=True)
        for info, worker in cameras:
            self.recorders[info.cam_id] = CameraRecorder(
                session_id, folder, info, worker, bridge, codec)

    def start(self) -> None:
        for recorder in self.recorders.values():
            recorder.start()

    def wait_first_frames(self, timeout: float) -> dict[str, bool]:
        """Poll until every camera saw its first frame or the timeout expires."""
        deadline = time.monotonic() + timeout
        results = {cam_id: False for cam_id in self.recorders}
        while time.monotonic() < deadline:
            pending = [c for c, ok in results.items() if not ok]
            if not pending:
                break
            for cam_id in pending:
                results[cam_id] = self.recorders[cam_id]._started_marked
            if all(results.values()):
                break
            time.sleep(0.05)
        return results

    def stop(self) -> CameraStopResult:
        """Stop every camera in parallel and build the result. Never raises per cam."""
        recorders = list(self.recorders.values())

        def stop_one(recorder: CameraRecorder) -> None:
            try:
                recorder.stop()
            except Exception:
                logger.exception("stopping recorder %s failed", recorder.info.cam_id)

        threads = [threading.Thread(target=stop_one, args=(r,), daemon=True)
                   for r in recorders]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=STOP_ALL_BUDGET_S / max(1, len(threads)) + 20.0)

        recordings: list[CameraRecording] = []
        events: list[CameraEvent] = []
        missed: list[str] = []
        for recorder in recorders:
            try:
                usable = recorder.usable_parts()
                if not usable:
                    events.extend(recorder.events)
                    missed.append(recorder.info.cam_id)
                    continue
                reports = recorder.finalize_parts()
                stopped_ms = recorder.stopped_at_ms
                started_ms = recorder.started_at_ms
                recorder._record_event("stopped", stopped_ms)
                recorder._submit_anchor("stopped", stopped_ms)
                events.extend(recorder.events)
                recordings.append(CameraRecording(
                    cam_id=recorder.info.cam_id,
                    device_key=recorder.info.device_key,
                    label=recorder.info.label,
                    mime=recorder.mime,
                    files=[e.path for e in usable],
                    started_at_ms=started_ms,
                    flash_at_ms=started_ms,
                    stopped_at_ms=stopped_ms,
                    reports=reports,
                ))
            except Exception:
                logger.exception("finalising %s failed", recorder.info.cam_id)
                missed.append(recorder.info.cam_id)

        # The stopped anchors are queued futures on the bridge loop; wait for them so
        # the caller can read <sid>_cameras.json right after stop_recording returns.
        for recorder in recorders:
            recorder.wait_anchors(timeout=5.0)
        return CameraStopResult(session_id=self.session_id, recordings=recordings,
                                events=events, missed=missed)

    def abort(self) -> None:
        for recorder in self.recorders.values():
            try:
                recorder.abort()
            except Exception:
                logger.exception("aborting recorder %s failed", recorder.info.cam_id)

    def dropped_frames(self) -> dict[str, int]:
        """Frames dropped for the recorder subscribers, per camera (benchmark)."""
        return {cam_id: r.sub.dropped for cam_id, r in self.recorders.items()}
