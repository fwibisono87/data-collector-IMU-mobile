"""Codec benchmark: N fake 720p30 cameras recorded for T seconds per codec.

Spec (docs/rebuild/lanes/L5-cameras.md §3): run 5 fake cameras at 720p30 for
60 s with each codec on this machine and report CPU %, real encoder fps vs 30
and dropped frames. If vp9 cannot sustain 5 cameras with <1 % drops, vp8
becomes the default.

Run as a module so the env redirect below happens before anything imports
``master_backend.app.export`` (its ``SSD_PATH`` is frozen at import time):

    master_backend/.venv/bin/python -m master_backend.app.camera.benchmark
    # shorter sanity run:
    ... -m master_backend.app.camera.benchmark --duration 8 --cameras 2 --codecs vp9

CPU is measured on Linux from ``/proc/<pid>/stat`` (utime+stime) for the
backend process itself (frame pumps + reader threads live here), every ffmpeg
capture process and every encoder process. Percentages are relative to one
core; the machine total divides by ``os.cpu_count()``.
"""
from __future__ import annotations

import os
import tempfile

# Must happen before importing .service: the anchor writer chain
# (camera.service -> app.cameras -> app.export) freezes SSD_PATH at import.
_BENCH_TMP = tempfile.mkdtemp(prefix="imu-l5-bench-")
os.environ.setdefault("SSD_PATH", _BENCH_TMP)
os.environ["IMU_CAMERAS"] = "fake"

import argparse  # noqa: E402
import asyncio  # noqa: E402
import shutil  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from ..api.schema import CameraSelectionItem  # noqa: E402
from .service import create_service  # noqa: E402

_TCK = os.sysconf("SC_CLK_TCK")


def _proc_cpu_seconds(pid: int) -> float:
    """utime+stime seconds consumed by ``pid`` (Linux /proc)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return 0.0
    # Skip "pid (comm)"; comm may contain spaces/parens, so split after the
    # last ')'. Field 12/13 after that are utime/stime in clock ticks.
    fields = stat[stat.rindex(")") + 2:].split()
    return (int(fields[11]) + int(fields[12])) / _TCK


def _collect_pids(svc) -> dict[str, int]:
    """label -> pid for the backend process, captures and current encoders."""
    pids = {"backend": os.getpid()}
    for cam_id in sorted(svc._workers):
        worker = svc.worker(cam_id)
        proc = worker.process if worker else None
        if proc is not None:
            pids[f"capture:{cam_id}"] = proc.pid
    recorder = svc._recorder
    if recorder is not None:
        for cam_id, rec in sorted(recorder.recorders.items()):
            if rec.parts and rec.parts[-1].proc.poll() is None:
                pids[f"encoder:{cam_id}"] = rec.parts[-1].proc.pid
    return pids


def _cpu_snapshot(pids: dict[str, int]) -> dict[str, float]:
    return {label: _proc_cpu_seconds(pid) for label, pid in pids.items()}


async def _run_codec(codec: str, cameras: int, duration: float) -> dict:
    os.environ["IMU_FAKE_CAMERAS"] = str(cameras)
    os.environ["IMU_VIDEO_CODEC"] = codec

    svc = create_service()
    result: dict = {"codec": codec}
    try:
        svc.set_selection(
            [CameraSelectionItem(device_key=f"fake:{i}") for i in range(cameras)]
        )
        if not svc.wait_live(timeout=15):
            raise RuntimeError(f"{codec}: cameras did not go live")
        await asyncio.sleep(1.0)  # let the fps window settle

        session_id = f"bench_{codec}_{int(time.time())}"
        folder = Path(os.environ["SSD_PATH"]) / "Data_Riset_IMU" / "bench"
        folder.mkdir(parents=True, exist_ok=True)
        await svc.start_recording(session_id, folder)

        pids = _collect_pids(svc)
        before = _cpu_snapshot(pids)
        started = time.monotonic()
        await asyncio.sleep(duration)
        elapsed = time.monotonic() - started
        after = _cpu_snapshot(pids)

        # Per-camera encoder throughput and drops, read before stopping.
        recorder = svc._recorder
        per_cam = {}
        for cam_id, rec in sorted(recorder.recorders.items()):
            part = rec.parts[-1]
            worker = svc.worker(cam_id)
            received = worker.frames_received if worker else 0
            dropped = recorder.dropped_frames().get(cam_id, 0)
            per_cam[cam_id] = {
                "written": part.frames_written,
                "received": received,
                "dropped": dropped,
                "fps": part.frames_written / elapsed,
            }
        result.update(
            elapsed=elapsed,
            pids=pids,
            cpu={label: after[label] - before.get(label, 0.0)
                 for label in after},
            per_cam=per_cam,
        )
        await svc.stop_recording(session_id)
    finally:
        await svc.close()
    return result


def _print_report(res: dict, cores: int) -> None:
    codec, elapsed = res["codec"], res["elapsed"]
    cpu = res["cpu"]
    total_cpu = sum(cpu.values())
    pct_one_core = total_cpu / elapsed * 100.0
    pct_machine = pct_one_core / cores
    print(f"\n=== codec {codec} — {elapsed:.1f}s, {cores} cores ===")
    for label in sorted(cpu):
        print(f"  {label:<16} {cpu[label] / elapsed * 100:6.1f} % of one core")
    print(f"  {'TOTAL':<16} {pct_one_core:6.1f} % of one core "
          f"= {pct_machine:5.1f} % of machine")
    written = received = dropped = 0
    for cam_id, cam in sorted(res["per_cam"].items()):
        written += cam["written"]
        received += cam["received"]
        dropped += cam["dropped"]
        print(f"  {cam_id}: {cam['fps']:5.1f} fps encoded "
              f"({cam['written']} written, {cam['received']} received, "
              f"{cam['dropped']} dropped)")
    drop_pct = dropped / received * 100.0 if received else 0.0
    mean_fps = written / elapsed / max(1, len(res["per_cam"]))
    print(f"  mean encoder fps: {mean_fps:.1f} (target 30); "
          f"dropped {dropped}/{received} = {drop_pct:.2f} %")
    verdict = "SUSTAINS" if drop_pct < 1.0 and mean_fps >= 29.0 else "FAILS"
    print(f"  verdict: {verdict} {len(res['per_cam'])}x720p30 "
          f"(<1% drops, fps>=29)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--duration", type=float, default=60.0,
                        help="seconds to record per codec (default 60)")
    parser.add_argument("--cameras", type=int, default=5,
                        help="fake cameras (default 5)")
    parser.add_argument("--codecs", default="vp9,vp8",
                        help="comma-separated codecs (default vp9,vp8)")
    args = parser.parse_args(argv)

    cores = os.cpu_count() or 1
    codecs = [c.strip() for c in args.codecs.split(",") if c.strip()]
    print(f"benchmark: {args.cameras} fake cameras, {args.duration:.0f}s per "
          f"codec {codecs}, SSD_PATH={os.environ['SSD_PATH']}")
    try:
        for codec in codecs:
            res = asyncio.run(_run_codec(codec, args.cameras, args.duration))
            _print_report(res, cores)
    finally:
        if os.environ.get("SSD_PATH") == _BENCH_TMP:
            shutil.rmtree(_BENCH_TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
