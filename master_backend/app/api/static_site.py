"""Serve the built dashboard from ``master_backend/app/static/``.

The dashboard is same-origin with the backend, so the operator needs no second server.
A missing build is not an error: it serves a one-line page telling the operator how to
build it, which is far more useful than a bare 404 when ``run.py`` was started on a fresh
checkout.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

#: ``app/api/static_site.py`` → ``app/static`` (resolved, so PyInstaller-frozen paths work
#: as long as the directory ships next to the package).
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"

#: These prefixes belong to the backend; an unknown path under them is a 404, not the SPA.
RESERVED_PREFIXES = (
    "/api", "/ws", "/upload", "/export", "/session", "/recovery", "/cameras",
    "/health", "/docs", "/redoc", "/openapi.json",
)

BUILTIN_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>IMU dashboard not built</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 3rem; color: #1f2937; }
    code { background: #f3f4f6; padding: .2rem .4rem; border-radius: .25rem; }
  </style>
</head>
<body>
  <h1>Dashboard not built</h1>
  <p>Run <code>npm run build</code> in the dashboard directory, then reload this page.</p>
  <p>The backend API is running; the phone-facing endpoints are unaffected.</p>
</body>
</html>
"""

_NO_STORE = {"Cache-Control": "no-store"}
_IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}


def _is_reserved(path: str) -> bool:
    clean = "/" + path.strip("/")
    return any(
        clean == prefix or clean.startswith(prefix + "/")
        for prefix in RESERVED_PREFIXES
    )


def _index_file() -> Path | None:
    index = STATIC_DIR / "index.html"
    return index if index.is_file() else None


def _asset_file(relative: str) -> Path | None:
    root = STATIC_DIR.resolve()
    candidate = (STATIC_DIR / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def mount_static(app: FastAPI) -> None:
    """Register the dashboard routes. Call after every backend router."""

    @app.get("/assets/{asset_path:path}", include_in_schema=False)
    async def static_asset(asset_path: str):
        path = _asset_file(f"assets/{asset_path}")
        if path is None:
            raise HTTPException(status_code=404, detail="asset not found")
        return FileResponse(path, headers=_IMMUTABLE)

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if _is_reserved(path):
            raise HTTPException(status_code=404, detail="not found")
        index = _index_file()
        if index is not None:
            return FileResponse(index, headers=_NO_STORE)
        return HTMLResponse(BUILTIN_PAGE, headers=_NO_STORE)