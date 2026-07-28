"""
Simulates a single IMU mobile node for backend+frontend testing without a phone.
Run from repo root: python tools/device_simulator.py

Sends: DeviceRegister, PING every 1s, SensorPacket at --rate Hz on telemetry channel.
Can simulate a mid-session drop (--drop-at/--drop-for/--drops — closes BOTH the
control and telemetry sockets, so the backend's is_online heuristic actually flips),
a telemetry-only drop that leaves control/PING alive (--drop-telemetry-only, reproduces
D15 — online but sending no data), a late join (--join-late), buffering-while-dark
(--buffer-while-dark, replayed on reconnect before live streaming resumes — emulates
FallbackBufferManager), and a sequence-counter restart after a drop (--seq-restart,
reproduces D5).
See connectivity_robustness_plan.md §T0/§T18 for the scenarios this drives.

Press Ctrl+C to disconnect.
"""
import argparse
import asyncio
import math
import struct
import time
import uuid

try:
    import websockets
except ImportError:
    raise SystemExit("Run: pip install websockets")


# ── Proto binary helpers ──────────────────────────────────────────────────────

def _varint(v: int) -> bytes:
    buf = []
    while v > 0x7F:
        buf.append((v & 0x7F) | 0x80)
        v >>= 7
    buf.append(v & 0x7F)
    return bytes(buf)


def _str_field(field: int, value: str) -> bytes:
    enc = value.encode()
    return _varint((field << 3) | 2) + _varint(len(enc)) + enc


def _float_field(field: int, value: float) -> bytes:
    return _varint((field << 3) | 5) + struct.pack("<f", value)


def _int_field(field: int, value: int) -> bytes:
    return _varint((field << 3) | 0) + _varint(value)


# ── Message builders ────────────────────────────────────────────────────────

def build_device_register(device_id: str, role: str) -> bytes:
    return (
        _str_field(1, device_id) +
        _str_field(2, role) +
        _str_field(3, "Simulator") +
        _str_field(4, "14") +
        _str_field(5, "2.0.0") +
        _int_field(6, 1)        # schema_version
    )


def build_ping(command_id: str) -> bytes:
    # Command: type=PING(0), command_id
    return _int_field(1, 0) + _str_field(4, command_id)


def build_sensor_packet(seq: int, device_id: str) -> bytes:
    t = int(time.time() * 1000)
    theta = seq * 0.1
    return (
        _float_field(1, 0.01 * math.sin(theta)) +      # acc_x
        _float_field(2, -0.02 * math.cos(theta)) +     # acc_y
        _float_field(3, 1.0 + 0.005 * math.sin(theta * 2)) +  # acc_z ≈ 1g
        _float_field(4, 0.5 * math.sin(theta * 0.5)) + # gyro_x
        _float_field(5, -0.3 * math.cos(theta * 0.5)) +# gyro_y
        _float_field(6, 0.1 * math.sin(theta * 0.7)) + # gyro_z
        _int_field(7, t) +          # timestamp_ms
        _int_field(8, seq) +        # sequence_number
        _str_field(9, device_id) +  # device_id
        _int_field(10, 1) +         # schema_version
        _int_field(11, t)           # raw_timestamp_ms
    )


# ── Simulator ────────────────────────────────────────────────────────────────

class Stats:
    def __init__(self) -> None:
        self.sent = 0
        self.buffered = 0
        self.replayed = 0


async def control_loop(ws, stop_event: asyncio.Event) -> None:
    """Send PING every 1s until stop_event is set or the socket dies."""
    ping_id = 0
    while not stop_event.is_set():
        cid = f"ping-{ping_id:04d}"
        try:
            await ws.send(build_ping(cid))
        except websockets.ConnectionClosed:
            return
        ping_id += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass


async def _stream_telemetry(ws, seq_holder: list, device_id: str, rate: float,
                             duration: float | None, stats: Stats,
                             buffer_out: list | None) -> None:
    """Send packets for `duration` seconds (None = forever) at `rate` Hz.

    If `buffer_out` is not None, packets are appended there instead of sent —
    used to emulate FallbackBufferManager while "dark" (no live socket exists then).
    """
    interval = 1.0 / rate
    started = time.monotonic()
    while duration is None or (time.monotonic() - started) < duration:
        seq = seq_holder[0]
        seq_holder[0] += 1
        pkt = build_sensor_packet(seq, device_id)
        if buffer_out is not None:
            buffer_out.append(pkt)
            stats.buffered += 1
        else:
            await ws.send(pkt)
            stats.sent += 1
        await asyncio.sleep(interval)


async def run(args: argparse.Namespace) -> None:
    device_id = args.device_id or ("sim-" + str(uuid.uuid4())[:8])
    stats = Stats()
    seq_holder = [0]

    print(f"device_id={device_id} role={args.role} rate={args.rate}Hz")

    if args.join_late > 0:
        print(f"  --join-late {args.join_late}s: waiting before first connect (reproduces D2)")
        await asyncio.sleep(args.join_late)

    total_drops = args.drops if args.drop_at is not None else 0
    pending_replay: list[bytes] = []

    if args.drop_telemetry_only:
        # Control channel (and its PING/PONG) never drops — only telemetry does. This
        # reproduces D15: the device stays "online" (green) on the dashboard the whole
        # time while silently sending no data.
        ctrl = await websockets.connect(f"ws://{args.host}:8000/ws/control")
        await ctrl.send(build_device_register(device_id, args.role))
        print("  DeviceRegister sent — control channel stays up for the whole run")
        stop_event = asyncio.Event()
        ctrl_task = asyncio.create_task(control_loop(ctrl, stop_event))
        try:
            for cycle in range(total_drops + 1):
                tel = await websockets.connect(f"ws://{args.host}:8000/ws/telemetry")
                is_last_cycle = cycle == total_drops
                duration = None if is_last_cycle else args.drop_at
                if is_last_cycle:
                    print(f"  Streaming at {args.rate} Hz. Press Ctrl+C to disconnect.\n")
                try:
                    await _stream_telemetry(tel, seq_holder, device_id, args.rate, duration, stats, None)
                finally:
                    await tel.close()
                if is_last_cycle:
                    break
                print(f"  [drop {cycle + 1}/{total_drops}] telemetry only closed "
                      f"(control still alive) — dark for {args.drop_for}s")
                await asyncio.sleep(args.drop_for)
                print(f"  [drop {cycle + 1}/{total_drops}] reconnecting telemetry")
        finally:
            stop_event.set()
            ctrl_task.cancel()
            await ctrl.close()
        print(f"\nsent={stats.sent} buffered={stats.buffered} replayed={stats.replayed}")
        return

    for cycle in range(total_drops + 1):
        ctrl = await websockets.connect(f"ws://{args.host}:8000/ws/control")
        await ctrl.send(build_device_register(device_id, args.role))
        print(f"  DeviceRegister sent (connect #{cycle + 1})")
        tel = await websockets.connect(f"ws://{args.host}:8000/ws/telemetry")

        stop_event = asyncio.Event()
        ctrl_task = asyncio.create_task(control_loop(ctrl, stop_event))

        for pkt in pending_replay:
            await tel.send(pkt)
            stats.replayed += 1
        if pending_replay:
            print(f"  replayed {len(pending_replay)} buffered packet(s)")
        pending_replay = []

        is_last_cycle = cycle == total_drops
        duration = None if is_last_cycle else args.drop_at
        if is_last_cycle:
            print(f"  Streaming at {args.rate} Hz. Press Ctrl+C to disconnect.\n")

        try:
            await _stream_telemetry(tel, seq_holder, device_id, args.rate, duration, stats, None)
        finally:
            stop_event.set()
            ctrl_task.cancel()
            await tel.close()
            await ctrl.close()

        if is_last_cycle:
            break

        print(f"  [drop {cycle + 1}/{total_drops}] both sockets closed — dark for {args.drop_for}s")
        if args.seq_restart:
            seq_holder[0] = 0
            print("  --seq-restart: sequence counter reset to 0")
        if args.buffer_while_dark:
            await _stream_telemetry(None, seq_holder, device_id, args.rate,
                                     args.drop_for, stats, pending_replay)
        else:
            await asyncio.sleep(args.drop_for)
        print(f"  [drop {cycle + 1}/{total_drops}] reconnecting")

    print(f"\nsent={stats.sent} buffered={stats.buffered} replayed={stats.replayed}")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="localhost", help="backend host")
    p.add_argument("--role", default="chest", help="device role")
    p.add_argument("--device-id", default=None, help="stable id across restarts (default: random sim-xxxx)")
    p.add_argument("--rate", type=float, default=100, help="Hz")
    p.add_argument("--drop-at", type=float, default=None, help="seconds after connect to kill both sockets")
    p.add_argument("--drop-for", type=float, default=20, help="seconds to stay dark before reconnecting")
    p.add_argument("--drops", type=int, default=1, help="how many times to repeat the drop cycle")
    p.add_argument("--buffer-while-dark", action="store_true",
                    help="keep generating packets while dark and replay them on reconnect")
    p.add_argument("--join-late", type=float, default=0, help="wait N seconds before connecting at all")
    p.add_argument("--seq-restart", action="store_true",
                    help="after a drop, restart sequence numbers at 0 (reproduces D5)")
    p.add_argument("--drop-telemetry-only", action="store_true",
                    help="only drop the telemetry socket, keep control/PING alive (reproduces D15)")
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(run(_parse_args()))
    except KeyboardInterrupt:
        print("\nSimulator disconnected.")
