"""
Dev runner — works from ANY directory:
    python master_backend/run.py
Manual equivalent (must be run from the repo root):
    python -m uvicorn master_backend.app.main:app --host 0.0.0.0 --port 8000 --reload
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
    uvicorn.run(
        "master_backend.app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=[str(REPO_ROOT / "master_backend")],
        log_level="info",
    )
