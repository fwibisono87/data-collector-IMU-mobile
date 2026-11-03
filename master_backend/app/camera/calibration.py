r"""Calibration: reference stills, storage, and drift measurement.

Implements ``docs/rebuild/CALIBRATION.md``:

* **Storage** under ``SSD_PATH/Data_Riset_IMU/_calibration/``:
  ``references/<ref_id>/reference.json`` + ``<device_key_slug>.jpg`` (+ precomputed
  ``<device_key_slug>.edges.png``), ``active.json``, ``session_checks.jsonl``.
  Reference sets are written atomically: everything goes into a temp directory that
  is then renamed into place, so a crash can never leave a half set behind.
* **Simultaneous capture**: one trigger timestamp for every live selected camera;
  each camera contributes the *first frame that arrived at or after the trigger*
  (worker subscription, not "latest frame"), so all stills are one instant within
  one frame interval; the skew per camera is recorded.
* **Drift**: edge-based phase correlation with numpy FFT — grayscale, resize to
  320 px width, Gaussian blur σ=1, Sobel gradient magnitude, zero-mean/unit-variance
  normalisation, Hann window, whitened cross-power spectrum, sub-pixel peak, then
  normalised cross-correlation of the aligned gradient images as the similarity
  score. Thresholds are the named constants below; the reported (dx, dy) is the
  camera's view-window displacement relative to the reference in reference pixels
  (the fake camera's crop offset +20 px reads back as dx ≈ +20).

Rotation and zoom are not estimated: they lower the NCC score, which shows up as
CHECK/MOVED. Computation stays well under 30 ms per camera at 320 px width and is
rate-limited to 2 Hz per camera by :class:`DriftMeter`.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

from ..api.schema import CompareCamera, CompareResult, Drift, Reference, ReferenceCamera

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# thresholds (CALIBRATION.md "Drift measurement", percentages of reference width)
# ---------------------------------------------------------------------------
ANALYSIS_WIDTH = 320
OK_SHIFT_PCT = 0.5            # |shift| ≤ 0.5 % W and score ≥ OK_SCORE → OK
CHECK_SHIFT_PCT = 1.5         # |shift| ≤ 1.5 % W (or mid score) → CHECK; above → MOVED
OK_SCORE = 0.6
CHECK_SCORE = 0.4             # score < 0.4 → MOVED (rotated/zoomed/obstructed)
NO_SIGNAL_CONFIDENCE = 0.05   # phase-correlation peak below this → NO_SIGNAL
GAUSS_SIGMA = 1.0
HANN_EPS = 1e-12
ASPECT_TOLERANCE = 0.01       # >1 % aspect difference → RESOLUTION_CHANGED
MIN_GRAD_STD = 1e-6           # flat/blank image → NO_SIGNAL
COMPARE_MIN_INTERVAL_S = 0.5  # ≤ 2 Hz per camera

_SLUG_INVALID = re.compile(r"[^A-Za-z0-9._-]")


def new_ref_id(now: float | None = None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now else time.time()))
    return f"ref_{stamp}_{secrets.token_hex(2)}"


def device_key_slug(device_key: str) -> str:
    """Device key → filename: ``[^A-Za-z0-9._-]`` → ``_``, 80 chars + 8-char hash."""
    sanitized = _SLUG_INVALID.sub("_", device_key)[:80]
    digest = hashlib.sha256(device_key.encode("utf-8")).hexdigest()[:8]
    return f"{sanitized}_{digest}"


# ---------------------------------------------------------------------------
# image pipeline
# ---------------------------------------------------------------------------
def _decode_gray(data: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(data)) as image:
        return np.asarray(image.convert("L"), dtype=np.float64)


def _gaussian_kernel(sigma: float) -> np.ndarray:
    radius = max(1, int(round(3 * sigma)))
    k = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    return k / k.sum()


def _blur(a: np.ndarray, sigma: float = GAUSS_SIGMA) -> np.ndarray:
    k = _gaussian_kernel(sigma)
    radius = len(k) // 2
    p = np.pad(a, ((0, 0), (radius, radius)), mode="edge")
    out = sum(p[:, i:i + a.shape[1]] * k[i] for i in range(len(k)))
    p = np.pad(out, ((radius, radius), (0, 0)), mode="edge")
    return sum(p[i:i + a.shape[0], :] * k[i] for i in range(len(k)))


def _sobel_magnitude(a: np.ndarray) -> np.ndarray:
    gx = ((a[:-2, 2:] + 2 * a[1:-1, 2:] + a[2:, 2:])
          - (a[:-2, :-2] + 2 * a[1:-1, :-2] + a[2:, :-2]))
    gy = ((a[2:, :-2] + 2 * a[2:, 1:-1] + a[2:, 2:])
          - (a[:-2, :-2] + 2 * a[:-2, 1:-1] + a[:-2, 2:]))
    return np.hypot(gx, gy)


def _prepare(a: np.ndarray) -> np.ndarray | None:
    """Gradient image, normalised and Hann-windowed; None when it carries no signal."""
    g = _sobel_magnitude(_blur(a))
    std = g.std()
    if std < MIN_GRAD_STD:
        return None  # blank scene / lens covered
    g = (g - g.mean()) / std
    h, w = g.shape
    return g * (np.hanning(h)[:, None] * np.hanning(w)[None, :])


def _subpixel(cc: np.ndarray, y: int, x: int) -> tuple[float, float]:
    h, w = cc.shape

    def parabolic(minus: float, centre: float, plus: float) -> float:
        den = minus - 2 * centre + plus
        if abs(den) < 1e-12:
            return 0.0
        return float(np.clip(0.5 * (minus - plus) / den, -1.0, 1.0))

    dy = parabolic(cc[(y - 1) % h, x], cc[y, x], cc[(y + 1) % h, x])
    dx = parabolic(cc[y, (x - 1) % w], cc[y, x], cc[y, (x + 1) % w])
    return dx, dy


def measure_shift(ref_gray: np.ndarray, live_gray: np.ndarray) -> dict:
    """Phase correlation between two grayscale images of the same size.

    Returns ``{dx, dy, confidence, score}`` at the input scale, where (dx, dy) is
    the displacement of the live camera's view window relative to the reference:
    ``live(x, y) ≈ ref(x + dx, y + dy)``. ``confidence`` is the whitened
    cross-power peak (≈1 for a perfect match, →0 for uncorrelated or blank input);
    ``score`` is the NCC of the gradient images after alignment.
    """
    a = _prepare(ref_gray)
    b = _prepare(live_gray)
    if a is None or b is None:
        return {"dx": 0.0, "dy": 0.0, "confidence": 0.0, "score": 0.0,
                "no_signal": True}

    h, w = a.shape
    cross = np.fft.rfft2(a) * np.conj(np.fft.rfft2(b))
    magnitude = np.abs(cross)
    magnitude[magnitude < HANN_EPS] = HANN_EPS
    cc = np.fft.irfft2(cross / magnitude, s=a.shape)

    peak_y, peak_x = np.unravel_index(int(np.argmax(cc)), cc.shape)
    confidence = float(cc[peak_y, peak_x])
    shift_y = peak_y - h if peak_y > h // 2 else peak_y
    shift_x = peak_x - w if peak_x > w // 2 else peak_x
    sub_x, sub_y = _subpixel(cc, peak_y, peak_x)
    fx, fy = shift_x + sub_x, shift_y + sub_y

    # NCC of the gradient images after aligning live onto reference.
    idx, idy = int(round(fx)), int(round(fy))
    aligned = np.roll(np.roll(b, idy, axis=0), idx, axis=1)
    margin = min(max(abs(idx), abs(idy)) + 1, min(h, w) // 4)
    sl = (slice(margin, h - margin), slice(margin, w - margin))
    a2, b2 = a[sl], aligned[sl]
    a2 = a2 - a2.mean()
    b2 = b2 - b2.mean()
    denom = float(np.sqrt((a2 * a2).sum() * (b2 * b2).sum()))
    score = float((a2 * b2).sum() / denom) if denom > 1e-9 else 0.0
    return {"dx": fx, "dy": fy, "confidence": confidence, "score": score,
            "no_signal": False}


def verdict_for(shift_pct: float, score: float, confidence: float) -> str:
    """CALIBRATION.md verdict ladder; thresholds are the constants above."""
    if confidence < NO_SIGNAL_CONFIDENCE:
        return "NO_SIGNAL"
    if shift_pct > CHECK_SHIFT_PCT or score < CHECK_SCORE:
        return "MOVED"
    if shift_pct <= OK_SHIFT_PCT and score >= OK_SCORE:
        return "OK"
    return "CHECK"


def measure_drift(ref_bytes: bytes, live_bytes: bytes, at_ms: int) -> Drift:
    """Full drift measurement between a reference still and a live frame."""
    ref_img = Image.open(io.BytesIO(ref_bytes)).convert("L")
    live_img = Image.open(io.BytesIO(live_bytes)).convert("L")
    ref_w, ref_h = ref_img.size
    live_w, live_h = live_img.size
    if ref_w == 0 or ref_h == 0:
        raise ValueError("reference image is empty")

    ref_aspect = ref_w / ref_h
    live_aspect = live_w / live_h if live_h else 0.0
    if abs(ref_aspect - live_aspect) / ref_aspect > ASPECT_TOLERANCE:
        return Drift(verdict="RESOLUTION_CHANGED", dx_px=0.0, dy_px=0.0,
                     shift_pct=0.0, score=0.0, confidence=0.0, at_ms=at_ms)
    if (live_w, live_h) != (ref_w, ref_h):
        live_img = live_img.resize((ref_w, ref_h), Image.BILINEAR)

    analysis_h = max(8, round(ref_h * ANALYSIS_WIDTH / ref_w))
    ref_small = np.asarray(ref_img.resize((ANALYSIS_WIDTH, analysis_h),
                                          Image.BILINEAR), dtype=np.float64)
    live_small = np.asarray(live_img.resize((ANALYSIS_WIDTH, analysis_h),
                                            Image.BILINEAR), dtype=np.float64)

    result = measure_shift(ref_small, live_small)
    if result["no_signal"]:
        return Drift(verdict="NO_SIGNAL", dx_px=0.0, dy_px=0.0, shift_pct=0.0,
                     score=result["score"], confidence=result["confidence"],
                     at_ms=at_ms)

    scale = ref_w / ANALYSIS_WIDTH
    dx_px = result["dx"] * scale
    dy_px = result["dy"] * scale
    shift_pct = float(np.hypot(dx_px, dy_px)) / ref_w * 100.0
    verdict = verdict_for(shift_pct, result["score"], result["confidence"])
    return Drift(verdict=verdict, dx_px=round(dx_px, 2), dy_px=round(dy_px, 2),
                 shift_pct=round(shift_pct, 3), score=round(result["score"], 4),
                 confidence=round(result["confidence"], 4), at_ms=at_ms)


def render_edges_png(ref_bytes: bytes) -> bytes:
    """Thin white edge outline on transparency — the "Edges only" overlay source."""
    gray = _decode_gray(ref_bytes)
    mag = _sobel_magnitude(_blur(gray))
    if mag.max() <= mag.min():
        rgba = np.zeros((*mag.shape, 4), dtype=np.uint8)
    else:
        norm = (mag - mag.min()) / (mag.max() - mag.min())
        threshold = max(float(norm.mean() + norm.std()), 0.2)
        edges = norm > threshold
        rgba = np.zeros((*mag.shape, 4), dtype=np.uint8)
        rgba[edges] = (255, 255, 255, 255)
    out = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(out, format="PNG")
    return out.getvalue()


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
class CalibrationError(RuntimeError):
    """A calibration operation failed; ``code`` matches the API error codes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CalibrationStore:
    """Everything on disk under ``Data_Riset_IMU/_calibration/``."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.references_dir = self.base_dir / "references"
        self.active_path = self.base_dir / "active.json"
        self.session_checks_path = self.base_dir / "session_checks.jsonl"
        self._write_lock = threading.Lock()

    # -- references -----------------------------------------------------------
    def ref_dir(self, ref_id: str) -> Path:
        if not re.fullmatch(r"ref_[0-9]{8}-[0-9]{6}_[0-9a-f]{4}", ref_id or ""):
            raise CalibrationError("not_found", f"invalid reference id {ref_id!r}")
        return self.references_dir / ref_id

    def save_reference(self, ref_id: str, name: str, trigger_ms: int,
                       entries: list[tuple[ReferenceCamera, bytes]]) -> Reference:
        """Atomically create ``references/<ref_id>/`` (temp dir + rename)."""
        with self._write_lock:
            self.references_dir.mkdir(parents=True, exist_ok=True)
            final = self.ref_dir(ref_id)
            if final.exists():
                raise CalibrationError("busy", f"reference {ref_id} already exists")
            tmp = Path(tempfile.mkdtemp(prefix=f".tmp_{ref_id}_",
                                        dir=str(self.references_dir)))
            try:
                cameras = []
                for camera, jpeg_bytes in entries:
                    (tmp / camera.file).write_bytes(jpeg_bytes)
                    try:
                        (tmp / f"{Path(camera.file).stem}.edges.png").write_bytes(
                            render_edges_png(jpeg_bytes))
                    except Exception:
                        logger.exception("edges.png failed for %s", camera.file)
                    cameras.append(camera)
                reference = Reference(
                    ref_id=ref_id, name=name, created_at_ms=trigger_ms,
                    trigger_ms=trigger_ms, cameras=cameras,
                    thumb_urls=self._thumb_urls(ref_id, cameras),
                )
                (tmp / "reference.json").write_text(
                    json.dumps(reference.model_dump(mode="json"), indent=2),
                    encoding="utf-8")
                os.rename(tmp, final)
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
        return reference

    @staticmethod
    def _thumb_urls(ref_id: str,
                    cameras: list[ReferenceCamera]) -> dict[str, str]:
        return {
            camera.device_key:
                f"/api/calibration/references/{ref_id}/{Path(camera.file).name}"
            for camera in cameras
        }

    def list_references(self) -> list[Reference]:
        if not self.references_dir.is_dir():
            return []
        references = []
        for entry in sorted(self.references_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            loaded = self._load(entry.name)
            if loaded is not None:
                references.append(loaded)
        references.sort(key=lambda r: r.created_at_ms, reverse=True)
        return references

    def load(self, ref_id: str) -> Reference:
        reference = self._load(ref_id)
        if reference is None:
            raise CalibrationError("not_found", f"no reference {ref_id!r}")
        return reference

    def _load(self, ref_id: str) -> Reference | None:
        path = self.ref_dir(ref_id) / "reference.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("thumb_urls", None)
            reference = Reference.model_validate(data)
            reference.thumb_urls = self._thumb_urls(reference.ref_id,
                                                    reference.cameras)
            return reference
        except Exception:
            logger.exception("could not read reference %s", ref_id)
            return None

    def rename(self, ref_id: str, name: str) -> Reference:
        with self._write_lock:
            path = self.ref_dir(ref_id) / "reference.json"
            if not path.is_file():
                raise CalibrationError("not_found", f"no reference {ref_id!r}")
            data = json.loads(path.read_text(encoding="utf-8"))
            data["name"] = name
            _atomic_write_text(path, json.dumps(data, indent=2))
        return self.load(ref_id)

    def delete(self, ref_id: str) -> None:
        with self._write_lock:
            directory = self.ref_dir(ref_id)
            if not directory.is_dir():
                raise CalibrationError("not_found", f"no reference {ref_id!r}")
            if self.active_ref_id() == ref_id:
                raise CalibrationError(
                    "bad_state",
                    f"reference {ref_id!r} is active; deactivate it first")
            shutil.rmtree(directory, ignore_errors=True)

    # -- active reference -----------------------------------------------------
    def active_ref_id(self) -> str | None:
        try:
            data = json.loads(self.active_path.read_text(encoding="utf-8"))
            ref_id = data.get("ref_id")
            return ref_id if isinstance(ref_id, str) and ref_id else None
        except (OSError, json.JSONDecodeError):
            return None

    def activate(self, ref_id: str) -> None:
        self.load(ref_id)  # not_found when it does not exist
        with self._write_lock:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(self.active_path, json.dumps(
                {"ref_id": ref_id, "activated_at_ms": int(time.time() * 1000)},
                indent=2))

    def deactivate(self) -> None:
        with self._write_lock:
            try:
                os.unlink(self.active_path)
            except FileNotFoundError:
                pass

    # -- images ----------------------------------------------------------------
    def image_path(self, ref_id: str, key_slug: str) -> Path:
        return self.ref_dir(ref_id) / f"{key_slug}.jpg"

    def edges_path(self, ref_id: str, key_slug: str) -> Path:
        return self.ref_dir(ref_id) / f"{key_slug}.edges.png"

    # -- session checks ----------------------------------------------------------
    def append_session_check(self, record: dict) -> None:
        with self._write_lock:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            with open(self.session_checks_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
                fh.flush()

    def session_checks(self, session_id: str) -> list[dict]:
        if not self.session_checks_path.is_file():
            return []
        checks = []
        for line in self.session_checks_path.read_text(
                encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("session_id") == session_id:
                checks.append(record)
        return checks


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# simultaneous capture (CALIBRATION.md "Capturing")
# ---------------------------------------------------------------------------
CAPTURE_FRAME_TIMEOUT_S = 0.5


def capture_simultaneous(workers: dict, timeout: float = CAPTURE_FRAME_TIMEOUT_S):
    """One trigger instant, one still per camera.

    Every worker is subscribed *before* the trigger timestamp is taken, then each
    camera contributes its first frame whose arrival time is at or after the
    trigger (waiting up to ``timeout``). That bounds the skew to about one frame
    interval instead of "latest frame", which can be a frame old on one camera and
    fresh on another.

    Returns ``(trigger_ms, frames, failed)``: ``frames`` maps cam_id → Frame,
    ``failed`` lists the cam_ids that produced nothing in time. Duck-typed on the
    worker's subscribe/unsubscribe API so this module stays import-light.
    """
    subs = {cam_id: worker.subscribe(64, drop_oldest=True)
            for cam_id, worker in workers.items()}
    trigger_ns = time.time_ns()
    trigger_ms = trigger_ns // 1_000_000
    frames: dict = {}
    try:
        def collect(cam_id: str, sub) -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                frame = sub.get(timeout=0.02)
                if frame is not None and frame.arrival_ns >= trigger_ns:
                    frames[cam_id] = frame
                    return

        threads = [threading.Thread(target=collect, args=(cam_id, sub), daemon=True)
                   for cam_id, sub in subs.items()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=timeout + 1.0)
    finally:
        for cam_id, sub in subs.items():
            workers[cam_id].unsubscribe(sub)
    failed = sorted(set(workers) - set(frames))
    return trigger_ms, frames, failed


# ---------------------------------------------------------------------------
# rate-limited drift meter
# ---------------------------------------------------------------------------
class DriftMeter:
    """Computes compare results at ≤ 2 Hz per camera with a small cache."""

    def __init__(self, store: CalibrationStore) -> None:
        self.store = store
        self._cache: dict[tuple[str, str], tuple[float, CompareCamera]] = {}
        self._lock = threading.Lock()

    def compare(self, ref_id: str, live_frames, cam_ids: dict[str, str],
                labels: dict[str, str] | None = None) -> CompareResult:
        """Compare every camera of the reference against its live frame.

        ``live_frames`` maps device_key → ``(jpeg_bytes, cam_id)`` for the currently
        live selected cameras; ``cam_ids`` maps device_key → cam_id for the selected
        slots (used to report MISSING with the slot the camera had).
        """
        reference = self.store.load(ref_id)
        now_ms = int(time.time() * 1000)
        cameras: list[CompareCamera] = []
        for entry in reference.cameras:
            cached = self._cached(ref_id, entry.device_key)
            if cached is not None:
                cameras.append(cached)
                continue
            live = live_frames.get(entry.device_key)
            if live is None:
                cam_id = cam_ids.get(entry.device_key, entry.cam_id_at_capture)
                item = CompareCamera(
                    verdict="MISSING", dx_px=0.0, dy_px=0.0, shift_pct=0.0,
                    score=0.0, confidence=0.0, at_ms=now_ms,
                    device_key=entry.device_key, cam_id=cam_id)
            else:
                frame_bytes, cam_id = live
                try:
                    ref_bytes = (self.store.ref_dir(ref_id)
                                 / Path(entry.file).name).read_bytes()
                    drift = measure_drift(ref_bytes, frame_bytes, now_ms)
                except (OSError, ValueError) as exc:
                    logger.warning("drift measurement failed for %s: %s",
                                   entry.device_key, exc)
                    drift = Drift(verdict="NO_SIGNAL", dx_px=0.0, dy_px=0.0,
                                  shift_pct=0.0, score=0.0, confidence=0.0,
                                  at_ms=now_ms)
                item = CompareCamera(**drift.model_dump(),
                                     device_key=entry.device_key, cam_id=cam_id)
            self._store_cache(ref_id, entry.device_key, item)
            cameras.append(item)
        return CompareResult(ref_id=ref_id, at_ms=now_ms, cameras=cameras)

    def _cached(self, ref_id: str, device_key: str) -> CompareCamera | None:
        with self._lock:
            hit = self._cache.get((ref_id, device_key))
        if hit and time.monotonic() - hit[0] < COMPARE_MIN_INTERVAL_S:
            return hit[1]
        return None

    def _store_cache(self, ref_id: str, device_key: str,
                     item: CompareCamera) -> None:
        with self._lock:
            self._cache[(ref_id, device_key)] = (time.monotonic(), item)

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()
