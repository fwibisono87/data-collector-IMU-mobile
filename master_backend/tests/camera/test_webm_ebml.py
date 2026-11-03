"""Unit tests for the EBML walker on real recorded WebM files (spec §7).

The counter must see exactly one top-level EBML header in a clean file and two
in a naive concatenation of two files — that is how the recorder proves a
rollover part never got appended into one file.
"""
from __future__ import annotations

import subprocess

import pytest

from master_backend.app.camera import bins, webm


@pytest.fixture(scope="module")
def tiny_webm(tmp_path_factory) -> bytes:
    """A real ~1 s 320x240 vp9 WebM produced by ffmpeg itself."""
    out = tmp_path_factory.mktemp("webm") / "tiny.webm"
    cmd = [bins.ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
           "-t", "1", "-c:v", "libvpx-vp9", "-deadline", "realtime",
           "-cpu-used", "8", "-row-mt", "1", "-f", "webm", str(out)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return out.read_bytes()


def test_top_level_elements(tmp_path, tiny_webm):
    path = tmp_path / "one.webm"
    path.write_bytes(tiny_webm)
    ids = [eid for eid, _ in webm.iter_top_level(path)]
    assert ids[0] == webm.EBML_HEADER_ID
    assert ids[1] == webm.SEGMENT_ID
    assert webm.count_ebml_headers(path) == 1


def test_concatenated_files_have_two_headers(tmp_path, tiny_webm):
    path = tmp_path / "two.webm"
    path.write_bytes(tiny_webm + tiny_webm)
    assert webm.count_ebml_headers(path) == 2


def test_finalized_file_has_cues_and_duration(tmp_path, tiny_webm):
    path = tmp_path / "cues.webm"
    path.write_bytes(tiny_webm)
    assert webm.has_cues(path)
    duration = webm.probe_duration_ms(path)
    assert duration is not None
    assert abs(duration - 1000) < 400
    frames = webm.probe_frame_count(path)
    assert frames is not None and 20 <= frames <= 40


def test_garbage_is_zero_not_a_crash(tmp_path):
    path = tmp_path / "junk.webm"
    path.write_bytes(b"\x00\x01not ebml at all" * 10)
    assert webm.count_ebml_headers(path) == 0
    assert not webm.has_cues(path)
    assert webm.probe_duration_ms(path) is None


def test_missing_file_is_zero(tmp_path):
    path = tmp_path / "absent.webm"
    assert webm.count_ebml_headers(path) == 0
    assert not webm.has_cues(path)
