"""WebM finalisation evidence: EBML walking, ffprobe duration, remux.

``video_report.json`` (OUTPUT_CONTRACT.md §9) needs, per file: the probed duration and
the number of ``1A45DFA3`` EBML headers **at top-level element positions** — a
substring search would also count the bytes appearing inside payloads, so the file is
walked as EBML: every top-level element is (id, size) and the next element starts
after ``size`` bytes. Two concatenated recordings therefore report 2 headers, a
healthy single recording exactly 1, and ``healthy`` in the report is
``every(v => v.ok && v.ebmlHeaders === 1)``.

A file whose encoder was killed has no Duration and no Cues; :func:`remux` rewrites
the container with ``ffmpeg -c copy`` (the same thing ``tools/fix_webm.sh`` does),
which rebuilds both without touching the compressed frames.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from .bins import ffmpeg_exe, ffprobe_exe

logger = logging.getLogger(__name__)

EBML_HEADER_ID = 0x1A45DFA3
SEGMENT_ID = 0x18538067
CUES_ID = 0x1C53BB6B

_PROBE_TIMEOUT_S = 30
_REMUX_TIMEOUT_S = 120


class _Reader:
    """Sequential EBML reader over a file object."""

    def __init__(self, fh) -> None:
        self._fh = fh

    def read_vint(self, *, strip_marker: bool) -> tuple[int, int] | None:
        """Read one EBML variable-size integer: ``(value, byte_length)``.

        With ``strip_marker`` the length-descriptor marker bit is removed (sizes);
        without it the raw bytes are returned (element IDs). Returns None at EOF or
        on a truncated vint.
        """
        first = self._fh.read(1)
        if not first:
            return None
        b0 = first[0]
        if b0 == 0:
            return None
        length = 1
        while length <= 8 and not (b0 & (0x80 >> (length - 1))):
            length += 1
        if length > 8:
            return None
        value = b0 & (0xFF >> length) if strip_marker else b0
        rest = self._fh.read(length - 1) if length > 1 else b""
        if len(rest) != length - 1:
            return None
        for byte in rest:
            value = (value << 8) | byte
        return value, length

    def read_size(self) -> tuple[int | None, int]:
        """Read an element size; ``None`` means "unknown size" (runs to EOF)."""
        vint = self.read_vint(strip_marker=True)
        if vint is None:
            return None, 0
        value, length = vint
        if value == (1 << (7 * length)) - 1:
            return None, length
        return value, length


def iter_top_level(path: Path):
    """Yield ``(element_id, size|None)`` for every top-level EBML element.

    Stops at EOF, at an unknown-size element (nothing follows it by definition) or
    at the first malformed vint.
    """
    with open(path, "rb") as fh:
        reader = _Reader(fh)
        while True:
            vint = reader.read_vint(strip_marker=False)
            if vint is None:
                return
            element_id, id_len = vint
            size, size_len = reader.read_size()
            yield element_id, size
            if size is None:
                return
            fh.seek(size, os.SEEK_CUR)


def count_ebml_headers(path: Path) -> int:
    """How many EBML header elements sit at top-level positions in ``path``."""
    try:
        return sum(1 for eid, _ in iter_top_level(path) if eid == EBML_HEADER_ID)
    except OSError:
        return 0


def has_cues(path: Path) -> bool:
    """Whether the (single) Segment of ``path`` contains a Cues element.

    Walks Segment children the same way as the top level. Live-muxed files with
    unknown sizes report False — after :func:`remux` every size is known.
    """
    try:
        with open(path, "rb") as fh:
            reader = _Reader(fh)
            while True:
                vint = reader.read_vint(strip_marker=False)
                if vint is None:
                    return False
                element_id, _ = vint
                size, _ = reader.read_size()
                if element_id == SEGMENT_ID:
                    break
                if size is None:
                    return False
                fh.seek(size, os.SEEK_CUR)
            if size is None:
                return False
            end = fh.tell() + size
            while fh.tell() < end:
                vint = reader.read_vint(strip_marker=False)
                if vint is None:
                    return False
                child_id, _ = vint
                if child_id == CUES_ID:
                    return True
                child_size, _ = reader.read_size()
                if child_size is None:
                    return False
                fh.seek(child_size, os.SEEK_CUR)
            return False
    except OSError:
        return False


def probe_duration_ms(path: Path) -> int | None:
    """ffprobe's ``format.duration`` in ms (rounded), or None when absent."""
    try:
        proc = subprocess.run(
            [ffprobe_exe(), "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffprobe failed for %s: %s", path, exc)
        return None
    if proc.returncode != 0:
        return None
    try:
        raw = json.loads(proc.stdout).get("format", {}).get("duration")
        if raw is None:
            return None
        return round(float(raw) * 1000)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def probe_frame_count(path: Path) -> int | None:
    """Decoded video frame count via ffprobe (``-count_frames``); None on failure."""
    try:
        proc = subprocess.run(
            [ffprobe_exe(), "-v", "error", "-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames", "-of",
             "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S * 4,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def remux(path: Path, *, genpts: bool = False) -> bool:
    """Rewrite the container in place so Duration and Cues exist.

    ``ffmpeg -i f -c copy tmp.webm`` then replace. With ``genpts`` timestamps are
    regenerated (the second attempt fix_webm.sh makes when a plain copy leaves no
    usable duration). Returns whether the file has a duration afterwards.
    """
    tmp = path.with_name(path.name + ".remux.tmp.webm")
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path)]
    if genpts:
        cmd += ["-fflags", "+genpts"]
    cmd += ["-c", "copy", str(tmp)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_REMUX_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("remux of %s failed: %s", path, exc)
        tmp.unlink(missing_ok=True)
        return False
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        logger.warning("remux of %s returned %s: %s", path, proc.returncode,
                       proc.stderr.decode(errors="replace")[-500:])
        tmp.unlink(missing_ok=True)
        return False
    os.replace(tmp, path)
    return probe_duration_ms(path) is not None
