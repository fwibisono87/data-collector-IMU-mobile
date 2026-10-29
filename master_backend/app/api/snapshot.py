"""The v3 Snapshot: one object the dashboard renders everything from.

Built from the existing singletons (SessionManager, IoManager), the runtime stores
(notices, draft), the camera service and the post-session job. No state is derived from
anything the browser remembers, which is what makes a page reload mid-session safe.
"""
from __future__ import annotations

import time

from ..io_manager import io_manager
from ..session_manager import SessionState, session_manager
from .post_session import session_progress
from .readiness import compute_readiness, _disk_anchor, _free_bytes
from .runtime import Runtime
from .schema import (
    CalibrationState,
    DiskInfo,
    Phone,
    SessionInfo,
    SessionLabel,
    Snapshot,
)

#: PREFLIGHT/READY exist for the phone handshake; the dashboard only sees IDLE.
_IDLE_STATES = {SessionState.IDLE, SessionState.PREFLIGHT, SessionState.READY}
_LIVE_STATES = {
    SessionState.RECORDING,
    SessionState.FINALIZING,
    SessionState.VALIDATING,
}


def _mapped_state() -> SessionState:
    state = session_manager.state
    return SessionState.IDLE if state in _IDLE_STATES else state


def _label() -> SessionLabel:
    timeline = io_manager.label_timeline
    if not timeline:
        return SessionLabel(id=0, name="0", since_ms=0)
    last = timeline[-1]
    return SessionLabel(
        id=int(last.get("label_id", 0)),
        name=str(last.get("label_name", "0")),
        since_ms=int(last.get("timestamp_ms", 0) or 0),
    )


def _session_info() -> SessionInfo | None:
    if session_manager.state not in _LIVE_STATES or not session_manager.session_id:
        return None
    return SessionInfo(
        session_id=session_manager.session_id,
        subject=session_manager.subject_name,
        session_tag=session_manager.session_tag,
        operator=session_manager.operator,
        started_at_ms=int(session_manager._recording_started_epoch_ms or 0),
        scheduled_start_ms=int(session_manager.scheduled_start_ms or 0),
        label=_label(),
    )


def _last_session_id() -> str | None:
    if session_manager.session_id:
        return session_manager.session_id
    for record in session_manager._ledger.list():
        sid = str(record.get("session_id") or "")
        if sid and (record.get("terminal") or record.get("state") == "IDLE"):
            return sid
    return None


def _phone(dev, recording: bool) -> Phone:
    online = dev.control_ws is not None
    offline_since_ms = None
    if not online and dev.offline_intervals:
        last = dev.offline_intervals[-1]
        if last.get("end_ms") is None:
            offline_since_ms = int(last.get("start_ms") or 0)
    return Phone(
        device_id=dev.device_id,
        role=dev.device_role,
        model=dev.device_model,
        app_version=dev.app_version,
        online=online,
        offline_since_ms=offline_since_ms,
        rate_hz=round(float(dev.rate_hz or 0.0), 1),
        true_hz_avg=round(float(dev.true_hz_avg or 0.0), 1),
        packets=int(dev.packets_received or 0),
        telemetry_age_ms=(
            int((time.monotonic() - dev.last_packet_at) * 1000)
            if dev.last_packet_at else None
        ),
        # A phone that is dark during RECORDING buffers locally; the Recovery view uses
        # this to explain why a rescue upload is still owed.
        buffering=bool(recording and not online),
    )


def _cameras(rt: Runtime):
    try:
        return list(rt.camera_service.cameras())
    except Exception:
        return []


def _calibration(rt: Runtime) -> CalibrationState:
    service = rt.camera_service
    ref_id = getattr(service, "active_ref_id", None)
    if callable(ref_id):
        try:
            ref_id = ref_id()
        except Exception:
            ref_id = None
    return CalibrationState(active_ref_id=str(ref_id) if ref_id else None)


def build_snapshot(rt: Runtime) -> Snapshot:
    # Imported lazily: main.py builds the app (and imports this module) at import time.
    from ..main import build_id

    state = _mapped_state()
    sid = session_manager.session_id
    progress = session_progress(rt.post, sid) if sid else None
    anchor = _disk_anchor(rt)
    return Snapshot(
        build_id=build_id(),
        server_time_ms=int(time.time() * 1000),
        state=state,
        session=_session_info(),
        last_session_id=_last_session_id(),
        draft=rt.drafts.get(),
        phones=[_phone(d, state == SessionState.RECORDING)
                for d in session_manager._devices.values()],
        cameras=_cameras(rt),
        readiness=compute_readiness(rt),
        finalize=progress,
        notices=rt.notices.snapshot(),
        disk=DiskInfo(path=str(anchor), free_bytes=_free_bytes(anchor)),
        calibration=_calibration(rt),
    )