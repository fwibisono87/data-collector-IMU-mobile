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
from .finalize_job import FinalizeContext, FinalizeJob
from .io_manager import io_manager
from .session_ledger import SessionLedger

logger = logging.getLogger(__name__)


class IllegalTransition(RuntimeError):
    """A lifecycle command was issued from a state that does not permit it.

    Raised rather than silently returning an empty result: a repeated STOP used to be
    acknowledged as success and broadcast an empty integrity report, which overwrote the
    real one on every connected dashboard.
    """

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


def _open_telemetry_gap(dev: "DeviceInfo", source: str = "telemetry_disconnect") -> None:
    """Track a telemetry-channel gap independently from control/offline liveness."""
    if dev.telemetry_gaps and dev.telemetry_gaps[-1]["end_ms"] is None:
        return
    dev.telemetry_gaps.append(
        {"start_ms": int(time.time() * 1000), "end_ms": None, "source": source}
    )


def _close_telemetry_gap(dev: "DeviceInfo") -> None:
    if dev.telemetry_gaps and dev.telemetry_gaps[-1]["end_ms"] is None:
        dev.telemetry_gaps[-1]["end_ms"] = int(time.time() * 1000)


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
    telemetry_gaps: list = field(default_factory=list)  # telemetry-only gaps
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
        self._ledger = SessionLedger(self._state_path)
        self._offline_check_task: asyncio.Task | None = None
        self._recording_started_at: float = 0.0
        self._recording_started_epoch_ms: int = 0
        self._preflight_failed: list[str] = []
        self._stop_reason: str = ""
        # Lifecycle observers (the WebSocket layer registers itself). Every transition and
        # every finalize step is published here, so no component has to poll and no slow
        # operation is invisible.
        self._observers: list = []
        self._finalize_job: FinalizeJob | None = None
        self._finalize_task: asyncio.Task | None = None
        self._last_report: dict = {}

    # ── Lifecycle observation ────────────────────────────────────────────────

    def add_observer(self, callback) -> None:
        """Register an async callback invoked with every lifecycle event."""
        self._observers.append(callback)

    async def _notify(self, event: dict) -> None:
        for callback in list(self._observers):
            try:
                await callback(event)
            except Exception as exc:
                # An observer is a reporting channel. It must never be able to fail the
                # operation it is reporting on.
                logger.debug("lifecycle observer failed: %s", exc)

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
        preserved_telemetry_gaps = list(existing.telemetry_gaps) if existing else []
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
            telemetry_gaps=preserved_telemetry_gaps,
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
            _open_telemetry_gap(dev)

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
            _close_telemetry_gap(dev)

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

        # Which preflight checks were red when the operator pressed START. A failing check
        # warns but does not block (a field session must never be stranded by a judgement
        # call), so the session has to carry its own provenance: the audit log records it,
        # and open_session stamps it into the CSV metadata line. A half-rate recording is
        # then self-documenting instead of looking indistinguishable from a clean one.
        preflight_failed = [str(x) for x in (payload.get("preflight_failed") or [])]
        self._preflight_failed = preflight_failed
        if preflight_failed:
            await audit.log("WARN", "preflight_failed_at_start", {
                "session_id": self.session_id,
                "failed_checks": preflight_failed,
            })

        # Coordinated start: all devices start at the same ms (CLAUDE.md §22.5)
        self.scheduled_start_ms = int(time.time() * 1000) + _COORDINATED_START_LEAD_MS
        self._recording_started_epoch_ms = int(time.time() * 1000)

        device_roles = {d.device_id: d.device_role for d in self.online_devices}
        # Preflight's smoothed measurement names the file. It is provisional — close_session
        # re-tiers from the session-wide average — but it means a file is never unlabelled,
        # not even if the backend dies mid-session.
        device_rates = {d.device_id: d.true_hz_avg for d in self.online_devices}
        # Persist the identity before opening any writer. If the process dies during
        # startup, the next dashboard can still discover a session was attempted and
        # inspect the exact metadata rather than seeing a blank IDLE state.
        await self._save_state()
        try:
            await io_manager.open_session(
                session_id=self.session_id,
                subject_name=self.subject_name,
                session_tag=self.session_tag,
                operator=self.operator,
                device_roles=device_roles,
                device_rates=device_rates,
                preflight_failed=preflight_failed,
            )
        except Exception as exc:
            # open_session can fail after opening only a subset of device writers. Close
            # whatever was acquired so a failed START cannot leak file descriptors or leave
            # a half-open writer that contaminates the next session.
            try:
                await io_manager.close_session()
            except Exception as close_exc:
                await audit.log("ERROR", "startup_cleanup_failed", {
                    "session_id": self.session_id,
                    "error": str(close_exc),
                })
            await self._transition(SessionState.ERROR)
            await self._save_state(reason=f"open_session_failed: {exc}")
            raise
        self._recording_started_at = time.monotonic()
        self._recording_started_epoch_ms = int(time.time() * 1000)
        dedup.clear()

        # Reset per-device session state
        for dev in self._devices.values():
            dev.first_packet_ts = None
            dev.offline_intervals = []
            dev.telemetry_gaps = []
            dev.packets_received = 0
            dev.last_packet_at = 0.0
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

    async def request_stop(self, reason: str = "operator_stop") -> str:
        """Begin finalization and return immediately.

        Everything slow — closing writers, re-sorting, hashing, validating, bundling —
        runs in a background job whose progress is broadcast to every dashboard. The
        command that triggered this is acknowledged in milliseconds, so a long finalize
        can no longer be mistaken for a hung backend, and a client retry can no longer
        re-enter finalization.

        Raises IllegalTransition if the session is not RECORDING.
        """
        if self.state != SessionState.RECORDING:
            raise IllegalTransition(
                f"cannot stop from {self.state.value}: only a RECORDING session can be stopped"
            )

        self._stop_reason = reason
        # Arm late delivery before STOP reaches phones. Once we transition out of
        # RECORDING, a reconnecting phone may immediately flush its buffered tail.
        io_manager.arm_late_window()
        await self._transition(SessionState.FINALIZING)

        # Notify mobile nodes while their control sockets are still live.
        stop_cmd = Command(
            type=CommandType.STOP_SESSION,
            payload=json.dumps({"reason": reason}),
            issued_at_ms=int(time.time() * 1000),
        ).to_bytes()
        await self.broadcast_control(stop_cmd)
        if self._offline_check_task:
            self._offline_check_task.cancel()
            self._offline_check_task = None

        # Close any open offline and telemetry intervals.
        for dev in self._devices.values():
            if dev.offline_intervals and dev.offline_intervals[-1]["end_ms"] is None:
                dev.offline_intervals[-1]["end_ms"] = int(time.time() * 1000)
            _close_telemetry_gap(dev)
            dev.substate = DeviceSubstate.FINALIZED

        await self._save_state()
        await audit.log("INFO", "session_finalizing", {"reason": reason})
        self._start_finalize_job(reason)
        return self.session_id

    def _start_finalize_job(
        self,
        reason: str,
        *,
        steps: dict | None = None,
        collect_artifacts=None,
    ) -> FinalizeJob:
        ctx = FinalizeContext(
            session_id=self.session_id,
            reason=reason,
            scheduled_start_ms=self.scheduled_start_ms,
            session_start_ms=self._recording_started_epoch_ms,
            session_end_ms=int(time.time() * 1000),
            devices=list(self._devices.values()),
            label_timeline=io_manager.label_timeline,
            true_hz=self._session_true_hz(),
            collect_artifacts=collect_artifacts,
            persist=self._persist_finalize_progress,
            notify=self._notify,
            enter_state=self._enter_state_by_name,
        )
        job = FinalizeJob(ctx, steps)
        self._finalize_job = job
        self._finalize_task = asyncio.create_task(
            self._run_finalize(job), name=f"finalize-{self.session_id}"
        )
        return job

    async def _enter_state_by_name(self, name: str) -> None:
        await self._transition(SessionState(name))

    async def _persist_finalize_progress(self, job: FinalizeJob) -> None:
        """Checkpoint step progress so a restart resumes instead of restarting."""
        if not self.session_id:
            return
        data = self._ledger.read(self.session_id) or {"session_id": self.session_id}
        data["finalize"] = job.snapshot()
        data["updated_at_ms"] = int(time.time() * 1000)
        self._ledger.write(self.session_id, data)

    async def _run_finalize(self, job: FinalizeJob) -> dict:
        """Drive one finalize job to a terminal ledger record, whatever happens inside."""
        report: dict = {}
        try:
            report = await job.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # FinalizeJob.run isolates each step, so reaching here means the runner
            # itself broke. Still produce a terminal record rather than leaving the
            # dashboard waiting forever.
            logger.error("finalize runner crashed: %s", exc, exc_info=True)
            await audit.log("ERROR", "finalize_runner_crashed", {
                "session_id": self.session_id, "error": str(exc),
            })
        finally:
            if not report:
                report = {
                    "session_id": self.session_id,
                    "status": "FAIL",
                    "analysis_ready": False,
                    "analysis_ready_reasons": [
                        f"finalization did not produce a report (failed steps: "
                        f"{', '.join(job.failed_steps) or 'unknown'})"
                    ],
                    "devices": [],
                    "cross_device_checks": {},
                }
            self._last_report = report
            await self._transition(SessionState.IDLE)
            try:
                await self._save_state(
                    terminal=True,
                    report=report,
                    file_results=job.ctx.file_results,
                    reason=job.ctx.reason,
                )
            except Exception as exc:
                # A ledger write failure must not turn successfully closed and validated
                # CSVs into a failed session. The report file and the data on disk remain
                # authoritative; the next startup rediscovers them from the folder.
                await audit.log("ERROR", "terminal_ledger_write_failed", {
                    "session_id": self.session_id, "error": str(exc),
                })
            await self._notify({
                "type": "FINALIZE_COMPLETE",
                "session_id": self.session_id,
                "integrity_report": report,
                "finalize": job.snapshot(),
            })
        return report

    async def wait_for_finalize(self) -> None:
        """Block until the in-flight finalize job settles. Used by tests and shutdown."""
        task = self._finalize_task
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)

    @property
    def finalize_snapshot(self) -> dict | None:
        return self._finalize_job.snapshot() if self._finalize_job else None

    async def stop_recording(self, reason: str = "operator_stop") -> dict:
        """Stop and wait for finalization to complete, returning the integrity report.

        Kept for callers that genuinely need the synchronous shape — process shutdown and
        the test suite. Interactive callers should use request_stop and follow the
        broadcast progress instead of holding a socket open across the whole job.
        """
        try:
            await self.request_stop(reason)
        except IllegalTransition:
            return {}
        await self.wait_for_finalize()
        return self._last_report or {}
    async def abort(self, reason: str = "error") -> None:
        await audit.log("ERROR", "session_aborted", {"reason": reason})
        # Close unconditionally rather than only from RECORDING. Aborting out of
        # FINALIZING/VALIDATING used to leave the writers open, and because open_session
        # never cleared them the stale handles were carried into the next session.
        if io_manager.session_open:
            await io_manager.close_session()
        await self._transition(SessionState.ERROR)
        dedup.clear()

    async def _transition(self, new_state: SessionState) -> None:
        """Advance the lifecycle: record it durably, then tell everyone.

        The broadcast is unconditional and lives here rather than at the call sites, so a
        state can no longer exist only in this process. FINALIZING and VALIDATING used to
        be written to the audit log and nowhere else, which is why the dashboard sat on
        RECORDING for the whole of finalization with no way to tell a slow close from a
        hang.
        """
        old = self.state
        self.state = new_state
        await audit.log(
            "INFO",
            "state_transition",
            {"from": old, "to": new_state, "session_id": self.session_id},
        )
        if self.session_id:
            try:
                await self._persist_state()
            except Exception as exc:
                logger.warning("state checkpoint failed at %s: %s", new_state, exc)
        await self._notify({
            "type": "STATE_TRANSITION",
            "from": old.value if isinstance(old, SessionState) else str(old),
            "to": new_state.value,
            "session_id": self.session_id,
        })

    # ── Broadcast helpers ────────────────────────────────────────────────────

    async def broadcast_control(self, data: bytes) -> None:
        """Send to every device whose control socket is still open.

        `is_online` is a liveness HEURISTIC (pinged within _DEVICE_OFFLINE_SEC), not
        socket state. Gating on it meant STOP_SESSION was withheld from phones whose
        socket was perfectly alive but whose last PING was 9 s old — the phone then
        stayed stuck in RECORDING forever (plan D1).
        """
        async def send(device: DeviceInfo) -> None:
            if device.control_ws is None:
                return
            try:
                await asyncio.wait_for(device.control_ws.send_bytes(data), timeout=2.0)
            except Exception:
                device.is_online = False
        await asyncio.gather(*(send(device) for device in self._devices.values()))

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
        previous_session_id = self.session_id
        self._devices.clear()
        self.session_id = ""
        self.subject_name = ""
        self.session_tag = ""
        self.operator = ""
        self.scheduled_start_ms = 0
        if self.state != SessionState.IDLE:
            await self._transition(SessionState.IDLE)
        dedup.clear()
        if previous_session_id:
            data = self._ledger.read(previous_session_id) or {"session_id": previous_session_id}
            data["state"] = SessionState.IDLE.value
            data["reset_at_ms"] = int(time.time() * 1000)
            self._ledger.write(previous_session_id, data)

    # ── Persistence ──────────────────────────────────────────────────────────

    async def _save_state(
        self,
        *,
        terminal: bool = False,
        report: dict | None = None,
        file_results: dict | None = None,
        reason: str | None = None,
    ) -> None:
        await self._persist_state(
            terminal=terminal,
            report=report,
            file_results=file_results,
            reason=reason,
        )

    async def _persist_state(
        self,
        *,
        terminal: bool = False,
        report: dict | None = None,
        file_results: dict | None = None,
        reason: str | None = None,
    ) -> None:
        if not self.session_id:
            return
        existing = self._ledger.read(self.session_id) or {}
        data = {
            **existing,
            "session_id": self.session_id,
            "state": self.state.value,
            "subject_name": self.subject_name,
            "session_tag": self.session_tag,
            "operator": self.operator,
            "scheduled_start_ms": self.scheduled_start_ms,
            "recording_started_ms": self._recording_started_epoch_ms,
            # This remains true during the narrow START/open_session window. If the backend
            # dies before it can publish RECORDING, the next dashboard must still surface the
            # attempted session instead of treating the ledger as an ordinary idle snapshot.
            "startup_in_progress": (
                not terminal
                and self.state in (SessionState.IDLE, SessionState.PREFLIGHT, SessionState.READY)
                and bool(self.session_id)
            ),
            "preflight_failed": self._preflight_failed,
            "stop_reason": reason or self._stop_reason or existing.get("stop_reason", ""),
            "updated_at_ms": int(time.time() * 1000),
            "devices": [
                {
                    "device_id": d.device_id,
                    "role": d.device_role,
                    "model": d.device_model,
                    "app_version": d.app_version,
                    "packets": d.packets_received,
                    "first_packet_ts": d.first_packet_ts,
                    "offline_intervals": d.offline_intervals,
                    "substate": d.substate.value,
                }
                for d in self._devices.values()
            ],
            "label_timeline": (
                io_manager.label_timeline
                if io_manager.session_id == self.session_id
                else existing.get("label_timeline", [])
            ),
        }
        if report is not None:
            data["integrity_report"] = report
        if file_results is not None:
            data["file_results"] = file_results
        if terminal:
            data["terminal"] = True
            data["finalized_at_ms"] = int(time.time() * 1000)
        self._ledger.write(self.session_id, data)

    async def _clear_state(self) -> None:
        if not self.session_id:
            return
        data = self._ledger.read(self.session_id) or {}
        data["state"] = SessionState.IDLE.value
        data["startup_in_progress"] = False
        data["updated_at_ms"] = int(time.time() * 1000)
        self._ledger.write(self.session_id, data)

    def get_interrupted_sessions(self) -> list[dict]:
        return [
            data for data in self._ledger.list()
            if (
                data.get("startup_in_progress")
                or data.get("state") in ("RECORDING", "FINALIZING", "VALIDATING")
            )
            and not data.get("terminal", False)
            and not (
                data.get("startup_in_progress")
                and data.get("session_id") == self.session_id
                and self.state in (SessionState.IDLE, SessionState.PREFLIGHT, SessionState.READY)
            )
        ]

    def get_recovery_session(self, session_id: str) -> dict | None:
        return self._ledger.read(session_id)

    def list_recovery_sessions(self) -> list[dict]:
        return [
            data for data in self._ledger.list()
            if (
                data.get("terminal", False)
                or data.get("startup_in_progress", False)
                or data.get("state") in ("RECORDING", "FINALIZING", "VALIDATING", "ERROR")
            )
            and not (
                data.get("startup_in_progress")
                and data.get("session_id") == self.session_id
                and self.state in (SessionState.IDLE, SessionState.PREFLIGHT, SessionState.READY)
            )
        ]

    async def recover_interrupted_sessions(self) -> None:
        """Finalize sessions left non-terminal by a backend process death.

        The old startup check only logged the session id. That preserved the bytes but left
        the operator with no integrity verdict and no end-of-session workflow. Recovery is
        deliberately conservative: it merges every on-disk source available at startup,
        validates the resulting per-role files, and records a terminal ledger entry. Phone
        rescue uploads that arrive later remain visible as pending in the export manifest and
        can be consolidated again.
        """
        interrupted = self.get_interrupted_sessions()
        for record in interrupted:
            session_id = str(record.get("session_id", ""))
            if not session_id:
                continue

            async def collect(rec=record):
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._collect_recovery_artifacts, rec
                )

            async def persist(job, sid=session_id):
                data = self._ledger.read(sid) or {"session_id": sid}
                data["finalize"] = job.snapshot()
                data["updated_at_ms"] = int(time.time() * 1000)
                self._ledger.write(sid, data)

            ctx = FinalizeContext(
                session_id=session_id,
                reason="backend_restart_recovery",
                scheduled_start_ms=int(record.get("scheduled_start_ms", 0) or 0),
                session_start_ms=int(record.get("recording_started_ms", 0) or 0),
                session_end_ms=int(time.time() * 1000),
                label_timeline=list(record.get("label_timeline", [])),
                collect_artifacts=collect,
                persist=persist,
                notify=self._notify,
            )
            # Resume rather than restart. A finalize that got as far as closing and
            # validating before the process died does not redo that work, and — more
            # importantly — recovery now runs the *same* steps as a live stop instead of
            # a second implementation that could drift from it.
            saved_steps = {
                str(entry.get("step")): {
                    key: value for key, value in entry.items() if key != "step"
                }
                for entry in ((record.get("finalize") or {}).get("steps") or [])
                if entry.get("step")
            }
            job = FinalizeJob(ctx, saved_steps)
            try:
                report = await job.run()
                updated = self._ledger.read(session_id) or dict(record)
                updated.update({
                    "state": SessionState.IDLE.value,
                    "terminal": True,
                    "startup_in_progress": False,
                    "stop_reason": "backend_restart_recovery",
                    "finalized_at_ms": int(time.time() * 1000),
                    "integrity_report": report,
                    "file_results": ctx.file_results,
                    "finalize": job.snapshot(),
                    "recovered_after_backend_restart": True,
                })
                self._ledger.write(session_id, updated)
                await audit.log("WARN", "interrupted_session_recovered", {
                    "session_id": session_id,
                    "status": report.get("status"),
                    "analysis_ready": report.get("analysis_ready"),
                    "failed_steps": job.failed_steps,
                })
            except Exception as exc:
                # Keep it discoverable and non-terminal so a later startup/retry or the
                # operator's direct bundle link cannot mistake a failed recovery for success.
                failed = self._ledger.read(session_id) or dict(record)
                failed.update({
                    "state": SessionState.ERROR.value,
                    "recovery_error": str(exc),
                    "finalize": job.snapshot(),
                    "updated_at_ms": int(time.time() * 1000),
                })
                self._ledger.write(session_id, failed)
                await audit.log("ERROR", "interrupted_session_recovery_failed", {
                    "session_id": session_id, "error": str(exc),
                })

    @staticmethod
    def _collect_recovery_artifacts(record: dict) -> tuple[dict, list[DeviceInfo]]:
        """Merge and inventory interrupted-session files; blocking work runs in an executor."""
        from .csv_schema import is_valid_data_row, parse_row
        from .export import (
            _ORIGINAL_KINDS, _recovery_manifest, _recovery_role, _role_from_name,
            _session_files, _session_folders, _slug,
        )
        from .io_manager import _sha256
        from .upload import merge_csv_sources_per_role

        session_id = str(record.get("session_id", ""))
        recovery = _recovery_manifest(session_id)
        verified_recovery = [
            r for r in recovery
            if r.get("complete") and r.get("sha256_verified") and r.get("csv_exists")
        ]
        files = _session_files(session_id)
        sources: list[tuple[str, str, Path]] = []
        for f in files:
            if f["kind"] in _ORIGINAL_KINDS:
                role = _slug(_role_from_name(f["name"], session_id)) or "unknown"
                sources.append((role, f["kind"], Path(f["path"])))
        for r in verified_recovery:
            sources.append((_slug(_recovery_role(r)), "recovery", Path(r["csv_path"])))

        folders = _session_folders(session_id)
        if folders:
            output = folders[0]
        elif verified_recovery:
            first = verified_recovery[0]
            subject = str(first.get("subject") or "Unknown").replace(" ", "_")
            tag = str(first.get("session_tag") or "Session").replace(" ", "_")
            output = Path(os.getenv("SSD_PATH", "./data")) / "Data_Riset_IMU" / f"{subject}_{tag}"
        else:
            output = Path(os.getenv("SSD_PATH", "./data")) / "Data_Riset_IMU" / "_recovered" / session_id

        per_role = merge_csv_sources_per_role(
            sources,
            output,
            session_id,
            metadata_prefix=f"session_id={session_id},source=backend_restart_recovery",
        ) if sources else {"per_role": {}}

        file_results: dict[str, dict] = {}
        devices: list[DeviceInfo] = []
        for raw in record.get("devices", []):
            device_id = str(raw.get("device_id", ""))
            role = str(raw.get("role", "unknown"))
            dev = DeviceInfo(
                device_id=device_id,
                device_role=role,
                device_model=str(raw.get("model", "")),
                app_version=str(raw.get("app_version", "")),
                is_online=False,
                packets_received=int(raw.get("packets", 0) or 0),
                first_packet_ts=raw.get("first_packet_ts"),
                offline_intervals=list(raw.get("offline_intervals", [])),
                substate=DeviceSubstate.FINALIZED,
            )
            devices.append(dev)
            role_result = per_role.get("per_role", {}).get(_slug(role))
            if not role_result:
                continue
            path = Path(role_result["path"])
            rows = 0
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parsed = parse_row(line)
                    if parsed is not None and is_valid_data_row(
                        parsed, expected_device_id=device_id
                    ):
                        rows += 1
            file_results[device_id] = {
                "path": str(path),
                "rows": rows,
                "sha256": _sha256(path),
                "reordered": 0,
            }
        return file_results, devices

    def _session_true_hz(self) -> dict[str, float]:
        """Session-wide average of DISTINCT readings per second, per device.

        `acc_changes` counts distinct accelerometer readings and is reset at start_recording,
        so dividing by the elapsed recording time gives the whole-session rate — the same
        quantity sampling_analysis derives from the finished CSV, without re-reading it.
        """
        elapsed = time.monotonic() - self._recording_started_at
        if elapsed <= 0:
            return {}
        return {d.device_id: d.acc_changes / elapsed for d in self._devices.values()}

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
            # Checkpoint device counters, first-packet timestamps, and offline intervals at
            # the same cadence. After a backend crash, restart reconciliation then has the
            # last known connectivity boundary instead of only a stale session identity.
            try:
                await self._save_state()
            except Exception as exc:
                await audit.log("ERROR", "recording_checkpoint_failed", {
                    "session_id": self.session_id,
                    "error": str(exc),
                })

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
            # A successfully registered, still-open control WebSocket is retained.  The
            # control handler now emits server heartbeats and will clean up immediately
            # on a send failure; pruning an open channel merely because a client PING is
            # late creates a false disconnect and loses the operator's device state.
            dead = [
                device_id
                for device_id, dev in self._devices.items()
                if dev.control_ws is None
            ]
            if not dead:
                continue
            for device_id in dead:
                self._devices.pop(device_id, None)
                drop_latest_sample(device_id)
                await audit.log("INFO", "device_pruned_idle", {"device_id": device_id})
            await broadcast_to_frontends(_state_snapshot())


session_manager = SessionManager()
