"""
Dev runner — works from ANY directory:
    python master_backend/run.py
Manual equivalent (must be run from the repo root):
    python -m uvicorn master_backend.app.main:app --host 0.0.0.0 --port 8000 \
        --timeout-graceful-shutdown 5

Set IMU_DEV_RELOAD=1 to restart on code changes while developing. Never in the field:
a reload mid-session restarts the backend under a running recording.
"""
import os
import sys
from pathlib import Path

# app/main.py imports `master_backend.*` (absolute), so the repo root must be on
# sys.path. Set it on PYTHONPATH too: uvicorn's --reload spawns a fresh interpreter
# that only inherits env vars, not the parent's sys.path.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ["PYTHONPATH"] = os.pathsep.join(
    filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH", "")])
)

import uvicorn

if __name__ == "__main__":
    dev_reload = os.environ.get("IMU_DEV_RELOAD") == "1"
    uvicorn.run(
        "master_backend.app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=dev_reload,
        reload_dirs=[str(REPO_ROOT / "master_backend")] if dev_reload else None,
        log_level="info",
        # Camera previews are endless MJPEG responses. Without a bound, uvicorn waits for
        # them forever on shutdown, the lifespan never closes the camera service, and the
        # backend and its ffmpeg children never exit.
        timeout_graceful_shutdown=5,
    )
