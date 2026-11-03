"""REST routes for the camera service (``router = APIRouter(prefix="/api")``).

Every ``/api/cameras/*`` and ``/api/calibration/*`` endpoint from docs/rebuild/API.md,
returning the L2 schema models as ``response_model``. Errors use the API.md shape
``{"error": {code, message, details}}`` produced here — the routes do not rely on
L4's exception handlers.

The service instance comes from ``request.app.state.camera_service`` when L4 sets
it; otherwise a module-level singleton from :func:`create_service` is used (so tests
can mount this router alone). Note the calibration and preview endpoints use the
:class:`RealCameraService` surface (``compare``, ``capture_reference``, ``worker``,
``latest_frame``, ``store``) which is wider than the L2 ``CameraService`` protocol —
the in-memory ``FakeCameraService`` has no previews or references and gets clean
``not_found`` responses instead of crashes.
"""
from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from PIL import Image
from pydantic import ValidationError

from ..api.schema import (
    CameraSelectionRequest,
    CameraSelectionResponse,
    CompareResult,
    DeviceList,
    EmptyResponse,
    Reference,
    ReferenceCreate,
    ReferenceList,
    ReferenceRename,
)
from .calibration import CalibrationError
from .interface import CameraError
from .service import CodedCameraError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["cameras"])

#: API.md error codes → HTTP status.
_STATUS_FOR_CODE = {
    "blocked": 409,
    "bad_state": 409,
    "busy": 409,
    "not_found": 404,
    "invalid": 422,
    "internal": 500,
}

#: preview.mjpg: at most this many concurrent clients per camera (429 beyond).
PREVIEW_MAX_CLIENTS = 4
_PREVIEW_BOUNDARY = b"--frame\r\n"

_preview_counts: dict[str, int] = {}
_preview_lock = threading.Lock()


# ---------------------------------------------------------------------------
# service resolution (module singleton so the router can be mounted alone)
# ---------------------------------------------------------------------------
_service_singleton = None
_service_lock = threading.Lock()


def get_service(request: Request):
    """``app.state.camera_service`` when set (L4), else the module singleton."""
    service = getattr(request.app.state, "camera_service", None)
    if service is not None:
        return service
    global _service_singleton
    if _service_singleton is None:
        with _service_lock:
            if _service_singleton is None:
                from .service import create_service

                _service_singleton = create_service()
    return _service_singleton


def set_service(service) -> None:
    """Replace the module singleton (tests)."""
    global _service_singleton
    with _service_lock:
        _service_singleton = service


def reset_service() -> None:
    """Drop the module singleton without closing it (tests own the lifecycle)."""
    set_service(None)


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
def error_response(code: str, message: str, details=None,
                   status: int | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status or _STATUS_FOR_CODE.get(code, 500),
        content={"error": {"code": code, "message": message, "details": details}},
    )


def _map_exception(exc: Exception) -> JSONResponse:
    code = getattr(exc, "code", None)
    if code is None:
        # Plain CameraError: "not valid in the current state" per the seam docs.
        code = "bad_state" if isinstance(exc, CameraError) else "internal"
    if code == "internal":
        logger.exception("unhandled error in a camera route", exc_info=exc)
    return error_response(code, str(exc))


def _parse_body(model, raw: bytes):
    """Validate a request body; raises _InvalidBody for the API.md 422 shape."""
    try:
        return model.model_validate_json(raw or b"{}")
    except ValidationError as exc:
        raise _InvalidBody(exc.errors(include_url=False)) from exc


class _InvalidBody(Exception):
    def __init__(self, errors) -> None:
        super().__init__("invalid request body")
        self.errors = errors


# ---------------------------------------------------------------------------
# /api/cameras
# ---------------------------------------------------------------------------
@router.get("/cameras/devices", response_model=DeviceList)
async def get_devices(request: Request):
    service = get_service(request)
    # Enumeration may spawn ffmpeg (dshow) — keep the event loop free.
    devices = await asyncio.to_thread(service.list_devices)
    return DeviceList(devices=devices)


@router.put("/cameras/selection", response_model=CameraSelectionResponse)
async def put_selection(request: Request):
    service = get_service(request)
    raw = await request.body()
    # set_selection raises CodedCameraError("bad_state") while recording → 409.
    try:
        parsed = _parse_body(CameraSelectionRequest, raw)
    except _InvalidBody as exc:
        return error_response("invalid", "invalid request body",
                              details={"errors": exc.errors})
    try:
        cameras = await asyncio.to_thread(service.set_selection, parsed.cameras)
    except (CodedCameraError, CalibrationError, CameraError) as exc:
        return _map_exception(exc)
    return CameraSelectionResponse(cameras=cameras)


@router.get("/cameras/{cam_id}/latest.jpg")
async def get_latest_jpg(request: Request, cam_id: str):
    service = get_service(request)
    getter = getattr(service, "latest_frame", None)
    frame = getter(cam_id) if callable(getter) else None
    if frame is None:
        return error_response("not_found",
                              f"no frame available for camera {cam_id!r}")
    return Response(content=frame.data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


def _downscale_jpeg(data: bytes, width: int) -> bytes | None:
    """Downscale a JPEG to ``width`` (never upscale); None when undecodable."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            if image.width > width:
                height = max(1, round(image.height * width / image.width))
                image = image.resize((width, height), Image.BILINEAR)
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=80)
            return out.getvalue()
    except Exception:
        logger.debug("preview frame could not be downscaled", exc_info=True)
        return None


async def _preview_stream(request: Request, worker, cam_id: str, width: int,
                          fps: int):
    """multipart/x-mixed-replace generator; releases the client slot on exit."""
    sub = worker.subscribe(2, drop_oldest=True)
    interval = 1.0 / fps
    try:
        while True:
            started = time.monotonic()
            if await request.is_disconnected():
                break
            frame = await asyncio.to_thread(sub.get, interval + 0.5)
            if frame is None:
                continue
            jpeg = await asyncio.to_thread(_downscale_jpeg, frame.data, width)
            if jpeg is None:
                continue
            yield (_PREVIEW_BOUNDARY
                   + b"Content-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpeg)).encode("ascii") + b"\r\n\r\n" + jpeg + b"\r\n")
            remaining = interval - (time.monotonic() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)
    finally:
        worker.unsubscribe(sub)
        with _preview_lock:
            left = _preview_counts.get(cam_id, 1) - 1
            if left > 0:
                _preview_counts[cam_id] = left
            else:
                _preview_counts.pop(cam_id, None)


@router.get("/cameras/{cam_id}/frame.jpg")
async def get_frame_jpg(request: Request, cam_id: str,
                        w: int = Query(default=320, ge=64, le=1920)):
    """One preview frame. The dashboard polls this instead of holding a preview.mjpg stream
    open per camera: four never-ending streams plus the snapshot socket used up Chrome's
    six connections per host, and the STOP POST then queued in the browser forever."""
    service = get_service(request)
    worker_for = getattr(service, "worker", None)
    worker = worker_for(cam_id) if callable(worker_for) else None
    if worker is None:
        return error_response("not_found", f"unknown camera {cam_id!r}")
    sub = worker.subscribe(1, drop_oldest=True)
    try:
        frame = await asyncio.to_thread(sub.get, 1.5)
    finally:
        worker.unsubscribe(sub)
    if frame is None:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    jpeg = await asyncio.to_thread(_downscale_jpeg, frame.data, w)
    if jpeg is None:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/cameras/{cam_id}/preview.mjpg")
async def get_preview_mjpg(request: Request, cam_id: str,
                           w: int = Query(default=640, ge=64, le=1920),
                           fps: int = Query(default=10, ge=1, le=30)):
    service = get_service(request)
    worker_for = getattr(service, "worker", None)
    worker = worker_for(cam_id) if callable(worker_for) else None
    if worker is None:
        return error_response("not_found", f"unknown camera {cam_id!r}")
    with _preview_lock:
        if _preview_counts.get(cam_id, 0) >= PREVIEW_MAX_CLIENTS:
            return error_response(
                "busy",
                f"camera {cam_id!r} already has {PREVIEW_MAX_CLIENTS} preview clients",
                status=429,
            )
        _preview_counts[cam_id] = _preview_counts.get(cam_id, 0) + 1
    return StreamingResponse(
        _preview_stream(request, worker, cam_id, w, fps),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# /api/calibration
# ---------------------------------------------------------------------------
@router.post("/calibration/references", response_model=Reference)
async def post_reference(request: Request):
    service = get_service(request)
    raw = await request.body()
    capture = getattr(service, "capture_reference", None)
    if not callable(capture):
        return error_response("not_found", "calibration is not available")
    try:
        body = _parse_body(ReferenceCreate, raw)
    except _InvalidBody as exc:
        return error_response("invalid", "invalid request body",
                              details={"errors": exc.errors})
    try:
        # 409 busy while another capture runs; bad_state when nothing is live.
        return await capture(body.name)
    except (CalibrationError, CodedCameraError, CameraError) as exc:
        return _map_exception(exc)


@router.get("/calibration/references", response_model=ReferenceList)
async def get_references(request: Request):
    service = get_service(request)
    lister = getattr(service, "list_references", None)
    if not callable(lister):
        return error_response("not_found", "calibration is not available")
    references, active_ref_id = await asyncio.to_thread(lister)
    return ReferenceList(references=references, active_ref_id=active_ref_id)


@router.patch("/calibration/references/{ref_id}", response_model=Reference)
async def patch_reference(request: Request, ref_id: str):
    service = get_service(request)
    raw = await request.body()
    renamer = getattr(service, "rename_reference", None)
    if not callable(renamer):
        return error_response("not_found", "calibration is not available")
    try:
        body = _parse_body(ReferenceRename, raw)
    except _InvalidBody as exc:
        return error_response("invalid", "invalid request body",
                              details={"errors": exc.errors})
    try:
        return await asyncio.to_thread(renamer, ref_id, body.name)
    except (CalibrationError, CodedCameraError, CameraError) as exc:
        return _map_exception(exc)


@router.post("/calibration/references/{ref_id}/activate",
             response_model=EmptyResponse)
async def post_activate(request: Request, ref_id: str):
    service = get_service(request)
    activate = getattr(service, "activate_reference", None)
    if not callable(activate):
        return error_response("not_found", "calibration is not available")
    try:
        await asyncio.to_thread(activate, ref_id)
    except (CalibrationError, CodedCameraError, CameraError) as exc:
        return _map_exception(exc)
    return EmptyResponse()


@router.post("/calibration/deactivate", response_model=EmptyResponse)
async def post_deactivate(request: Request):
    service = get_service(request)
    deactivate = getattr(service, "deactivate_reference", None)
    if not callable(deactivate):
        return error_response("not_found", "calibration is not available")
    try:
        await asyncio.to_thread(deactivate)
    except (CalibrationError, CodedCameraError, CameraError) as exc:
        return _map_exception(exc)
    return EmptyResponse()


@router.delete("/calibration/references/{ref_id}", response_model=EmptyResponse)
async def delete_reference(request: Request, ref_id: str):
    service = get_service(request)
    delete = getattr(service, "delete_reference", None)
    if not callable(delete):
        return error_response("not_found", "calibration is not available")
    try:
        # 409 bad_state when the reference is the active one.
        await asyncio.to_thread(delete, ref_id)
    except (CalibrationError, CodedCameraError, CameraError) as exc:
        return _map_exception(exc)
    return EmptyResponse()


@router.get("/calibration/compare", response_model=CompareResult)
async def get_compare(request: Request,
                      ref_id: str | None = Query(default=None)):
    service = get_service(request)
    if ref_id is None:
        return error_response("invalid", "ref_id query parameter is required")
    compare = getattr(service, "compare", None)
    if not callable(compare):
        return error_response("not_found", "calibration is not available")
    try:
        return await compare(ref_id)
    except (CalibrationError, CodedCameraError, CameraError) as exc:
        return _map_exception(exc)


def _reference_image(request: Request, ref_id: str, key_slug: str,
                     suffix: str, media_type: str):
    service = get_service(request)
    store = getattr(service, "store", None)
    if store is None:
        return error_response("not_found", "calibration is not available")
    try:
        ref_dir: Path = store.ref_dir(ref_id)  # validates the id shape → not_found
    except CalibrationError as exc:
        return _map_exception(exc)
    if not key_slug or "/" in key_slug or "\\" in key_slug or ".." in key_slug:
        return error_response("invalid", "invalid key slug")
    path = (ref_dir / f"{key_slug}{suffix}").resolve()
    if not str(path).startswith(str(ref_dir.resolve())):
        return error_response("invalid", "invalid key slug")
    if not path.is_file():
        return error_response("not_found", f"no image {path.name} in {ref_id}")
    return FileResponse(path, media_type=media_type,
                        headers={"Cache-Control": "no-store"})


@router.get("/calibration/references/{ref_id}/{key_slug}.jpg")
async def get_reference_jpg(request: Request, ref_id: str, key_slug: str):
    return _reference_image(request, ref_id, key_slug, ".jpg", "image/jpeg")


@router.get("/calibration/references/{ref_id}/{key_slug}.edges.png")
async def get_reference_edges(request: Request, ref_id: str, key_slug: str):
    return _reference_image(request, ref_id, key_slug, ".edges.png", "image/png")
