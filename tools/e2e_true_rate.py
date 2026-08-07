"""End-to-end check of live true-rate detection while the session is IDLE.

Manual integration test — needs a running backend:

    SSD_PATH=/tmp/e2e PORT=8078 python -m uvicorn master_backend.app.main:app --port 8078 &
    python tools/e2e_true_rate.py

Reproduces the 2026-08-07 failure shape: a device that streams a confident 100 packets/sec
while the underlying hardware only produces 50 distinct readings per second (every other
packet repeats the previous acc triple). The dashboard's preflight gate must see true_hz ~50
for that device and ~100 for a healthy one, WHILE IDLE — preflight runs before recording.
"""
import asyncio
import json
import math
import struct
import sys
import time

import websockets

HOST = "127.0.0.1"
PORT = 8078


def _varint(v: int) -> bytes:
    buf = []
    while v > 0x7F:
        buf.append((v & 0x7F) | 0x80)
        v >>= 7
    buf.append(v & 0x7F)
    return bytes(buf)


def _str_field(f: int, v: str) -> bytes:
    e = v.encode()
    return _varint((f << 3) | 2) + _varint(len(e)) + e


def _float_field(f: int, v: float) -> bytes:
    return _varint((f << 3) | 5) + struct.pack("<f", v)


def _int_field(f: int, v: int) -> bytes:
    return _varint((f << 3) | 0) + _varint(v)


def build_register(device_id: str, role: str) -> bytes:
    return (_str_field(1, device_id) + _str_field(2, role) + _str_field(3, "Simulator")
            + _str_field(4, "14") + _str_field(5, "2.1.0") + _int_field(6, 2))


def build_ping(cid: str) -> bytes:
    return _int_field(1, 0) + _str_field(4, cid)


def build_packet(seq: int, device_id: str, held_every: int) -> bytes:
    """held_every=1 -> every packet distinct (true 100 Hz).
       held_every=2 -> value changes only every 2nd packet (true 50 Hz, 50% held)."""
    theta = (seq // held_every) * 0.1
    t = int(time.time() * 1000)
    return (
        _float_field(1, 0.01 * math.sin(theta)) +
        _float_field(2, -0.02 * math.cos(theta)) +
        _float_field(3, 1.0 + 0.005 * math.sin(theta * 2)) +
        _float_field(4, 0.5 * math.sin(theta * 0.5)) +
        _float_field(5, -0.3 * math.cos(theta * 0.5)) +
        _float_field(6, 0.1 * math.sin(theta * 0.7)) +
        _int_field(7, t) + _int_field(8, seq) + _str_field(9, device_id) +
        _int_field(10, 2) + _int_field(11, t)
    )


async def device(device_id: str, role: str, held_every: int, stop: asyncio.Event):
    ctrl = await websockets.connect(f"ws://{HOST}:{PORT}/ws/control")
    await ctrl.send(build_register(device_id, role))
    tel = await websockets.connect(f"ws://{HOST}:{PORT}/ws/telemetry")

    async def pinger():
        i = 0
        while not stop.is_set():
            try:
                await ctrl.send(build_ping(f"p{i}"))
            except Exception:
                return
            i += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    asyncio.create_task(pinger())
    seq = 0
    interval = 1 / 100.0          # 100 PACKETS per second regardless of held_every
    nxt = time.monotonic()
    while not stop.is_set():
        try:
            await tel.send(build_packet(seq, device_id, held_every))
        except Exception:
            return
        seq += 1
        nxt += interval
        await asyncio.sleep(max(0, nxt - time.monotonic()))


async def main():
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(device("sim-healthy", "chest", 1, stop)),
        asyncio.create_task(device("sim-halfrate", "waist", 2, stop)),
    ]
    await asyncio.sleep(2)

    fe = await websockets.connect(f"ws://{HOST}:{PORT}/ws/frontend")
    snapshots = []
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(fe.recv(), timeout=3)
        except asyncio.TimeoutError:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if msg.get("type") == "STATE_UPDATE" and msg.get("devices"):
            snapshots.append(msg)

    stop.set()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if not snapshots:
        print("FAIL: no STATE_UPDATE with devices received while IDLE")
        return 1

    last = snapshots[-1]
    print(f"session state during test: {last.get('state')}")
    print(f"STATE_UPDATE snapshots received while IDLE: {len(snapshots)}")
    by_role = {d["role"]: d for d in last["devices"]}
    for role, d in sorted(by_role.items()):
        print(f"  {role:8s} packets={d.get('packets'):>6} rate_hz={d.get('rate_hz'):>6} "
              f"true_hz={d.get('true_hz'):>6} held_pct={d.get('held_pct'):>6} "
              f"app_version={d.get('app_version')}")

    ok = True
    healthy = by_role.get("chest")
    half = by_role.get("waist")
    if not healthy or not half:
        print("FAIL: expected both roles present")
        return 1
    if not (80 <= healthy["true_hz"] <= 115):
        print(f"FAIL: healthy device true_hz={healthy['true_hz']} not ~100"); ok = False
    if not (35 <= half["true_hz"] <= 65):
        print(f"FAIL: half-rate device true_hz={half['true_hz']} not ~50"); ok = False
    if half["held_pct"] < 35:
        print(f"FAIL: half-rate held_pct={half['held_pct']} should be ~50"); ok = False
    if healthy["held_pct"] > 15:
        print(f"FAIL: healthy held_pct={healthy['held_pct']} should be ~0"); ok = False

    print("\nPASS: half-rate device detected while IDLE" if ok else "\nFAILED")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
