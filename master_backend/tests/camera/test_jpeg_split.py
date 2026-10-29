"""Unit tests for the incremental JPEG splitter (spec §7).

Covers: concatenated frames, an EXIF thumbnail (embedded SOI/EOI inside APP1),
frames split across arbitrary read boundaries (down to one byte per feed), and
garbage between frames.
"""
from __future__ import annotations

import io
import struct

import numpy as np
import pytest
from PIL import Image

from master_backend.app.camera.jpeg import JpegFrameSplitter, sof_size


def _jpeg(width: int = 64, height: int = 48, seed: int = 0) -> bytes:
    rng = np.random.default_rng(seed)
    array = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(array).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _with_exif_thumbnail(data: bytes) -> bytes:
    """Insert an APP1 EXIF segment whose payload embeds a whole tiny JPEG.

    A naive splitter that scans for SOI/EOI without parsing segment lengths
    would cut the frame at the thumbnail's EOI.
    """
    thumb = b"\xff\xd8" + b"\xff\x01" + b"\x00" * 16 + b"\xff\xd9"
    payload = b"Exif\x00\x00" + b"MM\x00\x2a" + b"\x00" * 64 + thumb
    app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    assert data[:2] == b"\xff\xd8"
    return data[:2] + app1 + data[2:]


def _feed_all(splitter: JpegFrameSplitter, data: bytes, chunk: int):
    frames = []
    for i in range(0, len(data), chunk):
        frames.extend(splitter.feed(data[i:i + chunk]))
    return frames


@pytest.mark.parametrize("chunk", [1, 3, 7, 64, 4096, 10**9])
def test_concatenated_frames_any_boundary(chunk):
    originals = [_jpeg(seed=i) for i in range(3)]
    stream = b"".join(originals)
    frames = _feed_all(JpegFrameSplitter(), stream, chunk)
    assert [f.data for f in frames] == originals
    for frame in frames:
        assert (frame.width, frame.height) == (64, 48)


def test_exif_thumbnail_is_not_a_split_point():
    original = _with_exif_thumbnail(_jpeg(seed=11))
    # The thumbnail's EOI inside APP1 must not terminate the frame early.
    assert original.count(b"\xff\xd9") >= 2
    frames = _feed_all(JpegFrameSplitter(), original, 5)
    assert len(frames) == 1
    assert frames[0].data == original
    with Image.open(io.BytesIO(frames[0].data)) as image:
        assert image.size == (64, 48)


def test_garbage_between_frames_is_skipped_and_counted():
    a, b = _jpeg(seed=1), _jpeg(seed=2)
    junk1 = b"garbage-not-a-jpeg\x00\x01\x02"
    junk2 = b"\x00\xffnot a frame\xff\xd9 more junk"
    stream = junk1 + a + junk2 + b
    splitter = JpegFrameSplitter()
    frames = _feed_all(splitter, stream, 9)
    assert [f.data for f in frames] == [a, b]
    assert splitter.bytes_dropped >= len(junk1) + len(junk2)


def test_partial_tail_is_buffered_until_complete():
    a, b = _jpeg(seed=3), _jpeg(seed=4)
    splitter = JpegFrameSplitter()
    frames = splitter.feed(a + b[:50])
    assert [f.data for f in frames] == [a]
    frames = splitter.feed(b[50:])
    assert [f.data for f in frames] == [b]


def test_sof_size_reads_geometry():
    data = _jpeg(width=320, height=180, seed=5)
    assert sof_size(data) == (320, 180)
    assert sof_size(b"\xff\xd8\xff\xd9") == (None, None)
