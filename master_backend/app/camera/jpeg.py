"""Splitting an MJPEG byte stream into complete JPEG frames.

The capture worker pipes ``-f mjpeg`` output from ffmpeg; a frame cannot be found by
"split on ``FFD9``" because an EXIF (APP1) segment may embed a thumbnail that itself
contains ``FFD8…FFD9``. The only correct way is to walk the segment structure:

* find SOI (``FFD8``) — bytes before it are garbage and are dropped;
* walk marker segments, each ``FFxx`` followed by a 2-byte big-endian length that
  includes itself, up to SOS (``FFDA``);
* after SOS the entropy-coded data follows: scan for ``FF`` not followed by ``00``
  (a stuffed byte) or ``D0..D7`` (restart markers). ``FFD9`` there is the real EOI.

While walking, SOF0/SOF2 (and the other SOFn variants) give width and height.

The splitter is incremental: ``feed()`` accepts arbitrary chunk boundaries and returns
the frames completed by that chunk, each as ``(data, width, height)``.
"""
from __future__ import annotations

from typing import NamedTuple

_SOI = 0xD8
_EOI = 0xD9
_SOS = 0xDA
_TEM = 0x01
#: markers with no payload
_STANDALONE = frozenset(range(0xD0, 0xD8)) | {_TEM}
#: SOFn markers: C0-CF except C4 (DHT), C8 (JPG), CC (DAC)
_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

#: Safety valve: a stream of garbage must not grow the buffer forever. A 720p MJPEG
#: frame is well under this; a buffer beyond it means we lost the stream.
MAX_BUFFER_BYTES = 64 * 1024 * 1024


class JpegFrame(NamedTuple):
    """One complete JPEG frame as it arrived from the capture process."""

    data: bytes
    width: int | None
    height: int | None


class JpegFrameSplitter:
    """Incremental parser: feed bytes, get complete :class:`JpegFrame`\\ s out."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._in_frame = False
        self._pos = 0            # parse position inside the current frame
        self._entropy = False    # inside entropy-coded data (after SOS)
        self._w: int | None = None
        self._h: int | None = None
        self.bytes_dropped = 0   # garbage skipped between frames

    # -- public API ---------------------------------------------------------
    def feed(self, chunk: bytes) -> list[JpegFrame]:
        """Add bytes from the stream; return every frame completed by this chunk."""
        self._buf += chunk
        out: list[JpegFrame] = []
        while True:
            if not self._in_frame:
                idx = self._buf.find(b"\xff\xd8")
                if idx < 0:
                    # Keep a trailing FF: it may be the first half of the next SOI.
                    keep = 1 if self._buf and self._buf[-1] == 0xFF else 0
                    self.bytes_dropped += len(self._buf) - keep
                    del self._buf[: len(self._buf) - keep]
                    break
                if idx > 0:
                    self.bytes_dropped += idx
                    del self._buf[:idx]
                self._in_frame = True
                self._pos = 2
                self._entropy = False
                self._w = self._h = None

            result = self._parse(out)
            if result == "need_more":
                if len(self._buf) > MAX_BUFFER_BYTES:
                    # Hopeless: drop what we have and resynchronise on the next SOI.
                    self._reset_frame(drop_all=True)
                    continue
                break
            if result == "resync":
                continue
            # "done" -> the loop starts over looking for the next SOI
        return out

    # -- internals ----------------------------------------------------------
    def _reset_frame(self, *, drop_all: bool) -> None:
        self._in_frame = False
        self._entropy = False
        if drop_all:
            self.bytes_dropped += len(self._buf)
            self._buf.clear()
        else:
            # Drop the SOI so the search finds the *next* one, not this broken frame.
            self.bytes_dropped += min(2, len(self._buf))
            del self._buf[:2]

    def _emit(self, end: int, out: list[JpegFrame]) -> None:
        out.append(JpegFrame(bytes(self._buf[:end]), self._w, self._h))
        del self._buf[:end]
        self._in_frame = False
        self._entropy = False
        self._pos = 0

    def _parse(self, out: list[JpegFrame]) -> str:
        """Advance the parse over the buffer.

        Returns ``"need_more"`` (wait for the next chunk), ``"done"`` (a frame was
        emitted) or ``"resync"`` (the frame is corrupt; abandon and search again).
        """
        buf = self._buf
        n = len(buf)
        p = self._pos

        while True:
            if self._entropy:
                # Scan entropy-coded data for a real marker.
                exhausted = False
                while p < n:
                    # Jump to the next 0xFF at C speed. Stepping byte by byte in Python
                    # cost ~15M iterations/s at 5 cameras x 30 fps and held the GIL so
                    # long that the asyncio loop serving the phones stalled for seconds.
                    p = buf.find(0xFF, p)
                    if p < 0:
                        p = n
                        break
                    ff_start = p
                    while p < n and buf[p] == 0xFF:
                        p += 1
                    if p >= n:
                        # Chunk ended inside the FF fill: rewind and wait for more.
                        p = ff_start
                        exhausted = True
                        break
                    m = buf[p]
                    if m == 0x00 or 0xD0 <= m <= 0xD7:
                        p += 1  # stuffed byte or restart marker: stay in entropy data
                        continue
                    if m == _EOI:
                        self._emit(p + 1, out)
                        return "done"
                    # Some other marker ends the scan (multi-scan JPEG); walk it.
                    self._entropy = False
                    break
                if exhausted or p >= n:
                    self._pos = p
                    return "need_more"

            # Marker walk. `p` points at the first FF of a marker (or at a marker
            # byte just after the entropy scan).
            while p < n and buf[p] == 0xFF:
                p += 1
            if p >= n:
                self._pos = p
                return "need_more"
            m = buf[p]
            p += 1
            if m in _STANDALONE:
                continue
            if m == _EOI:
                self._emit(p, out)
                return "done"
            if m == _SOI:
                # A nested SOI means we lost sync; restart the frame here.
                self._pos = p
                self._reset_frame(drop_all=False)
                return "resync"
            if p + 2 > n:
                self._pos = p - 1  # rewind onto the marker byte
                return "need_more"
            seg_len = (buf[p] << 8) | buf[p + 1]
            if seg_len < 2:
                self._pos = p
                self._reset_frame(drop_all=False)
                return "resync"
            if m == _SOS:
                # Segment payload is the scan header; entropy data follows it.
                if p + seg_len > n:
                    self._pos = p - 1
                    return "need_more"
                p += seg_len
                self._entropy = True
                continue
            if p + seg_len > n:
                self._pos = p - 1
                return "need_more"
            if m in _SOF and seg_len >= 7:
                self._h = (buf[p + 3] << 8) | buf[p + 4]
                self._w = (buf[p + 5] << 8) | buf[p + 6]
            p += seg_len


def sof_size(data: bytes) -> tuple[int | None, int | None]:
    """Parse width/height out of a complete standalone JPEG (SOF0/SOF2)."""
    splitter = JpegFrameSplitter()
    frames = splitter.feed(data)
    if frames:
        return frames[0].width, frames[0].height
    return None, None
