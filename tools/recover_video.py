#!/usr/bin/env python3
"""Recover footage from session .webm files that hold more than one recording.

Why this exists
---------------
Until the 2026-08-14 fix, a `STATE_UPDATE` carrying state=RECORDING re-armed
`startRecording` every time it arrived rather than only on the transition into
RECORDING, and `startFn` replaced `mediaRef.current` without stopping the recorder
already running on that camera. Two MediaRecorders then wrote into one chunk index,
so a saved file is the *interleaved concatenation of two parallel recordings* of the
same camera, each with its own EBML header. Players parse the first header, reach the
second where clusters should continue, and stop — the "only the first second plays"
symptom.

What this can and cannot do
---------------------------
`streamChunks` concatenates chunks in index order, so the file alternates ~1s chunks:
A0 B0 A1 B1 ... Chunk switches are detected as backward jumps in block timestamps.

That detection is INCOMPLETE, and knowing why matters. Both recorders filmed the same
camera, so their timestamps advance in near-lockstep; when a switch lands on a block whose
timestamp still exceeds the previous one, no backward jump appears and the switch is
invisible. On the reference session only ~55% of switches were detectable (639 runs where
~1150 were expected), and 590 of 638 detected switches fell MID-cluster rather than on a
cluster boundary. So the two recordings cannot be reliably separated from the concatenated
bytes alone: `.split*.webm` outputs are diagnostic, and generally still contain stray bytes
from the other recording.

The dependable output is `.combined.webm` — a whole-file `ffmpeg -c copy`, which drops
unparseable bytes and keeps one coherent picture track covering the whole session. Since
both recorders filmed the SAME camera, the scene is intact; but frames from the two
recordings are mixed, so its timeline is approximate and must NOT be trusted for
frame-accurate IMU sync.

For an exact reconstruction, recover the per-chunk records from the dashboard's IndexedDB
(`imu-video-backup`, store `chunks`) instead: there each chunk is a separate record with
its index, so the two recorders separate cleanly. Chunks survive until the start of the
NEXT session, and only then if the session was confirmed-saved.

The `ffmpeg -c copy` pass also rebuilds SeekHead/Info/Cues, so output has a real duration
and can be seeked — MediaRecorder never writes those for a live stream.

Usage
-----
    recover_video.py SESSION.zip  -o OUTDIR
    recover_video.py a.webm b.webm -o OUTDIR

Nothing is ever deleted, and short fragments are written out too (flagged in the report)
rather than discarded.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

EBML_MAGIC = b"\x1a\x45\xdf\xa3"
CLUSTER_ID = b"\x1f\x43\xb6\x75"
TIMECODE_ID = 0xE7
SIMPLEBLOCK_ID = 0xA3
BLOCKGROUP_ID = 0xA0

# A run shorter than this is a fragment, not a recording. Reported, never dropped.
FRAGMENT_S = 2.0


def _vint(buf: bytes, p: int, keep_marker: bool) -> tuple[int | None, int]:
    """Read an EBML variable-length integer at `p`. Returns (value, next_pos)."""
    n = len(buf)
    if p >= n:
        return None, p
    b = buf[p]
    if b == 0:
        return None, p
    length, mask = 1, 0x80
    while not b & mask:
        length += 1
        mask >>= 1
        if length > 8:
            return None, p
    if p + length > n:
        return None, p
    value = b if keep_marker else (b & (mask - 1))
    for i in range(1, length):
        value = (value << 8) | buf[p + i]
    return value, p + length


@dataclass
class Block:
    """One SimpleBlock: where its element starts, and its absolute timestamp."""
    elem_start: int
    ts_ms: int
    # Offset of the Cluster header that opens this block's cluster, when that cluster
    # begins immediately before it. A chunk that starts on a cluster boundary must be
    # cut at the Cluster ID, not at the block.
    cluster_start: int | None


def parse_blocks(data: bytes) -> list[Block]:
    """Walk every cluster and collect its blocks with absolute timestamps."""
    blocks: list[Block] = []
    i = data.find(CLUSTER_ID)
    while i != -1:
        nxt = data.find(CLUSTER_ID, i + 1)
        end = nxt if nxt != -1 else len(data)

        p = i + 4
        _, p = _vint(data, p, False)          # cluster size (unknown in live streams)
        if p is None:
            break
        sid, q = _vint(data, p, True)
        cluster_tc = None
        if sid == TIMECODE_ID:
            size, q3 = _vint(data, q, False)
            if size is not None and 0 < size <= 8:
                cluster_tc = int.from_bytes(data[q3:q3 + size], "big")
                p = q3 + size

        first_in_cluster = True
        while cluster_tc is not None and p is not None and p < end:
            elem_start = p
            eid, p2 = _vint(data, p, True)
            if eid is None:
                break
            esize, p3 = _vint(data, p2, False)
            if esize is None or p3 + esize > end:
                break
            if eid in (SIMPLEBLOCK_ID, BLOCKGROUP_ID):
                # BlockGroup wraps a Block whose header has the same shape.
                bp = p3
                if eid == BLOCKGROUP_ID:
                    bid, b2 = _vint(data, p3, True)
                    bsz, b3 = _vint(data, b2, False)
                    bp = b3 if bid == 0xA1 and bsz is not None else None
                if bp is not None:
                    _, r = _vint(data, bp, False)          # track number
                    if r is not None and r + 2 <= len(data):
                        rel = int.from_bytes(data[r:r + 2], "big", signed=True)
                        blocks.append(Block(
                            elem_start=elem_start,
                            ts_ms=cluster_tc + rel,
                            cluster_start=i if first_in_cluster else None,
                        ))
                        first_in_cluster = False
            p = p3 + esize
        i = nxt
    return blocks


@dataclass
class Run:
    """A maximal stretch of the file whose block timestamps only move forward."""
    start: int
    first_ts: int
    last_ts: int


def split_runs(blocks: list[Block], headers: list[int]) -> list[Run]:
    """Cut the file into byte runs at every backward timestamp jump.

    A recorder's very first chunk opens with its own EBML header + Info + Tracks. That
    prefix sits between the previous recorder's last block and this run's first cluster,
    so cutting at the cluster would strand it on the wrong stream — leaving one stream
    headerless and planting a stray header inside the other.
    """
    if not blocks:
        return []
    runs = [Run(start=0, first_ts=blocks[0].ts_ms, last_ts=blocks[0].ts_ms)]
    for prev, cur in zip(blocks, blocks[1:]):
        if cur.ts_ms < prev.ts_ms:
            # The new chunk begins at its cluster header when it opens one, otherwise at
            # the block element itself.
            cut = cur.cluster_start if cur.cluster_start is not None else cur.elem_start
            # ...unless a fresh EBML header sits in the gap: that IS the chunk boundary.
            for h in headers:
                if prev.elem_start < h <= cut:
                    cut = h
                    break
            runs.append(Run(start=cut, first_ts=cur.ts_ms, last_ts=cur.ts_ms))
        else:
            runs[-1].last_ts = cur.ts_ms
    return runs


@dataclass
class Stream:
    runs: list[tuple[int, int]] = field(default_factory=list)   # (start, end)
    last_ts: int = -1


def assign_runs(data: bytes, runs: list[Run], max_streams: int) -> list[Stream]:
    """Assign each run to the stream it continues.

    Both recorders film the same camera, so their timestamps advance nearly in lockstep
    and cannot be told apart by value. The discriminator is continuity: a run belongs to
    whichever stream it extends without moving backwards. Comparing against the stream's
    LAST timestamp (not the run's first) is what separates the two ramps — a run that
    restarts below where a stream already reached must belong to the other one.

    One recorder writes exactly one EBML header, so `max_streams` (the header count) is
    the true number of recorders. Without that bound, ordinary timestamp jitter — a run
    starting fractionally below where both streams already reached — spawns phantom
    streams made of stray bytes that no longer parse as WebM.
    """
    bounds = [r.start for r in runs] + [len(data)]
    streams: list[Stream] = []
    for idx, run in enumerate(runs):
        candidates = [s for s in streams if s.last_ts <= run.first_ts]
        if candidates:
            target = max(candidates, key=lambda s: s.last_ts)
        elif len(streams) < max_streams:
            target = Stream()
            streams.append(target)
        else:
            # Jitter, not a new recorder: give it to whichever stream is furthest behind.
            target = min(streams, key=lambda s: s.last_ts)
        target.runs.append((run.start, bounds[idx + 1]))
        target.last_ts = max(target.last_ts, run.last_ts)
    return streams


def rebuild(data: bytes, stream: Stream) -> bytes:
    return b"".join(data[s:e] for s, e in stream.runs)


def remux(src: Path, dst: Path) -> dict:
    """ffmpeg -c copy: rebuilds SeekHead/Info/Cues without touching the frames."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(src), "-c", "copy", "-y", str(dst)],
        capture_output=True, text=True,
    )
    return {
        "ffmpeg_stderr": proc.stderr.strip(),
        "duration_s": probe_duration(dst) if dst.exists() else None,
        "bytes": dst.stat().st_size if dst.exists() else 0,
    }


def probe_duration(path: Path) -> float | None:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return round(float(proc.stdout.strip()), 3)
    except ValueError:
        return None


def recover_file(path: Path, outdir: Path, cameras: dict | None) -> dict:
    data = path.read_bytes()
    headers = [i for i in range(len(data)) if data.startswith(EBML_MAGIC, i)]
    blocks = parse_blocks(data)
    runs = split_runs(blocks, headers)
    streams = assign_runs(data, runs, max_streams=max(1, len(headers)))

    report: dict = {
        "file": path.name,
        "bytes": len(data),
        "ebml_headers": len(headers),
        "header_offsets": headers,
        "blocks": len(blocks),
        "runs": len(runs),
        "streams_found": len(streams),
        "outputs": [],
    }

    stem = path.stem

    # Best-effort whole-file remux. This is the DEPENDABLE output: ffmpeg drops the bytes
    # it cannot parse and keeps a single coherent picture track. Because both recorders
    # filmed the same camera, what survives is the real scene for the full session — but
    # frames from the two recordings are mixed, so the timeline is approximate and must
    # not be trusted for frame-accurate IMU sync.
    combined = outdir / f"{stem}.combined.webm"
    report["combined"] = {"output": combined.name, **remux(path, combined)}

    for n, stream in enumerate(streams):
        raw = outdir / f"{stem}.stream{n}.raw.webm"
        raw.write_bytes(rebuild(data, stream))
        final = outdir / f"{stem}.split{n}.webm"
        info = remux(raw, final)
        raw.unlink(missing_ok=True)
        entry = {
            "output": final.name,
            "runs": len(stream.runs),
            "last_block_ts_s": round(stream.last_ts / 1000, 3),
            **info,
        }
        entry["fragment"] = (info["duration_s"] or 0) < FRAGMENT_S
        report["outputs"].append(entry)

    if cameras:
        cam = match_camera(path.name, cameras)
        if cam:
            expected = (cam["stopped_at_ms"] - cam["started_at_ms"]) / 1000
            report["expected_duration_s"] = round(expected, 3)
            report["camera"] = {k: cam.get(k) for k in
                                ("cam_id", "started_at_ms", "flash_at_ms", "stopped_at_ms")}
            for entry in [report["combined"], *report["outputs"]]:
                if entry["duration_s"] is not None:
                    entry["vs_expected_s"] = round(entry["duration_s"] - expected, 3)
    return report


def match_camera(filename: str, cameras: dict) -> dict | None:
    for cam in cameras.get("cameras", []):
        if f"_{cam['cam_id']}_" in filename:
            return cam
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path, help="session .zip, or .webm files")
    ap.add_argument("-o", "--outdir", type=Path, required=True)
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"error: {tool} not found on PATH", file=sys.stderr)
            return 2

    args.outdir.mkdir(parents=True, exist_ok=True)
    targets: list[Path] = []
    cameras: dict | None = None

    for src in args.inputs:
        if src.suffix == ".zip":
            with zipfile.ZipFile(src) as zf:
                for name in zf.namelist():
                    if name.endswith("cameras.json"):
                        cameras = json.loads(zf.read(name))
                    elif name.endswith(".webm"):
                        dest = args.outdir / Path(name).name
                        with zf.open(name) as fh, open(dest, "wb") as out:
                            shutil.copyfileobj(fh, out, 8 << 20)
                        targets.append(dest)
        else:
            targets.append(src)

    reports = []
    for target in targets:
        print(f"-- {target.name}", flush=True)
        rep = recover_file(target, args.outdir, cameras)
        reports.append(rep)
        print(f"   {rep['ebml_headers']} EBML header(s), {rep['blocks']} blocks, "
              f"{rep['runs']} runs -> {rep['streams_found']} stream(s)")
        comb = rep["combined"]
        vs = rep.get("expected_duration_s")
        vs_txt = f", {comb['duration_s'] - vs:+.3f}s vs expected" if vs and comb["duration_s"] else ""
        print(f"   USE THIS -> {comb['output']}: {comb['duration_s']}s{vs_txt}")
        if rep["ebml_headers"] > 1:
            print("     (two recordings were mixed into this file; timeline is approximate "
                  "— see module docstring before using it for IMU sync)")
        for entry in rep["outputs"]:
            flag = "  [FRAGMENT]" if entry["fragment"] else ""
            vs = entry.get("vs_expected_s")
            vs_txt = f", {vs:+.3f}s vs expected" if vs is not None else ""
            print(f"     {entry['output']}: {entry['duration_s']}s"
                  f"{vs_txt}{flag}")
            if entry["ffmpeg_stderr"]:
                first = entry["ffmpeg_stderr"].splitlines()[0]
                print(f"       ffmpeg: {first}")

    out = args.outdir / "recovery_report.json"
    out.write_text(json.dumps(reports, indent=2))
    print(f"\nreport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
