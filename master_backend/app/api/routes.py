"""The v3 REST surface (API.md), excluding L5's camera/calibration router.

Commands go over REST so every operator action gets a status code; ``/ws/ui`` only
pushes state. The endpoints reuse the legacy actions (``ws_handler``) rather than
reimplementing them, so the phone protocol cannot drift between the two dashboards.
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter

from ..audit_logger import audit
from ..io_manager import io_manager
from ..session_manager import IllegalTransition, SessionState, session_manager
from .. import ws_handler as legacy_ws
from .errors import bad_state, blocked, not_found
from .readiness import compute_readiness
from .runtime import get_runtime
from .schema import (
    Draft,
    DraftUpdate,
    EmptyResponse,
    Issue,
    LabelRequest,
    LabelResponse,
    Snapshot,
    StartRequest,
    StartResponse,
    StopRequest,
    StopResponse,
)
from .sessions import router as sessions_router
from .snapshot import build_snapshot

router = APIRouter(prefix="/api", tags=["session"])
router.include_router(sessions_router)

#: The last successful STOP, for the 10 s idempotency window in API.md.
_last_stop: tuple[str, float] | None = None
STOP_IDEMPOTENT_S = 10.0

_CAMERA_RETRY_S = 1.0


@router.get("/state", response_model=Snapshot)
async def get_state():
    return build_snapshot(get_runtime())


@router.put("/session/draft", response_model=Draft)
async def update_draft(body: DraftUpdate):
    return get_runtime().drafts.update(
        subject=body.subject,
        session_tag=body.session_tag,
        operator=body.operator,
    )


async def _camera_start(rt, session_id: str) -> list[Issue]:
    """Start capture, retrying once; a camera failure must never abort the IMU session."""
    folder = io_manager.session_folder
    if folder is None:
        rt.notices.raise_(
            code="camera_start_failed",
            level="error",
            message="The session folder was not opened, so camera recording could not start.",
            session_id=session_id,
            key=f"camera_start_failed:{session_id}",
        )
        return [Issue(code="camera_start_failed",
                      message="Camera recording did not start (no session folder).")]

    first_error: Exception | None = None
    for attempt in range(2):
        try:
            failed_cams = await rt.camera_service.start_recording(session_id, folder)
        except Exception as exc:  # camera service errors, ffmpeg, permissions…
            first_error = exc
            if attempt == 0:
                await asyncio.sleep(_CAMERA_RETRY_S)
            continue
        rt.notices.resolve(f"camera_start_failed:{session_id}")
        if failed_cams:
            # Capture started for the others; this one is live but dark. Warn now so the
            # operator can check the cable while the IMU session is still running.
            message = (
                f"{', '.join(failed_cams)} produced no video frames after start — check "
                "it now; the IMU recording continues"
            )
            rt.notices.raise_(
                code="camera_start_failed",
                level="warn",
                message=message,
                session_id=session_id,
                key=f"camera_start_failed:{session_id}",
            )
            return [Issue(code="camera_start_failed", message=message, subject="cameras")]
        return []
    message = (
        f"Camera recording did not start for session {session_id}: {first_error}. "
        "The IMU session is still recording; install the footage or restart the "
        "cameras after the session."
    )
    rt.notices.raise_(
        code="camera_start_failed",
        level="error",
        message=message,
        session_id=session_id,
        key=f"camera_start_failed:{session_id}",
    )
    return [Issue(code="camera_start_failed", message=message, subject="cameras")]


@router.post("/session/start", response_model=StartResponse)
async def start_session(_body: StartRequest | None = None):
    rt = get_runtime()
    # Recompute rather than trust the snapshot: the phone that was online when the
    # dashboard rendered may have dropped in the meantime.
    readiness = compute_readiness(rt)
    if readiness.blockers:
        raise blocked(
            "The session cannot start yet: "
            + "; ".join(issue.message for issue in readiness.blockers),
            readiness.blockers,
        )

    draft = rt.drafts.get()
    payload = {
        "subject_name": draft.subject,
        "session_tag": draft.session_tag,
        "operator": draft.operator,
        # Same provenance the legacy dashboard records: warnings never block a START,
        # but the session must carry the fact that they were red.
        "preflight_failed": [issue.message for issue in readiness.warnings],
    }
    ok, detail = await session_manager.start_recording(payload)
    if not ok:
        raise bad_state(detail)

    session_id = session_manager.session_id
    # Phones must get START immediately; this is the same broadcast the legacy dashboard
    # path sends after start_recording (payload included).
    await legacy_ws.broadcast_start_to_phones()
    await legacy_ws.broadcast_state_update(
        scheduled_start_ms=session_manager.scheduled_start_ms
    )
    await audit.log("INFO", "session_start", {"initiated_by": "v3_api"})

    warnings = await _camera_start(rt, session_id)
    # Consume the subject; tag/operator stay for the next session in the same visit.
    rt.drafts.clear_subject()
    return StartResponse(
        session_id=session_id,
        scheduled_start_ms=int(session_manager.scheduled_start_ms),
        warnings=warnings,
    )


@router.post("/session/stop", response_model=StopResponse)
async def stop_session(body: StopRequest | None = None):
    global _last_stop
    rt = get_runtime()
    session_id = session_manager.session_id
    if session_manager.state != SessionState.RECORDING:
        if (
            _last_stop is not None
            and _last_stop[0] == session_id
            and time.monotonic() - _last_stop[1] <= STOP_IDEMPOTENT_S
        ):
            return StopResponse(session_id=session_id)
        raise bad_state(
            f"Cannot stop from {session_manager.state.value}: only a RECORDING session "
            "can be stopped."
        )
    reason = (body.reason if body and body.reason else "operator_stop")
    try:
        await session_manager.request_stop(reason)
    except IllegalTransition as exc:
        raise bad_state(str(exc)) from exc

    if rt.post is not None:
        # Cameras run concurrently with FinalizeJob; packaging follows both.
        await rt.post.on_stopped(session_id)
    _last_stop = (session_id, time.monotonic())
    await audit.log("INFO", "session_stop", {"reason": reason})
    return StopResponse(session_id=session_id)


@router.post("/session/label", response_model=LabelResponse)
async def set_label(body: LabelRequest):
    if session_manager.state != SessionState.RECORDING:
        raise bad_state("A label can only be set while a session is recording.")
    await legacy_ws.apply_label(int(body.label_id), str(body.label_id))
    timeline = io_manager.label_timeline
    since_ms = int(timeline[-1]["timestamp_ms"]) if timeline else int(time.time() * 1000)
    return LabelResponse(label_id=int(body.label_id), since_ms=since_ms)


@router.post("/notices/{notice_id}/dismiss", response_model=EmptyResponse)
async def dismiss_notice(notice_id: str):
    if not get_runtime().notices.dismiss(notice_id):
        raise not_found(f"notice {notice_id} was not found")
    return EmptyResponse()


@router.post("/phones/reset", response_model=EmptyResponse)
async def reset_phones():
    if session_manager.state in (
        SessionState.RECORDING, SessionState.FINALIZING, SessionState.VALIDATING
    ):
        raise bad_state(
            "Phone connections cannot be reset while a session is recording or finalising."
        )
    await legacy_ws.reset_all_connections()
    return EmptyResponse()