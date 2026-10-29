"""Unit tests for the drift measurement (spec §7 / CALIBRATION.md).

Sign convention: ``live(x, y) ≈ ref(x + dx, y + dy)``, so a live frame built
as ``np.roll(ref, (-dy, -dx), axis=(0, 1))`` must measure ``(dx, dy)``.
Tolerance is ±1 px in full-resolution pixels (analysis happens at 320 width,
so sub-pixel estimation carries the last few pixels).
"""
from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image
from scipy.ndimage import gaussian_filter

from master_backend.app.camera import calibration

WIDTH, HEIGHT = 1280, 720


@pytest.fixture(scope="module")
def ref() -> np.ndarray:
    """A textured 720p image: smoothed noise, edges everywhere, no period that
    could alias at the tested shifts."""
    rng = np.random.default_rng(7)
    array = rng.integers(0, 256, (HEIGHT, WIDTH, 3)).astype(np.float32)
    array = gaussian_filter(array, sigma=(2.0, 2.0, 0.0))
    return np.clip(array, 0, 255).astype(np.uint8)


@pytest.fixture(scope="module")
def ref_soft() -> np.ndarray:
    """Lower-frequency variant for the rotation test.

    A 3° rotation displaces content proportionally to distance from the
    centre; on 2 px-correlation-length noise nothing survives that, the
    phase-correlation peak collapses and the verdict becomes NO_SIGNAL. On
    larger blobs the (windowed) centre still correlates, and the verdict is
    driven by the score ladder as the spec intends.
    """
    rng = np.random.default_rng(9)
    array = rng.integers(0, 256, (HEIGHT, WIDTH, 3)).astype(np.float32)
    array = gaussian_filter(array, sigma=(8.0, 8.0, 0.0))
    return np.clip(array, 0, 255).astype(np.uint8)


def _jpeg(array: np.ndarray, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(array).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _drift(ref: np.ndarray, live: np.ndarray):
    return calibration.measure_drift(_jpeg(ref), _jpeg(live), 0)


def _shifted(ref: np.ndarray, dx: int, dy: int) -> np.ndarray:
    return np.roll(ref, (-dy, -dx), axis=(0, 1))


@pytest.mark.parametrize("dx,dy", [(0, 0), (5, -3), (20, 0), (-40, 25)])
def test_known_shifts_within_one_pixel(ref, dx, dy):
    result = _drift(ref, _shifted(ref, dx, dy))
    assert abs(result.dx_px - dx) <= 1.0, (result.dx_px, dx)
    assert abs(result.dy_px - dy) <= 1.0, (result.dy_px, dy)
    if (dx, dy) == (0, 0):
        assert result.verdict == "OK"
    elif abs(dx) / WIDTH * 100 <= calibration.OK_SHIFT_PCT:
        assert result.verdict == "OK"
    else:
        assert result.verdict in ("CHECK", "MOVED")


def test_20px_shift_is_moved(ref):
    # 20 px at 1280 width = 1.5625 % > CHECK_SHIFT_PCT → MOVED (spec V2).
    result = _drift(ref, _shifted(ref, 20, 0))
    assert result.verdict == "MOVED"
    assert result.shift_pct == pytest.approx(20 / WIDTH * 100, abs=0.3)


@pytest.mark.parametrize("factor", [1.3, 0.7])
def test_brightness_change_stays_ok(ref, factor):
    live = np.clip(ref.astype(np.float32) * factor, 0, 255).astype(np.uint8)
    result = _drift(ref, live)
    assert result.verdict == "OK"
    assert abs(result.dx_px) <= 1.0 and abs(result.dy_px) <= 1.0


def test_contrast_change_stays_ok(ref):
    live = np.clip(128.0 + (ref.astype(np.float32) - 128.0) * 1.15,
                   0, 255).astype(np.uint8)
    result = _drift(ref, live)
    assert result.verdict == "OK"
    assert abs(result.dx_px) <= 1.0 and abs(result.dy_px) <= 1.0


def test_three_degree_rotation_is_check_or_moved(ref_soft):
    rotated = np.array(Image.fromarray(ref_soft).rotate(3, resample=Image.BICUBIC))
    result = _drift(ref_soft, rotated)
    assert result.verdict in ("CHECK", "MOVED")


def test_black_frame_is_no_signal(ref):
    black = np.zeros_like(ref)
    result = _drift(ref, black)
    assert result.verdict == "NO_SIGNAL"


def test_verdict_for_thresholds():
    # shift_pct / score / confidence → verdict (CALIBRATION.md table).
    assert calibration.verdict_for(0.1, 0.9, 0.5) == "OK"
    assert calibration.verdict_for(1.0, 0.9, 0.5) == "CHECK"
    assert calibration.verdict_for(3.0, 0.9, 0.5) == "MOVED"
    assert calibration.verdict_for(0.1, 0.2, 0.5) == "MOVED"
    assert calibration.verdict_for(0.1, 0.9, 0.01) == "NO_SIGNAL"
