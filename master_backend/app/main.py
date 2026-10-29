"""
IMU Telemetry Backend — FastAPI entry point.

Run from the REPOSITORY ROOT (parent of master_backend/), not from inside
master_backend/ — the code uses absolute `master_backend.*` imports and
pyproject.toml declares the package root one level up:

    uvicorn master_backend.app.main:app --host 0.0.0.0 --port 8000 --reload
"""
import logging
import os
import socket
import subprocess
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

from .audit_logger import audit
from .session_manager import SessionState, session_manager


@lru_cache(maxsize=1)
def build_id() -> str:
    """Identity of the running backend, for the dashboard's preflight version check.

    The dashboard bakes its own git sha in at build time and compares the two, so a skew is
    caught before a session instead of surfacing as a render crash at save — the 2026-08-11
    failure, where a browser ran a bundle that existed on no disk and matched no commit.

    Returns "unknown" rather than guessing when this is not a git checkout; the preflight check
    treats "unknown" as pending, never as a mismatch, so a false alarm can never block an
    operator mid-study.
    """
    env = os.getenv("BUILD_ID", "").strip()
    if env:
        return env
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"
from .upload import router as recovery_router
from .export import router as export_router
from .cameras import router as cameras_router
from .ws_handler import router as ws_router, _live_broadcaster_loop
from .api.routes import router as api_router
from .api.errors import install_error_handlers
from .api.post_session import PostSessionJob
from .api.runtime import (
    camera_router_or_none,
    check_disk,
    create_runtime,
    set_runtime,
    watchdog_loop,
    wire_lifecycle,
)
from .api.static_site import mount_static
from .api.ws_ui import UiHub, router as ui_ws_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    _ensure_dirs()
    await _start_audit()
    await _start_mdns()
    # Capture the list before recovery: recovery finalises those sessions and makes them
    # terminal, but the post-session job still owes them cameras + package.
    interrupted = [
        str(record.get("session_id"))
        for record in session_manager.get_interrupted_sessions()
        if record.get("session_id")
    ]
    await session_manager.recover_interrupted_sessions()
    _check_interrupted_sessions()
    asyncio.create_task(_live_broadcaster_loop())
    asyncio.create_task(session_manager.run_idle_reaper())

    # v3 runtime: camera seam, notice/draft stores, post-session job and the UI hub.
    rt = create_runtime()
    set_runtime(rt)
    rt.post = PostSessionJob(rt)
    rt.hub = UiHub(rt)
    rt.hub.start()
    unwire_lifecycle = wire_lifecycle(rt)
    rt.post.start()
    check_disk(rt)
    watchdog = asyncio.create_task(watchdog_loop(rt), name="notice-watchdog")
    await rt.post.resume_pending(interrupted)
    local_ip = _get_local_ip()
    port = int(os.getenv("PORT", "8000"))
    logger.info(
        "\n"
        "==================================================================\n"
        "  IMU Telemetry Backend — READY\n"
        "  Dashboard (this laptop) : http://localhost:3000\n"
        "  Backend LAN IP          : %s:%s   <-- type THIS into phones & dashboard\n"
        "  Phone telemetry WS      : ws://%s:%s/ws/telemetry\n"
        "  Phone control   WS      : ws://%s:%s/ws/control\n"
        "  Health check            : http://%s:%s/health\n"
        "==================================================================",
        local_ip, port, local_ip, port, local_ip, port, local_ip, port,
    )
    yield
    # Shutdown — finalise an in-flight session so the CSV tail is fsynced, the file is
    # closed, and an integrity report exists. Ctrl-C used to lose up to FSYNC_INTERVAL_SEC
    # of rows and leave the session unfinalised (plan D8).
    shutdown_session = ""
    try:
        if session_manager.state == SessionState.RECORDING:
            shutdown_session = session_manager.session_id
            logger.warning("Shutdown during RECORDING — finalising session %s",
                           shutdown_session)
            await session_manager.stop_recording("backend_shutdown")
    except Exception as exc:
        logger.error("Shutdown finalisation failed: %s", exc)
    # That finalize ran outside the REST stop handler, so nothing started the cameras step
    # or the package one. Without this, a restart mid-session leaves a terminal session
    # with no ZIP and no post record for the next startup to resume from.
    if rt.post is not None and shutdown_session:
        try:
            await rt.post.on_stopped(shutdown_session)
            await rt.post.wait_settled(shutdown_session)
        except Exception as exc:
            logger.error("Shutdown packaging failed: %s", exc)
    # Tear down the v3 layer in reverse order. The post job goes first so no new package
    # run starts while the writers are closing; the camera service is closed last.
    watchdog.cancel()
    try:
        await rt.post.close()
        await rt.hub.close()
    except Exception as exc:
        logger.error("v3 shutdown failed: %s", exc)
    try:
        unwire_lifecycle()
    except Exception:
        pass
    try:
        await rt.camera_service.close()
    except Exception as exc:
        logger.debug("camera service close failed: %s", exc)
    set_runtime(None)
    await audit.close()
    await _stop_mdns()


app = FastAPI(
    title="IMU Telemetry Backend",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Frontend on same LAN; tighten via LAN_SUBNET if needed
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ws_router)
app.include_router(recovery_router)
app.include_router(export_router)
app.include_router(cameras_router)
app.include_router(api_router)
app.include_router(ui_ws_router)
# Lane L5's /api/cameras/* + /api/calibration/* router, when this checkout has it.
_camera_router = camera_router_or_none()
if _camera_router is not None:
    app.include_router(_camera_router)
install_error_handlers(app)
# Serves the built dashboard at /; must come after every backend route.
mount_static(app)


@app.get("/health")
async def health():
    local_ip = _get_local_ip()
    port = int(os.getenv("PORT", "8000"))
    return {
        "status": "ok",
        "version": "2.0.0",
        # Real commit identity. `version` is left untouched for any existing consumer.
        "build_id": build_id(),
        "session_state": session_manager.state,
        "session_id": session_manager.session_id or None,
        "online_devices": len(session_manager.online_devices),
        # --- added (additive) ---
        "lan_ip": local_ip,
        "port": port,
        "ws_telemetry": f"ws://{local_ip}:{port}/ws/telemetry",
        "ws_control": f"ws://{local_ip}:{port}/ws/control",
    }


@app.get("/session")
async def session_info():
    devices = [
        {
            "device_id": d.device_id,
            "role": d.device_role,
            "is_online": d.is_online,
            "packets": d.packets_received,
        }
        for d in session_manager.online_devices
    ]
    return {
        "state": session_manager.state,
        "session_id": session_manager.session_id,
        "subject": session_manager.subject_name,
        "devices": devices,
    }


@app.get("/session/recovery")
async def session_recovery_list():
    """List terminal/interrupted sessions for a dashboard that just reconnected."""
    return {
        "sessions": session_manager.list_recovery_sessions(),
    }


@app.get("/session/recovery/{session_id}")
async def session_recovery(session_id: str):
    """Return the durable lifecycle record even when no live WebSocket exists."""
    record = session_manager.get_recovery_session(session_id)
    if record is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="session ledger record not found")
    return record


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ensure_dirs():
    for env_key in ("SSD_PATH", "RESCUE_PATH", "RECOVERY_PATH"):
        p = Path(os.getenv(env_key, f"./{env_key.lower()}"))
        p.mkdir(parents=True, exist_ok=True)


async def _start_audit():
    log_path = Path(os.getenv("SSD_PATH", "./data")) / "backend_audit.jsonl"
    await audit.open(log_path)
    await audit.log("INFO", "backend_start", {"version": "2.0.0"})


def _check_interrupted_sessions():
    interrupted = session_manager.get_interrupted_sessions()
    if interrupted:
        logger.warning(
            "Found %d interrupted session(s): %s — recover via /session endpoint",
            len(interrupted),
            [s["session_id"] for s in interrupted],
        )


# mDNS advertisement so Flutter can discover the backend automatically.
_zc = None
_mdns_info = None


async def _start_mdns():
    global _zc, _mdns_info
    try:
        from zeroconf.asyncio import AsyncZeroconf
        from zeroconf import ServiceInfo

        local_ip = _get_local_ip()
        port = int(os.getenv("PORT", "8000"))
        _zc = AsyncZeroconf()
        _mdns_info = ServiceInfo(
            "_imu-telemetry._tcp.local.",
            "IMU Backend._imu-telemetry._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=port,
            properties={"version": "2.0.0"},
        )
        await _zc.async_register_service(_mdns_info)
        logger.info("mDNS registered — ip=%s port=%d", local_ip, port)
    except Exception as exc:
        logger.warning("mDNS registration skipped: %s", exc)


async def _stop_mdns():
    global _zc, _mdns_info
    if _zc and _mdns_info:
        try:
            await _zc.async_unregister_service(_mdns_info)
            await _zc.async_close()
        except Exception:
            pass


def _get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
