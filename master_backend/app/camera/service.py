"""The real camera service: :func:`create_service` → :class:`RealCameraService`.

Implements the lane-L2 seam (``interface.py`` ``CameraService``) on top of
:mod:`discovery`, :mod:`capture`, :mod:`recorder` and :mod:`calibration`:

* the selection is persisted in ``SSD_PATH/Data_Riset_IMU/_state/cameras.json`` as
  ``[{cam_id, device_key}]`` and reopened on startup for devices that are present;
* ``cameras()`` never blocks: it reads cached worker state and the drift cache,
  which a background thread refreshes every 10 s while idle when an active
  calibration reference exists;
* recording start/stop delegate to :class:`SessionRecorder` and append the
  START/STOP drift checks to ``_calibration/session_checks.jsonl``;
* "camera moved during the session" is *not* reported through the stop result's
  events (that would misuse the frozen event types): the message is exposed via
  :meth:`RealCameraService.last_session_drift_change` for L4 to poll (seam
  addition, see the lane report).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import threading
import time
from pathlib import Path

from PIL import Image

from ..api.schema import (
    Camera,
    CameraDevice,
    CameraSelectionItem,
    CompareResult,
    Drift,
    Issue,
    Reference,
    ReferenceCamera,
)
from . import discovery
from .calibration import (
    CalibrationError,
    CalibrationStore,
    DriftMeter,
    device_key_slug,
    new_ref_id,
)
from .capture import CaptureWorker, Frame
from .interface import (
    CameraError,
    CameraStopResult,
)
from .recorder import AnchorBridge, CameraInfo, SessionRecorder, codec_config

logger = logging.getLogger(__name__)

MAX_CAMERAS = 5
DRIFT_REFRESH_S = 10.0
FIRST_FRAME_WAIT_S = 1.5
_DRIFT_WARNING_VERDICTS = {"CHECK", "MOVED", "RESOLUTION_CHANGED", "MISSING"}


class CodedCameraError(CameraError):
    """A CameraError that carries an API.md error code (``bad_state``, ``busy``, …)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _ssd_root() -> Path:
    return Path(os.getenv("SSD_PATH", "./data"))


class RealCameraService:
    """The production :class:`~master_backend.app.camera.interface.CameraService`."""

    def __init__(self, *, ssd_root: Path | None = None) -> None:
        self._ssd_root_override = Path(ssd_root) if ssd_root else None
        root = self._ssd_root
        self.store = CalibrationStore(root / "Data_Riset_IMU" / "_calibration")
        self.meter = DriftMeter(self.store)
        self.bridge = AnchorBridge()

        self._lock = threading.RLock()
        self._workers: dict[str, CaptureWorker] = {}          # cam_id -> worker
        self._selection: list[tuple[str, str]] = []           # (device_key, cam_id)
        self._recorder: SessionRecorder | None = None
        self._closed = False
        self._capture_lock = threading.Lock()

        self._drift_cache: dict[str, Drift] = {}              # device_key -> drift
        self._drift_ref_name: str | None = None
        self._drift_thread: threading.Thread | None = None
        self._drift_stop = threading.Event()

        self._session_checks: dict[str, CompareResult] = {}   # session -> start check
        self._session_drift: dict[str, str | None] = {}

        self._restore_selection()
        self._start_drift_thread()

    # -- paths -----------------------------------------------------------------
    @property
    def _ssd_root(self) -> Path:
        return self._ssd_root_override or _ssd_root()

    @property
    def state_dir(self) -> Path:
        return self._ssd_root / "Data_Riset_IMU" / "_state"

    @property
    def selection_path(self) -> Path:
        return self.state_dir / "cameras.json"

    # -- CameraService: discovery & state ---------------------------------------
    def list_devices(self) -> list[CameraDevice]:
        try:
            return discovery.list_devices()
        except Exception:
            logger.exception("device listing failed")
            return []

    def cameras(self) -> list[Camera]:
        recording = self.is_recording()
        out: list[Camera] = []
        with self._lock:
            selection = list(self._selection)
            workers = dict(self._workers)
        for device_key, cam_id in selection:
            worker = workers.get(cam_id)
            if worker is None:
                out.append(Camera(
                    cam_id=cam_id, device_key=device_key,
                    label=self._label_for(device_key), state="opening",
                    recording=recording, width=None, height=None, fps=None,
                    dropped_frames=0, error=None,
                    drift=self._drift_cache.get(device_key)))
                continue
            snap = worker.snapshot()
            out.append(Camera(
                cam_id=cam_id, device_key=device_key,
                label=worker.device.label, state=snap["state"],
                recording=recording, width=snap["width"], height=snap["height"],
                fps=snap["fps"], dropped_frames=snap["dropped_frames"],
                error=snap["error"], drift=self._drift_cache.get(device_key)))
        return out

    def _label_for(self, device_key: str) -> str:
        for device in self.list_devices():
            if device.device_key == device_key:
                return device.label
        return device_key

    def readiness(self) -> tuple[list[Issue], list[Issue]]:
        blockers: list[Issue] = []
        cameras = self.cameras()
        if not cameras:
            blockers.append(Issue(
                code="no_camera_selected",
                message="Select at least one camera before starting.",
            ))
        for camera in cameras:
            if camera.state != "live":
                blockers.append(Issue(
                    code="camera_not_live",
                    message=f"{camera.cam_id} ({camera.label}) is not live.",
                    subject=camera.cam_id,
                ))
        warnings: list[Issue] = []
        ref_name = self._drift_ref_name
        for camera in cameras:
            drift = camera.drift
            if drift is None or drift.verdict not in _DRIFT_WARNING_VERDICTS:
                continue
            suffix = f" since reference '{ref_name}'" if ref_name else ""
            if drift.verdict == "MISSING":
                message = f"{camera.cam_id} is not connected{suffix}"
            elif drift.verdict == "RESOLUTION_CHANGED":
                message = (f"{camera.cam_id} changed resolution or aspect ratio"
                           f"{suffix}")
            else:
                shift = round(math.hypot(drift.dx_px, drift.dy_px))
                message = (f"{camera.cam_id} moved {shift} px{suffix} "
                           f"({drift.verdict})")
            warnings.append(Issue(code="camera_drift", message=message,
                                  subject=camera.cam_id))
        return blockers, warnings

    def is_recording(self) -> bool:
        with self._lock:
            return self._recorder is not None

    # -- selection ---------------------------------------------------------------
    def set_selection(self, items: list[CameraSelectionItem]) -> list[Camera]:
        with self._lock:
            if self._recorder is not None:
                raise CodedCameraError(
                    "bad_state", "cannot change the camera selection while recording")
        if len(items) > MAX_CAMERAS:
            raise CodedCameraError("invalid", f"at most {MAX_CAMERAS} cameras")
        known = {device.device_key: device for device in self.list_devices()}
        used: set[str] = set()
        selection: list[tuple[str, str]] = []
        for item in items:
            if item.device_key not in known:
                raise CodedCameraError(
                    "not_found", f"unknown camera device {item.device_key!r}")
            if item.cam_id is not None:
                cam_id = item.cam_id
                if not (cam_id.startswith("cam") and cam_id[3:].isdigit()
                        and 1 <= int(cam_id[3:]) <= MAX_CAMERAS):
                    raise CodedCameraError("invalid", f"invalid slot {cam_id!r}")
            else:
                index = 1
                while f"cam{index}" in used:
                    index += 1
                cam_id = f"cam{index}"
            if cam_id in used:
                raise CodedCameraError("invalid", f"duplicate camera slot {cam_id!r}")
            used.add(cam_id)
            selection.append((item.device_key, cam_id))

        self._apply_selection(selection, known)
        self._persist_selection(selection)
        return self.cameras()

    def _apply_selection(self, selection: list[tuple[str, str]],
                         known: dict[str, CameraDevice]) -> None:
        with self._lock:
            old = {cam_id: worker for cam_id, worker in self._workers.items()}
            wanted = {cam_id: device_key for device_key, cam_id in selection}
            # Stop workers whose slot disappeared or moved to another device.
            removed = {cam_id: worker for cam_id, worker in old.items()
                       if wanted.get(cam_id) is None
                       or worker.device.device_key != wanted[cam_id]}
            for cam_id in removed:
                self._workers.pop(cam_id, None)
            for cam_id, worker in removed.items():
                threading.Thread(target=worker.stop, daemon=True,
                                 name=f"stop-{cam_id}").start()
            for device_key, cam_id in selection:
                existing = old.get(cam_id)
                if (existing is not None and cam_id not in removed
                        and existing.device.device_key == device_key):
                    self._workers[cam_id] = existing
                    continue
                worker = CaptureWorker(cam_id, known[device_key])
                worker.start()
                self._workers[cam_id] = worker
            self._selection = list(selection)
        self.meter.invalidate()

    def _persist_selection(self, selection: list[tuple[str, str]]) -> None:
        payload = [{"cam_id": cam_id, "device_key": device_key}
                   for device_key, cam_id in selection]
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.selection_path.with_name(self.selection_path.name + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.selection_path)
        except OSError:
            logger.exception("could not persist the camera selection")

    def _restore_selection(self) -> None:
        try:
            raw = json.loads(self.selection_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, list):
            return
        items: list[CameraSelectionItem] = []
        for entry in raw[:MAX_CAMERAS]:
            if isinstance(entry, dict) and entry.get("device_key"):
                items.append(CameraSelectionItem(
                    device_key=str(entry["device_key"]),
                    cam_id=str(entry["cam_id"]) if entry.get("cam_id") else None))
        if not items:
            return
        try:
            known = {device.device_key: device for device in self.list_devices()}
            present = [item for item in items if item.device_key in known]
            if present:
                self._apply_selection(
                    [(item.device_key, item.cam_id or f"cam{i + 1}")
                     for i, item in enumerate(present)],
                    known)
                logger.info("restored camera selection: %s",
                            [item.device_key for item in present])
        except Exception:
            logger.exception("could not restore the camera selection")

    # -- recording -----------------------------------------------------------------
    async def start_recording(self, session_id: str, folder: Path) -> None:
        with self._lock:
            if self._closed:
                raise CameraError("camera service is closed")
            if self._recorder is not None:
                raise CameraError("already recording")
            cameras = self.cameras()
            live = [(camera, self._workers[camera.cam_id])
                    for camera in cameras
                    if camera.state == "live" and camera.cam_id in self._workers]
        if not live:
            raise CameraError("no selected camera is live")

        self._log_session_check(session_id, "start")

        codec = codec_config()
        pairs = [(CameraInfo(camera.cam_id, camera.device_key, camera.label), worker)
                 for camera, worker in live]
        recorder = SessionRecorder(session_id, Path(folder), pairs, self.bridge,
                                   codec)
        with self._lock:
            self._recorder = recorder
        try:
            recorder.start()
            await asyncio.to_thread(recorder.wait_first_frames, FIRST_FRAME_WAIT_S)
        except BaseException:
            with self._lock:
                self._recorder = None
            recorder.abort()
            raise

    async def stop_recording(self, session_id: str) -> CameraStopResult:
        with self._lock:
            recorder = self._recorder
        if recorder is None:
            raise CameraError("not recording")
        if recorder.session_id != session_id:
            raise CameraError(
                f"not recording {session_id} (recording {recorder.session_id})")

        # STOP drift check happens just before the encoders stop (CALIBRATION.md).
        stop_check = self._log_session_check(session_id, "stop")
        result = await asyncio.to_thread(recorder.stop)
        with self._lock:
            self._recorder = None
        start_check = self._session_checks.pop(session_id, None)
        self._session_drift[session_id] = self._drift_change(start_check, stop_check)
        return result

    def _log_session_check(self, session_id: str, moment: str) -> CompareResult | None:
        ref_id = self.store.active_ref_id()
        if ref_id is None:
            return None
        try:
            result = self._compare_sync(ref_id)
        except CalibrationError:
            return None
        except Exception:
            logger.exception("session drift check failed")
            return None
        record = {
            "session_id": session_id,
            "moment": moment,
            "at_ms": result.at_ms,
            "ref_id": ref_id,
            "cameras": [item.model_dump(mode="json") for item in result.cameras],
        }
        try:
            self.store.append_session_check(record)
        except OSError:
            logger.exception("could not append to session_checks.jsonl")
        if moment == "start":
            self._session_checks[session_id] = result
        return result

    def _drift_change(self, start: CompareResult | None,
                      stop: CompareResult | None) -> str | None:
        """CALIBRATION.md: verdict change or > 0.5 % W shift between START and STOP."""
        if start is None or stop is None:
            return None
        ref_widths: dict[str, int] = {}
        try:
            reference = self.store.load(stop.ref_id)
            ref_widths = {c.device_key: c.width for c in reference.cameras}
        except CalibrationError:
            pass
        start_by_key = {c.device_key: c for c in start.cameras}
        messages: list[str] = []
        for item in stop.cameras:
            before = start_by_key.get(item.device_key)
            if before is None:
                continue
            delta_px = math.hypot(item.dx_px - before.dx_px, item.dy_px - before.dy_px)
            width = ref_widths.get(item.device_key) or 0
            moved = (item.verdict != before.verdict
                     or (width > 0 and delta_px / width * 100.0 > 0.5))
            if moved:
                messages.append(
                    f"{item.cam_id} appears to have moved during the session "
                    f"(Δ {round(delta_px)} px)")
        return "; ".join(messages) if messages else None

    def last_session_drift_change(self, session_id: str) -> str | None:
        """Seam addition: the "moved during the session" message for L4 to poll."""
        return self._session_drift.get(session_id)

    # -- calibration -------------------------------------------------------------
    async def compare(self, ref_id: str) -> CompareResult:
        """Compare live cameras against ``ref_id`` (``"active"`` allowed)."""
        return await asyncio.to_thread(self._compare_sync, ref_id)

    def _compare_sync(self, ref_id: str) -> CompareResult:
        if ref_id == "active":
            active = self.store.active_ref_id()
            if active is None:
                raise CalibrationError("not_found", "no active calibration reference")
            ref_id = active
        live_frames, cam_ids = self._live_frames_by_device()
        result = self.meter.compare(ref_id, live_frames, cam_ids)
        self._absorb_drift(result)
        return result

    def _live_frames_by_device(self) -> tuple[dict, dict[str, str]]:
        live_frames: dict[str, tuple[bytes, str]] = {}
        cam_ids: dict[str, str] = {}
        with self._lock:
            workers = dict(self._workers)
        for cam_id, worker in workers.items():
            cam_ids[worker.device.device_key] = cam_id
            latest: Frame | None = worker.latest
            if latest is not None and worker.state == "live":
                live_frames[worker.device.device_key] = (latest.data, cam_id)
        return live_frames, cam_ids

    def _absorb_drift(self, result: CompareResult) -> None:
        self._drift_cache = {
            item.device_key: Drift(**item.model_dump(
                exclude={"device_key", "cam_id"}))
            for item in result.cameras
        }
        try:
            self._drift_ref_name = self.store.load(result.ref_id).name
        except CalibrationError:
            self._drift_ref_name = None

    async def capture_reference(self, name: str | None = None) -> Reference:
        """POST /api/calibration/references: one trigger, every live camera."""
        if not self._capture_lock.acquire(blocking=False):
            raise CalibrationError("busy", "a reference capture is already running")
        try:
            return await asyncio.to_thread(self._capture_reference_sync, name)
        finally:
            self._capture_lock.release()

    def _capture_reference_sync(self, name: str | None) -> Reference:
        from .calibration import capture_simultaneous  # local: keeps imports flat

        with self._lock:
            workers = {cam_id: worker for cam_id, worker in self._workers.items()
                       if worker.state == "live"}
        if not workers:
            raise CalibrationError("bad_state", "no live camera to capture")
        trigger_ms, frames, failed = capture_simultaneous(workers)
        entries = []
        for cam_id, frame in frames.items():
            worker = workers[cam_id]
            slug = device_key_slug(worker.device.device_key)
            width, height = frame.width, frame.height
            if width is None or height is None:
                with Image.open(io.BytesIO(frame.data)) as image:
                    width, height = image.size
            entries.append((
                ReferenceCamera(
                    device_key=worker.device.device_key,
                    label=worker.device.label,
                    cam_id_at_capture=cam_id,
                    file=f"{slug}.jpg",
                    width=int(width), height=int(height),
                    frame_arrival_ms=frame.arrival_ms,
                    skew_ms=frame.arrival_ms - trigger_ms,
                ),
                frame.data,
            ))
        if not entries:
            raise CalibrationError("busy", "no camera produced a frame in time")
        ref_id = new_ref_id()
        generated = name or time.strftime("Reference %Y-%m-%d %H:%M:%S")
        reference = self.store.save_reference(ref_id, generated, trigger_ms, entries)
        if failed:
            logger.warning("reference %s missing cameras: %s", ref_id, failed)
        self.meter.invalidate()
        return reference

    def list_references(self) -> tuple[list[Reference], str | None]:
        return self.store.list_references(), self.store.active_ref_id()

    def load_reference(self, ref_id: str) -> Reference:
        return self.store.load(ref_id)

    def rename_reference(self, ref_id: str, name: str) -> Reference:
        reference = self.store.rename(ref_id, name)
        self.meter.invalidate()
        return reference

    def activate_reference(self, ref_id: str) -> None:
        self.store.activate(ref_id)
        self.meter.invalidate()
        self._refresh_drift_now()

    def deactivate_reference(self) -> None:
        self.store.deactivate()
        self._drift_cache = {}
        self._drift_ref_name = None

    def delete_reference(self, ref_id: str) -> None:
        self.store.delete(ref_id)
        self.meter.invalidate()

    # -- drift background refresh ---------------------------------------------------
    def _start_drift_thread(self) -> None:
        if self._drift_thread is not None:
            return
        self._drift_stop.clear()
        self._drift_thread = threading.Thread(target=self._drift_loop,
                                              name="camera-drift", daemon=True)
        self._drift_thread.start()

    def _drift_loop(self) -> None:
        while not self._drift_stop.wait(DRIFT_REFRESH_S):
            if self.is_recording():
                continue
            if self.store.active_ref_id() is None:
                continue
            self._refresh_drift_now()

    def _refresh_drift_now(self) -> None:
        try:
            ref_id = self.store.active_ref_id()
            if ref_id is None:
                return
            self._compare_sync(ref_id)
        except CalibrationError:
            pass
        except Exception:
            logger.exception("drift refresh failed")

    # -- fake-camera test hook ------------------------------------------------------
    def set_fake_offset(self, cam_id: str, dx: int, dy: int) -> None:
        """Move a fake camera's crop window (calibration tests, acceptance C2)."""
        with self._lock:
            worker = self._workers.get(cam_id)
        if worker is None:
            raise CodedCameraError("not_found", f"{cam_id!r} is not a selected camera")
        worker.set_offset(dx, dy)

    def wait_live(self, cam_ids: list[str] | None = None, timeout: float = 5.0) -> bool:
        """Block until the given (default: all selected) workers are live.

        A worker mid-restart (``set_offset``) still reports the stale ``live``
        state of its previous process for up to 200 ms, so ``restart_pending``
        is checked too — wait_live must only return once the *new* process is
        delivering frames.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                workers = dict(self._workers)
            targets = ([workers[c] for c in cam_ids if c in workers]
                       if cam_ids else list(workers.values()))
            if targets and all(w.state == "live" and not w.restart_pending
                               for w in targets):
                return True
            time.sleep(0.05)
        return False

    # -- routes helpers (preview / latest frame) ---------------------------------------
    def worker(self, cam_id: str) -> CaptureWorker | None:
        with self._lock:
            return self._workers.get(cam_id)

    def latest_frame(self, cam_id: str) -> Frame | None:
        worker = self.worker(cam_id)
        return worker.latest if worker else None

    # -- shutdown --------------------------------------------------------------
    async def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            recorder, self._recorder = self._recorder, None
            workers = list(self._workers.values())
            self._workers.clear()
            self._selection = []
        if recorder is not None:
            recorder.abort()  # files stay on disk; no anchors are fabricated
        self._drift_stop.set()
        thread, self._drift_thread = self._drift_thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        await asyncio.to_thread(self._stop_all, workers)
        self.bridge.close()

    @staticmethod
    def _stop_all(workers: list[CaptureWorker]) -> None:
        threads = [threading.Thread(target=w.stop, daemon=True) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)


def create_service() -> RealCameraService:
    """Build the real camera service (called by the app wiring in lane L4)."""
    return RealCameraService()
