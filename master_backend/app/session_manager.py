"""
Session state machine + per-device tracking (CLAUDE.md §6, §22).
State: IDLE → PREFLIGHT → READY → RECORDING → FINALIZING → VALIDATING → IDLE
"""
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from fastapi import WebSocket

from master_backend.proto.commands import Command, CommandType

from .audit_logger import audit
from .dedup_store import dedup
from .io_manager import io_manager
from .integrity_validator import IntegrityValidator

logger = logging.getLogger(__name__)

_DEVICE_OFFLINE_SEC = 8.0   # matches Flutter _pongTimeoutSec in websocket_client.dart
_TRUE_HZ_WINDOW = 5         # seconds of true_hz history averaged for the preflight gate
_COORDINATED_START_LEAD_MS = 500   # ms ahead of now for scheduled_start


def _open_offline_interval(dev: "DeviceInfo", source: str) -> None:
    """Open an offline interval unless one is already open.

    A single physical drop used to append up to three intervals (unregister_device +
    _monitor_offline + note_telemetry_disconnect), inflating the '⚠ N gap(s)' badge and
    the integrity report (plan D6).
    """
    if dev.offline_intervals and dev.offline_intervals[-1]["end_ms"] is None:
        return
    dev.offline_intervals.append(
        {"start_ms": int(time.time() * 1000), "end_ms": None, "source": source}
    )


class SessionState(str, Enum):
    IDLE = "IDLE"
    PREFLIGHT = "PREFLIGHT"
    READY = "READY"
    RECORDING = "RECORDING"
    FINALIZING = "FINALIZING"
    VALIDATING = "VALIDATING"
    ERROR = "ERROR"


class DeviceSubstate(str, Enum):
    CONNECTED = "CONNECTED"
    RECORDING = "RECORDING"
    FINALIZED = "FINALIZED"
    DISCONNECTED = "DISCONNECTED"


@dataclass
class DeviceInfo:
    device_id: str
    device_role: str
    device_model: str
    app_version: str
    control_ws: WebSocket | None = None
    last_ping_ms: float = field(default_factory=time.monotonic)
    is_online: bool = False
    packets_received: int = 0
    substate: DeviceSubstate = DeviceSubstate.CONNECTED
    first_packet_ts: int | None = None      # epoch ms of first packet (for start drift)
    offline_intervals: list = field(default_factory=list)  # [{start_ms, end_ms}]
    last_packet_at: float = 0.0             # time.monotonic() of the last accepted packet
    _packets_prev_tick: int = 0
    rate_hz: float = 0.0
    last_acc: tuple | None = None        # last (acc_x, acc_y, acc_z) seen
    acc_changes: int = 0                 # cumulative count of DISTINCT acc readings
    _acc_changes_prev_tick: int = 0
    true_hz: float = 0.0                 # distinct acc readings in the LAST 1 s tick
    held_pct: float = 0.0                # % of packets in the last tick that were repeats
    # Rolling window of recent true_hz ticks. The instantaneous figure is far too noisy to
    # gate on — a single 1 s bucket swings 79→100→84 on a healthy device because tick
    # boundaries slice packet arrival unevenly. Preflight reads the smoothed value; the
    # device card shows the instantaneous one.
    _true_hz_window: list = field(default_factory=list)
    true_hz_avg: float = 0.0

    @property
    def is_alive(self) -> bool:
        return self.is_online and (time.monotonic() - self.last_ping_ms) < _DEVICE_OFFLINE_SEC


class SessionManager:
    def __init__(self) -> None:
        self.state: SessionState = SessionState.IDLE
        self.session_id: str = ""
        self.subject_name: str = ""
        self.session_tag: str = ""
        self.operator: str = ""
        self.scheduled_start_ms: int = 0
        self._devices: dict[str, DeviceInfo] = {}
        self._state_path = Path(os.getenv("SSD_PATH", "./data")) / ".sessions"
        self._offline_check_task: asyncio.Task | None = None

    # ── Device registry ──────────────────────────────────────────────────────

    def register_device(
        self,
        device_id: str,
        role: str,
        model: str,
        app_version: str,
        ws: WebSocket,
    ) -> str | None:
        """Register device. Returns error string if role collision, else None."""
        # Role uniqueness — reject duplicate roles (CLAUDE.md §22.1)
        for existing in self._devices.values():
            if (
                existing.device_role == role
                and existing.control_ws is not None
                and existing.device_id != device_id
                and not role.startswith("custom:")
            ):
                return f"Role '{role}' already taken by device {existing.device_id[:8]}"

        # Preserve session-level data if device reconnects mid-session.
        existing = self._devices.get(device_id)
        preserved_intervals = existing.offline_intervals if existing else []
        preserved_first_ts = existing.first_packet_ts if existing else None
        preserved_packets = existing.packets_received if existing else 0
        preserved_last_acc = existing.last_acc if existing else None
        preserved_acc_changes = existing.acc_changes if existing else 0
        preserved_acc_prev = existing._acc_changes_prev_tick if existing else 0
        preserved_true_hz = existing.true_hz if existing else 0.0
        preserved_held_pct = existing.held_pct if existing else 0.0
        preserved_true_avg = existing.true_hz_avg if existing else 0.0
        preserved_true_window = list(existing._true_hz_window) if existing else []

        self._devices[device_id] = DeviceInfo(
            device_id=device_id,
            device_role=role,
            device_model=model,
            app_version=app_version,
            control_ws=ws,
            last_ping_ms=time.monotonic(),
            is_online=True,
            offline_intervals=preserved_intervals,
            first_packet_ts=preserved_first_ts,
            packets_received=preserved_packets,
            last_acc=preserved_last_acc,
            acc_changes=preserved_acc_changes,
            _acc_changes_prev_tick=preserved_acc_prev,
            true_hz=preserved_true_hz,
            held_pct=preserved_held_pct,
            true_hz_avg=preserved_true_avg,
            _true_hz_window=preserved_true_window,
        )
        logger.info("Device registered: %s role=%s", device_id[:8], role)
        return None

    def unregister_device(self, device_id: str | None) -> None:
        if device_id and device_id in self._devices:
            dev = self._devices[device_id]
            dev.is_online = False
            dev.control_ws = None
            dev.substate = DeviceSubstate.DISCONNECTED
            # Record offline interval if session was recording
            if self.state == SessionState.RECORDING:
                _open_offline_interval(dev, "control_disconnect")

    def note_telemetry_disconnect(self, device_id: str) -> None:
        """Record a telemetry-channel drop for the integrity report.
        Does NOT touch control_ws — device lifecycle belongs to the control channel only."""
        if device_id not in self._devices:
            return
        dev = self._devices[device_id]
        if self.state == SessionState.RECORDING:
            _open_offline_interval(dev, "telemetry_disconnect")

    def mark_ping(self, device_id: str) -> None:
        if device_id in self._devices:
            dev = self._devices[device_id]
            # Close any open offline interval on reconnect
            if dev.offline_intervals and dev.offline_intervals[-1]["end_ms"] is None:
                dev.offline_intervals[-1]["end_ms"] = int(time.time() * 1000)
            dev.last_ping_ms = time.monotonic()
            dev.is_online = True

    def mark_first_packet(self, device_id: str, timestamp_ms: int) -> None:
        if device_id in self._devices:
            dev = self._devices[device_id]
            if dev.first_packet_ts is None:
                dev.first_packet_ts = timestamp_ms
                dev.substate = DeviceSubstate.RECORDING

    def increment_packets(self, device_id: str) -> None:
        if device_id in self._devices:
            dev = self._devices[device_id]
            dev.packets_received += 1
            dev.last_packet_at = time.monotonic()

    def note_sample(self, device_id: str, acc: tuple) -> None:
        """Count DISTINCT accelerometer readings. A repeated triple is a held sample:
        the OS re-delivered a stale hardware reading to satisfy the requested rate."""
        dev = self._devices.get(device_id)
        if dev is None:
            return
        if acc != dev.last_acc:
            dev.acc_changes += 1
        dev.last_acc = acc

    def get_device(self, device_id: str) -> DeviceInfo | None:
        return self._devices.get(device_id)

    @property
    def online_devices(self) -> list[DeviceInfo]:
        return [d for d in self._devices.values() if d.control_ws is not None]

    @property
    def connected_roles(self) -> list[str]:
        return [d.device_role for d in self.online_devices]

    # ── Quorum check ─────────────────────────────────────────────────────────

    def quorum_ok(self) -> tuple[bool, str]:
        """Returns (ok, reason). True if at least 1 device connected."""
        connected = self.online_devices
        if not connected:
            return False, "No devices connected"
        return True, f"{len(connected)} device(s) ready"

    # ── State transitions ────────────────────────────────────────────────────

    async def to_preflight(self) -> None:
        await self._transition(SessionState.PREFLIGHT)

    async def to_ready(self) -> None:
        await self._transition(SessionState.READY)

    async def start_recording(self, payload: dict) -> tuple[bool, str]:
        """Returns (ok, reason_or_session_id)."""
        if self.state not in (SessionState.PREFLIGHT, SessionState.READY, SessionState.IDLE):
            return False, f"Invalid state: {self.state}"

        ok, reason = self.quorum_ok()
        if not ok:
            return False, reason

        self.session_id = str(int(time.time() * 1000))
        self.subject_name = payload.get("subject_name", "Unknown")
        self.session_tag = payload.get("session_tag", "Session")
        self.operator = payload.get("operator", "Unknown")

        # Coordinated start: all devices start at the same ms (CLAUDE.md §22.5)
        self.scheduled_start_ms = int(time.time() * 1000) + _COORDINATED_START_LEAD_MS

        device_roles = {d.device_id: d.device_role for d in self.online_devices}
        await io_manager.open_session(
            session_id=self.session_id,
            subject_name=self.subject_name,
            session_tag=self.session_tag,
            operator=self.operator,
            device_roles=device_roles,
        )
        dedup.clear()

        # Reset per-device session state
        for dev in self._devices.values():
            dev.first_packet_ts = None
            dev.offline_intervals = []
            dev.packets_received = 0
            dev.last_acc = None
            dev.acc_changes = 0
            dev._acc_changes_prev_tick = 0
            dev.true_hz = 0.0
            dev.held_pct = 0.0
            # Keep the smoothed rate across the START boundary: it was measured seconds ago
            # on the same hardware and is what preflight just approved. Zeroing it would make
            # every device look broken for the first 5 s of every recording.

        await self._transition(SessionState.RECORDING)
        await self._save_state()
        self._offline_check_task = asyncio.create_task(self._monitor_offline())
        return True, self.session_id

    async def stop_recording(self, reason: str = "operator_stop") -> dict:
        if self.state != SessionState.RECORDING:
            return {}

        # Notify mobile nodes before closing files so they exit recording state
        # while their control_ws handles are still live.
        stop_cmd = Command(
            type=CommandType.STOP_SESSION,
            payload=json.dumps({"reason": reason}),
            issued_at_ms=int(time.time() * 1000),
        ).to_bytes()
        await self.broadcast_control(stop_cmd)

        await self._transition(SessionState.FINALIZING)
        if self._offline_check_task:
            self._offline_check_task.cancel()

        # Close any open offline intervals
        for dev in self._devices.values():
            if dev.offline_intervals and dev.offline_intervals[-1]["end_ms"] is None:
                dev.offline_intervals[-1]["end_ms"] = int(time.time() * 1000)
            dev.substate = DeviceSubstate.FINALIZED

        file_results = await io_manager.close_session()
        await audit.log("INFO", "session_finalizing", {"reason": reason, "files": file_results})

        await self._transition(SessionState.VALIDATING)
        report = await IntegrityValidator().run(
            session_id=self.session_id,
            file_results=file_results,
            devices=list(self._devices.values()),
            scheduled_start_ms=self.scheduled_start_ms,
        )
        await audit.log("INFO", "validation_complete", {"status": report.get("status")})

        await self._transition(SessionState.IDLE)
        await self._clear_state()
        dedup.clear()

        # Re-assert state for anyone who reconnected during finalisation (plan D1).
        from .ws_handler import _state_pong
        await self.broadcast_control(_state_pong())
        return report

    async def abort(self, reason: str = "error") -> None:
        await audit.log("ERROR", "session_aborted", {"reason": reason})
        if self.state == SessionState.RECORDING:
            await io_manager.close_session()
        await self._transition(SessionState.ERROR)
        dedup.clear()

    async def _transition(self, new_state: SessionState) -> None:
        old = self.state
        self.state = new_state
        await audit.log(
            "INFO",
            "state_transition",
            {"from": old, "to": new_state, "session_id": self.session_id},
        )

    # ── Broadcast helpers ────────────────────────────────────────────────────

    async def broadcast_control(self, data: bytes) -> None:
        """Send to every device whose control socket is still open.

        `is_online` is a liveness HEURISTIC (pinged within _DEVICE_OFFLINE_SEC), not
        socket state. Gating on it meant STOP_SESSION was withheld from phones whose
        socket was perfectly alive but whose last PING was 9 s old — the phone then
        stayed stuck in RECORDING forever (plan D1).
        """
        for device in self._devices.values():
            if device.control_ws is None:
                continue
            try:
                await device.control_ws.send_bytes(data)
            except Exception:
                device.is_online = False

    async def send_to_device(self, device_id: str, data: bytes) -> None:
        dev = self._devices.get(device_id)
        if dev and dev.control_ws:
            try:
                await dev.control_ws.send_bytes(data)
            except Exception:
                dev.is_online = False

    async def reset_all(self) -> None:
        """Operator reset: close every device control connection, then drop all
        per-device state (offline intervals / gap badges, packet counts, cached
        samples) so stale cards and connectivity warnings clear without a backend
        restart. Only meaningful while NOT recording (guarded by the caller)."""
        for dev in self._devices.values():
            if dev.control_ws is not None:
                try:
                    await dev.control_ws.close(code=4000, reason="operator_reset")
                except Exception:
                    pass
        self._devices.clear()
        self.session_id = ""
        self.subject_name = ""
        self.session_tag = ""
        self.operator = ""
        self.scheduled_start_ms = 0
        if self.state != SessionState.IDLE:
            await self._transition(SessionState.IDLE)
        dedup.clear()
        await self._clear_state()

    # ── Persistence ──────────────────────────────────────────────────────────

    async def _save_state(self) -> None:
        self._state_path.mkdir(parents=True, exist_ok=True)
        state_file = self._state_path / f"{self.session_id}.state.json"
        data = {
            "session_id": self.session_id,
            "state": self.state,
            "subject_name": self.subject_name,
            "session_tag": self.session_tag,
            "operator": self.operator,
            "scheduled_start_ms": self.scheduled_start_ms,
            "devices": [
                {"device_id": d.device_id, "role": d.device_role}
                for d in self._devices.values()
            ],
            "saved_at_ms": int(time.time() * 1000),
        }
        state_file.write_text(json.dumps(data, indent=2))

    async def _clear_state(self) -> None:
        state_file = self._state_path / f"{self.session_id}.state.json"
        if state_file.exists():
            data = json.loads(state_file.read_text())
            data["state"] = "IDLE"
            state_file.write_text(json.dumps(data, indent=2))

    def get_interrupted_sessions(self) -> list[dict]:
        if not self._state_path.exists():
            return []
        results = []
        for f in self._state_path.glob("*.state.json"):
            try:
                data = json.loads(f.read_text())
                if data.get("state") in ("RECORDING", "FINALIZING"):
                    results.append(data)
            except Exception:
                pass
        return results

    @staticmethod
    def _tick_rates(dev: "DeviceInfo") -> None:
        """Roll one second of per-device rate counters.

        `rate_hz` counts PACKETS; `true_hz` counts DISTINCT accelerometer readings. They
        diverge exactly when the OS re-delivers a stale hardware sample to satisfy the
        requested rate — the failure that silently halved two devices on 2026-08-07.

        Must be driven in every session state, not just RECORDING: preflight gates START on
        true_hz while the session is IDLE, so a counter that only advanced during RECORDING
        would leave every device pinned at 0 Hz and block recording outright.
        """
        dev.rate_hz = float(dev.packets_received - dev._packets_prev_tick)
        dev._packets_prev_tick = dev.packets_received
        dev.true_hz = float(dev.acc_changes - dev._acc_changes_prev_tick)
        dev.held_pct = (
            100.0 * (1 - dev.true_hz / dev.rate_hz) if dev.rate_hz > 0 else 0.0
        )
        dev._acc_changes_prev_tick = dev.acc_changes

        # Smooth only while the device is actually streaming. Folding idle zeros into the
        # window would drag the average down for seconds after a device starts, which reads
        # as a failing sensor rather than one that has just connected.
        if dev.rate_hz > 0:
            dev._true_hz_window.append(dev.true_hz)
            del dev._true_hz_window[:-_TRUE_HZ_WINDOW]
            dev.true_hz_avg = sum(dev._true_hz_window) / len(dev._true_hz_window)
        elif not dev._true_hz_window:
            dev.true_hz_avg = 0.0

    # ── Offline monitor ──────────────────────────────────────────────────────

    async def _monitor_offline(self) -> None:
        from .ws_handler import broadcast_to_frontends, _state_snapshot
        while self.state == SessionState.RECORDING:
            await asyncio.sleep(1)
            for dev in self._devices.values():
                was_online = dev.is_online
                dev.is_online = dev.is_alive
                self._tick_rates(dev)
                if was_online and not dev.is_online:
                    await audit.log(
                        "WARN",
                        "device_offline",
                        {"device_id": dev.device_id, "role": dev.device_role},
                    )
                    _open_offline_interval(dev, "ping_timeout")
            # Broadcast unconditionally: the packet counters, the rate, and the substate
            # all change every second, and nothing else pushes them. Without this the
            # dashboard shows "0 pkts" and no "live" badge for the entire recording,
            # which is a large part of why a healthy session looks like nothing is
            # happening (plan D17).
            await broadcast_to_frontends(_state_snapshot())

    # ── Idle reaper ──────────────────────────────────────────────────────────

    async def run_idle_reaper(self) -> None:
        """Permanent background loop. While the session is IDLE, drop any device whose
        control channel is gone (clean disconnect) or that stopped pinging (silent
        network death), so stale/offline cards clear from the dashboard WITHOUT a
        backend restart. Never prunes during RECORDING/FINALIZING/VALIDATING — those
        states need the full device set for offline-interval tracking and the
        integrity report. Mutually exclusive with _monitor_offline (gated on state),
        so the RECORDING path is untouched."""
        from .ws_handler import broadcast_to_frontends, _state_snapshot, drop_latest_sample
        while True:
            await asyncio.sleep(1)

            late_summary = await io_manager.finalize_late()
            if late_summary:
                await broadcast_to_frontends({"type": "LATE_DELIVERY", **late_summary})

            # Keep the rate counters live outside RECORDING. Phones stream telemetry as soon
            # as they connect, and preflight blocks START until it sees a healthy true_hz —
            # so without this every device sits at 0 Hz while IDLE and START is never
            # permitted. Skipped during RECORDING, where _monitor_offline owns the tick;
            # running both would consume the same delta twice and halve both rates.
            if self.state != SessionState.RECORDING and self._devices:
                for dev in self._devices.values():
                    dev.is_online = dev.is_alive
                    self._tick_rates(dev)
                await broadcast_to_frontends(_state_snapshot())

            if self.state != SessionState.IDLE:
                continue
            dead = [
                device_id
                for device_id, dev in self._devices.items()
                if dev.control_ws is None or not dev.is_alive
            ]
            if not dead:
                continue
            for device_id in dead:
                self._devices.pop(device_id, None)
                drop_latest_sample(device_id)
                await audit.log("INFO", "device_pruned_idle", {"device_id": device_id})
            await broadcast_to_frontends(_state_snapshot())


session_manager = SessionManager()
