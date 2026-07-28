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
            self._devices[device_id].packets_received += 1

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

    # ── Offline monitor ──────────────────────────────────────────────────────

    async def _monitor_offline(self) -> None:
        from .ws_handler import broadcast_to_frontends, _state_snapshot
        while self.state == SessionState.RECORDING:
            await asyncio.sleep(1)
            changed = False
            for dev in self._devices.values():
                was_online = dev.is_online
                dev.is_online = dev.is_alive
                if was_online and not dev.is_online:
                    changed = True
                    await audit.log(
                        "WARN",
                        "device_offline",
                        {"device_id": dev.device_id, "role": dev.device_role},
                    )
                    _open_offline_interval(dev, "ping_timeout")
            if changed:
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
