"""
WebSocket endpoints for telemetry and control channels (CLAUDE.md §8).
/ws/telemetry — SensorPacket binary stream (device → backend)
/ws/control   — Command binary channel (bidirectional)
"""
import asyncio
import json
import logging
import math
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from master_backend.proto.sensor_packet import DeviceRegister, SensorPacket
from master_backend.proto.commands import (
    Command, CommandType,
    make_ack, make_clock_sync_response, make_error_alert, make_pong,
)
from .audit_logger import audit
from .dedup_store import dedup
from .io_manager import io_manager
from .session_manager import SessionState, session_manager

logger = logging.getLogger(__name__)
router = APIRouter()

# Active frontend WebSocket connections (operator dashboards).
_frontend_connections: set[WebSocket] = set()
# Latest sensor sample per device — pushed to /ws/live at 20 fps.
_latest_samples: dict[str, dict] = {}

# Packet schema versions this backend will ingest. v2 appends acc_ts_ms/gyro_ts_ms/
# sample_kind; v1 phones omit them and parse to 0, so both are accepted. This gate
# `continue`s on mismatch — i.e. silently DROPS the packet — so it must be widened
# before, never after, phones start emitting a new version.
_ACCEPTED_SCHEMA_VERSIONS = frozenset({1, 2})


def _state_pong(command_id: str = "") -> bytes:
    """Authoritative session state, delivered on the PONG heartbeat (plan DD-1)."""
    return make_pong(
        command_id,
        state=session_manager.state.value,
        session_id=session_manager.session_id or "",
        late_sid=io_manager.late_session_id,
    )


def drop_latest_sample(device_id: str) -> None:
    """Remove a pruned device's cached sample so /ws/live stops re-broadcasting it
    and the cache cannot grow unbounded across device churn."""
    _latest_samples.pop(device_id, None)


async def broadcast_to_frontends(msg: dict) -> None:
    """Push a JSON state update to all connected operator dashboards."""
    dead: set[WebSocket] = set()
    text = json.dumps(msg)
    for ws in _frontend_connections:
        try:
            await ws.send_text(text)
        except Exception:
            dead.add(ws)
    _frontend_connections.difference_update(dead)


# ── Telemetry channel ─────────────────────────────────────────────────────────

@router.websocket("/ws/telemetry")
async def telemetry_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    device_id: str | None = None

    try:
        async for raw in websocket.iter_bytes():
            try:
                pkt = SensorPacket.from_bytes(raw)
            except Exception as exc:
                logger.debug("Failed to parse SensorPacket: %s", exc)
                continue

            device_id = pkt.device_id

            if pkt.schema_version not in _ACCEPTED_SCHEMA_VERSIONS:
                await audit.log(
                    "WARN",
                    "schema_mismatch",
                    {"expected": sorted(_ACCEPTED_SCHEMA_VERSIONS),
                     "got": pkt.schema_version, "device_id": device_id},
                )
                continue

            if session_manager.state != SessionState.RECORDING:
                # Rate accounting must run in EVERY state, not just RECORDING. Phones stream
                # telemetry from the moment they connect (attachSensorStream is persistent),
                # and preflight gates START on true_hz — so counting only while RECORDING left
                # the gate reading 0 Hz before every session and blocked recording outright.
                # start_recording() resets these counters, so idle traffic never contaminates
                # a session's totals. Deliberately outside the dedup path below: that one is
                # gated on the session id and exists to keep duplicate rows off disk.
                session_manager.increment_packets(device_id)
                session_manager.note_sample(device_id, (pkt.acc_x, pkt.acc_y, pkt.acc_z))
                # Post-STOP grace window: a phone reconnecting within LATE_ACCEPT_SEC is
                # flushing the buffered tail of the session that just ended. Dropping it
                # here (and letting the phone then erase its own copy) was a total-loss
                # path (plan D3).
                late_sid = io_manager.late_session_id
                if late_sid:
                    dev = session_manager.get_device(device_id)
                    role = dev.device_role if dev else "unknown"
                    if not dedup.is_duplicate(device_id, late_sid, pkt.sequence_number):
                        dedup.add(device_id, late_sid, pkt.sequence_number)
                        await io_manager.write_late(pkt, role)
                continue

            if dedup.is_duplicate(device_id, session_manager.session_id, pkt.sequence_number):
                continue

            dedup.add(device_id, session_manager.session_id, pkt.sequence_number)
            # A device that joined or rejoined after START has no writer yet; without this
            # every one of its packets is discarded while the dashboard counts them (plan D2).
            if not io_manager.has_writer(device_id):
                dev = session_manager.get_device(device_id)
                if dev is not None:
                    await io_manager.ensure_writer(device_id, dev.device_role)
            session_manager.increment_packets(device_id)
            session_manager.note_sample(device_id, (pkt.acc_x, pkt.acc_y, pkt.acc_z))
            session_manager.mark_first_packet(device_id, pkt.timestamp_ms)
            try:
                await io_manager.write_packet(pkt)
            except OSError as exc:
                logger.error("write_packet failed for %s: %s", device_id[:8], exc)
                await audit.log("ERROR", "write_packet_failed", {"device_id": device_id, "error": str(exc)})
                # Do not re-raise — an I/O error must not drop the WebSocket.
                # The mobile will activate its blackbox buffer on next reconnect.
            # Cache latest sample for /ws/live dashboard chart.
            _latest_samples[device_id] = {
                "acc": [round(pkt.acc_x, 4), round(pkt.acc_y, 4), round(pkt.acc_z, 4)],
                "gyro": [round(pkt.gyro_x, 4), round(pkt.gyro_y, 4), round(pkt.gyro_z, 4)],
                "ts": pkt.timestamp_ms,
            }

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        await audit.log("ERROR", "telemetry_ws_error", {"error": str(exc), "device_id": device_id})
    finally:
        # Do NOT call unregister_device here. The telemetry channel is a data pipe only;
        # control_ws and device lifecycle are managed exclusively by control_ws.finally.
        # Calling unregister_device here wipes control_ws while the control channel is
        # still alive, causing the dashboard to show 0 devices after recording stops.
        if device_id:
            session_manager.note_telemetry_disconnect(device_id)
        await audit.log("INFO", "ws_disconnect", {"channel": "telemetry", "device_id": device_id})


# ── Control channel ───────────────────────────────────────────────────────────

@router.websocket("/ws/control")
async def control_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    device_id: str | None = None

    try:
        # First binary message must be DeviceRegister.
        first_raw = await websocket.receive_bytes()
        reg = DeviceRegister.from_bytes(first_raw)

        if not reg.is_valid:
            await websocket.close(code=4001, reason="Invalid DeviceRegister")
            return

        device_id = reg.device_id

        # Role uniqueness check (CLAUDE.md §22.1)
        collision = session_manager.register_device(
            device_id=reg.device_id,
            role=reg.device_role,
            model=reg.device_model,
            app_version=reg.app_version,
            ws=websocket,
        )
        if collision:
            await audit.log("WARN", "role_collision", {"role": reg.device_role, "device_id": device_id[:8]})
            await websocket.send_bytes(
                make_error_alert("ROLE_COLLISION", collision)
            )
            await websocket.close(code=4002, reason=collision)
            return

        await audit.log(
            "INFO",
            "device_connected",
            {"device_id": device_id[:8], "role": reg.device_role, "model": reg.device_model},
        )
        # Notify frontend immediately so device shows as online.
        await broadcast_to_frontends(_state_snapshot())
        # Reconciliation on (re)connect: tell the device what the backend believes is
        # happening RIGHT NOW. Without this a phone that was offline during STOP stays
        # stuck showing "RECORDING" forever (plan D1).
        await websocket.send_bytes(_state_pong())

        # Subsequent messages are Commands.
        async for raw in websocket.iter_bytes():
            try:
                cmd = Command.from_bytes(raw)
            except Exception as exc:
                logger.debug("Failed to parse Command from %s: %s", device_id[:8], exc)
                continue

            try:
                await _handle_command(cmd, device_id, websocket)
            except Exception as exc:
                logger.warning("Command error for %s: %s", device_id[:8], exc)
                # Log and continue — a single bad command must not close the connection.

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        await audit.log("ERROR", "control_ws_error", {"error": str(exc), "device_id": device_id})
    finally:
        session_manager.unregister_device(device_id)
        await audit.log("INFO", "ws_disconnect", {"channel": "control", "device_id": device_id})
        # Notify frontend so device shows as offline immediately.
        await broadcast_to_frontends(_state_snapshot())


async def _handle_command(cmd: Command, device_id: str, ws: WebSocket) -> None:
    match cmd.type:
        case CommandType.PING:
            session_manager.mark_ping(device_id)
            await ws.send_bytes(_state_pong(cmd.command_id))

        case CommandType.CLOCK_SYNC:
            t1_ms = int(time.time() * 1000)
            try:
                payload = json.loads(cmd.payload)
                t0_ms = int(payload["t0_ms"])
            except Exception:
                return
            t2_ms = int(time.time() * 1000)
            await ws.send_bytes(
                make_clock_sync_response(cmd.command_id, t0_ms, t1_ms, t2_ms)
            )

        case CommandType.START_SESSION:
            try:
                payload = json.loads(cmd.payload) if cmd.payload else {}
            except Exception:
                payload = {}
            ok, detail = await session_manager.start_recording(payload)
            await ws.send_bytes(make_ack(cmd.command_id, "ok" if ok else "fail", "" if ok else detail))
            if ok:
                await audit.log("INFO", "session_start", {"initiated_by": device_id[:8]})
                # Broadcast START to all other connected devices, carrying the metadata the
                # phones need to name their local rescue CSVs (subject/tag/operator) and embed
                # in the CSV header.
                start_cmd = Command(
                    type=CommandType.START_SESSION,
                    payload=json.dumps({
                        "session_id": session_manager.session_id,
                        "subject": session_manager.subject_name,
                        "session_tag": session_manager.session_tag,
                        "operator": session_manager.operator,
                    }),
                    command_id=cmd.command_id,
                ).to_bytes()
                await session_manager.broadcast_control(start_cmd)

        case CommandType.STOP_SESSION:
            if session_manager.state != SessionState.RECORDING:
                await ws.send_bytes(make_ack(cmd.command_id, "fail", "Not recording"))
                return
            try:
                payload = json.loads(cmd.payload) if cmd.payload else {}
                reason = payload.get("reason", "operator_stop")
            except Exception:
                reason = "operator_stop"
            await session_manager.stop_recording(reason)
            await ws.send_bytes(make_ack(cmd.command_id, "ok"))
            await audit.log("INFO", "session_stop", {"reason": reason})

        case CommandType.SET_LABEL:
            try:
                payload = json.loads(cmd.payload)
                label_id = int(payload["label_id"])
                label_name = str(payload.get("label_name", str(label_id)))
            except Exception:
                await ws.send_bytes(make_ack(cmd.command_id, "fail", "Bad SET_LABEL payload"))
                return
            io_manager.set_label(label_id, label_name)
            await ws.send_bytes(make_ack(cmd.command_id, "ok"))
            await audit.log(
                "INFO",
                "label_injected",
                {"label_id": label_id, "label_name": label_name, "applied_at_ms": int(time.time() * 1000)},
            )

        case _:
            logger.debug("Unhandled command type %s from %s", cmd.type, device_id[:8])


# ── Frontend dashboard channel (JSON) ─────────────────────────────────────────

@router.websocket("/ws/frontend")
async def frontend_ws(websocket: WebSocket) -> None:
    """Operator dashboard — JSON protocol, not binary proto."""
    await websocket.accept()
    _frontend_connections.add(websocket)
    await audit.log("INFO", "frontend_connected", {})

    # Send current state immediately.
    await websocket.send_text(json.dumps(_state_snapshot()))

    try:
        async for text in websocket.iter_text():
            try:
                msg = json.loads(text)
            except Exception:
                continue
            await _handle_frontend_msg(msg, websocket)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("frontend_ws error: %s", exc)
    finally:
        _frontend_connections.discard(websocket)
        await audit.log("INFO", "frontend_disconnected", {})


async def _handle_frontend_msg(msg: dict, ws: WebSocket) -> None:
    cmd_type = msg.get("type", "")
    command_id = msg.get("command_id", "")
    payload = msg.get("payload", {})

    match cmd_type:
        case "START_SESSION":
            ok, detail = await session_manager.start_recording(payload)
            await ws.send_text(json.dumps({
                "type": "ACK",
                "command_id": command_id,
                "status": "ok" if ok else "fail",
                "detail": "" if ok else detail,
            }))
            if ok:
                # Broadcast coordinated START to all mobile devices (CLAUDE.md §22.5)
                start_cmd = Command(
                    type=CommandType.START_SESSION,
                    payload=json.dumps({
                        "session_id": session_manager.session_id,
                        "scheduled_start_ms": session_manager.scheduled_start_ms,
                    }),
                ).to_bytes()
                await session_manager.broadcast_control(start_cmd)
                await broadcast_to_frontends({
                    "type": "STATE_UPDATE",
                    **_state_snapshot(),
                    "scheduled_start_ms": session_manager.scheduled_start_ms,
                })

        case "STOP_SESSION":
            reason = payload.get("reason", "operator_stop")
            report = await session_manager.stop_recording(reason)
            await ws.send_text(json.dumps({
                "type": "ACK",
                "command_id": command_id,
                "status": "ok",
            }))
            await broadcast_to_frontends({
                "type": "STATE_UPDATE",
                **_state_snapshot(),
                "integrity_report": report,
            })

        case "SET_LABEL":
            label_id = int(payload.get("label_id", 0))
            label_name = str(payload.get("label_name", str(label_id)))
            io_manager.set_label(label_id, label_name)
            await audit.log("INFO", "label_injected", {
                "label_id": label_id,
                "label_name": label_name,
                "applied_at_ms": int(time.time() * 1000),
            })
            await ws.send_text(json.dumps({
                "type": "ACK",
                "command_id": command_id,
                "status": "ok",
            }))

        case "GET_STATE":
            await ws.send_text(json.dumps(_state_snapshot()))

        case "RESET":
            if session_manager.state == SessionState.RECORDING:
                await ws.send_text(json.dumps({
                    "type": "ACK",
                    "command_id": command_id,
                    "status": "fail",
                    "detail": "Cannot reset connections while a session is recording",
                }))
                return
            await session_manager.reset_all()
            _latest_samples.clear()   # stop /ws/live re-broadcasting stale device samples
            await ws.send_text(json.dumps({
                "type": "ACK",
                "command_id": command_id,
                "status": "ok",
            }))
            await audit.log("INFO", "operator_reset", {})
            await broadcast_to_frontends(_state_snapshot())


def _state_snapshot() -> dict:
    return {
        "type": "STATE_UPDATE",
        "state": session_manager.state,
        "session_id": session_manager.session_id,
        "subject": session_manager.subject_name,
        "session_tag": session_manager.session_tag,
        "operator": session_manager.operator,
        # Use control_ws presence as source of truth for online status.
        "devices": [
            {
                "device_id": d.device_id,
                "role": d.device_role,
                "is_online": d.control_ws is not None,
                "packets": d.packets_received,
                # Surfaced so a stale handset build is visible on the dashboard. Three phones
                # ran a pre-v2 build for days on 2026-08-07, leaving acc_ts_ms/sample_kind
                # empty on every row, and nothing anywhere reported it.
                "app_version": d.app_version,
                "device_model": d.device_model,
                "substate": d.substate,
                "first_packet_ts": d.first_packet_ts,
                "offline_intervals": len(d.offline_intervals),
                "rate_hz": round(d.rate_hz, 1),
                "true_hz": round(d.true_hz, 1),
                "true_hz_avg": round(d.true_hz_avg, 1),
                "held_pct": round(d.held_pct, 1),
                "streaming": (time.monotonic() - d.last_packet_at) < 3.0 if d.last_packet_at else False,
            }
            for d in session_manager._devices.values()
        ],
        "quorum": {
            "connected": len(session_manager.online_devices),
            "roles": session_manager.connected_roles,
        },
    }


# ── Live sensor data stream for dashboard chart (JSON, 20 fps) ───────────────

_live_connections: set[WebSocket] = set()


@router.websocket("/ws/live")
async def live_ws(websocket: WebSocket) -> None:
    """Push latest sensor samples to frontend chart at ~20 fps."""
    await websocket.accept()
    _live_connections.add(websocket)
    try:
        # Keep open until client disconnects.
        await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _live_connections.discard(websocket)


def _safe_float(v: object) -> object:
    """Replace NaN/Inf with None so json.dumps never raises ValueError."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


async def _live_broadcaster_loop() -> None:
    """Background task started from main.py lifespan."""
    while True:
        await asyncio.sleep(0.05)  # 20 fps — CancelledError propagates cleanly on shutdown
        if not _latest_samples or not _live_connections:
            continue
        try:
            safe: dict = {
                dev: {
                    k: [_safe_float(f) for f in v] if isinstance(v, list) else _safe_float(v)
                    for k, v in data.items()
                }
                for dev, data in _latest_samples.items()
            }
            msg = json.dumps({"samples": safe})
        except Exception as exc:
            logger.warning("live_broadcaster: serialization error, skipping frame: %s", exc)
            continue
        dead: set[WebSocket] = set()
        for ws in _live_connections:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        _live_connections.difference_update(dead)
