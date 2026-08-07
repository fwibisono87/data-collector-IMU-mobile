"""End-to-end: run a real session and verify the sampling-tier filename + rename.

Two simulated devices — one delivering distinct values every packet (true 100 Hz), one
repeating every second sample (true 50 Hz) — record a short session through the real backend.
Asserts each CSV is named with the tier it actually attained, and that the file is re-tiered
at close from the session average rather than left on the preflight guess.
"""
import asyncio
import json
import math
import os
import struct
import sys
import time
from pathlib import Path

import websockets

HOST = "127.0.0.1"
PORT = int(os.getenv("PORT", "8078"))
SSD = Path(sys.argv[1])


def _v(v):
    b = []
    while v > 0x7F:
        b.append((v & 0x7F) | 0x80); v >>= 7
    b.append(v & 0x7F)
    return bytes(b)


def _s(f, v): e = v.encode(); return _v((f << 3) | 2) + _v(len(e)) + e
def _f(f, v): return _v((f << 3) | 5) + struct.pack("<f", v)
def _i(f, v): return _v((f << 3) | 0) + _v(v)


def reg(d, r):
    return _s(1, d) + _s(2, r) + _s(3, "Sim") + _s(4, "14") + _s(5, "2.2.0") + _i(6, 2)


def ping(c): return _i(1, 0) + _s(4, c)


def pkt(seq, d, held_every):
    th = (seq // held_every) * 0.1
    t = int(time.time() * 1000)
    return (_f(1, 0.01 * math.sin(th)) + _f(2, -0.02 * math.cos(th)) +
            _f(3, 1.0 + 0.005 * math.sin(th * 2)) + _f(4, 0.5 * math.sin(th * 0.5)) +
            _f(5, -0.3 * math.cos(th * 0.5)) + _f(6, 0.1 * math.sin(th * 0.7)) +
            _i(7, t) + _i(8, seq) + _s(9, d) + _i(10, 2) + _i(11, t))


async def device(dev_id, role, held_every, stop):
    ctrl = await websockets.connect(f"ws://{HOST}:{PORT}/ws/control")
    await ctrl.send(reg(dev_id, role))
    tel = await websockets.connect(f"ws://{HOST}:{PORT}/ws/telemetry")

    async def pinger():
        i = 0
        while not stop.is_set():
            try: await ctrl.send(ping(f"p{i}"))
            except Exception: return
            i += 1
            try: await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError: pass

    asyncio.create_task(pinger())
    seq, nxt = 0, time.monotonic()
    while not stop.is_set():
        try: await tel.send(pkt(seq, dev_id, held_every))
        except Exception: return
        seq += 1
        nxt += 0.01
        await asyncio.sleep(max(0, nxt - time.monotonic()))


async def main():
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(device("sim-full", "waist", 1, stop)),
        asyncio.create_task(device("sim-half", "chest", 2, stop)),
    ]
    await asyncio.sleep(8)          # let preflight rates settle (5 s window)

    fe = await websockets.connect(f"ws://{HOST}:{PORT}/ws/frontend")
    await fe.send(json.dumps({"type": "START_SESSION", "command_id": "c1",
                              "payload": {"subject_name": "TierTest",
                                          "session_tag": "Run1", "operator": "auto"}}))
    print("started; recording 8 s…")
    await asyncio.sleep(8)
    await fe.send(json.dumps({"type": "STOP_SESSION", "command_id": "c2",
                              "payload": {"reason": "operator_stop"}}))
    await asyncio.sleep(5)

    stop.set()
    for t in tasks: t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    folder = SSD / "Data_Riset_IMU" / "TierTest_Run1"
    files = sorted(p.name for p in folder.glob("*_sensor_data.csv")) if folder.is_dir() else []
    print(f"\nfiles in {folder}:")
    for f in files: print("   ", f)

    ok = True
    waist = [f for f in files if "_waist_" in f]
    chest = [f for f in files if "_chest_" in f]
    if not waist or "100hz" not in waist[0]:
        print(f"FAIL: waist (true 100 Hz) should be 100hz, got {waist}"); ok = False
    if not chest or "50hz" not in chest[0]:
        print(f"FAIL: chest (true 50 Hz) should be 50hz, got {chest}"); ok = False
    if any("unkhz" in f for f in files):
        print("FAIL: a file was left unlabelled"); ok = False

    # The rename must be reflected in the integrity report, not just on disk.
    rep = list(folder.glob("*_integrity_report.json")) if folder.is_dir() else []
    if rep:
        data = json.loads(rep[0].read_text())
        for d in data["devices"]:
            p = Path(d["csv_path"])
            print(f"    report: {d['role']:8s} -> {p.name}  exists={p.exists()}")
            if not p.exists():
                print("FAIL: integrity report points at a path that no longer exists"); ok = False
    else:
        print("FAIL: no integrity report written"); ok = False

    print("\nPASS: tiers named and re-tiered correctly" if ok else "\nFAILED")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
