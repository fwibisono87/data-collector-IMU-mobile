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
from contextlib import asynccontextmanager
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
from .upload import router as recovery_router
from .ws_handler import router as ws_router, _live_broadcaster_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    _ensure_dirs()
    await _start_audit()
    await _start_mdns()
    _check_interrupted_sessions()
    asyncio.create_task(_live_broadcaster_loop())
    asyncio.create_task(session_manager.run_idle_reaper())
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
    try:
        if session_manager.state == SessionState.RECORDING:
            logger.warning("Shutdown during RECORDING — finalising session %s",
                           session_manager.session_id)
            await session_manager.stop_recording("backend_shutdown")
    except Exception as exc:
        logger.error("Shutdown finalisation failed: %s", exc)
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


@app.get("/health")
async def health():
    local_ip = _get_local_ip()
    port = int(os.getenv("PORT", "8000"))
    return {
        "status": "ok",
        "version": "2.0.0",
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
