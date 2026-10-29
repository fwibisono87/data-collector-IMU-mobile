"""API.md error envelope for the v3 ``/api`` prefix.

Every non-2xx response under ``/api`` is ``{"error": {code, message, details}}`` and the
dashboard renders ``message`` inline. Legacy endpoints (``/session``, ``/export``,
``/recovery``, ``/cameras`` and the phone/WebSocket routes) keep their existing body
shapes: the handlers and the middleware below only act on paths under ``/api``.

``details.blockers`` is populated for ``blocked`` (START refused) with the list of
``Issue`` objects that readiness produced.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

#: HTTP status → API.md error code, for errors that were not raised as ApiError.
_STATUS_CODES = {
    400: "invalid",
    404: "not_found",
    409: "busy",
    422: "invalid",
    500: "internal",
    501: "invalid",
}


class ApiError(Exception):
    """A v3 API failure with the documented envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message,
                          "details": self.details}}


def blocked(message: str, blockers: list) -> ApiError:
    return ApiError(409, "blocked", message, {
        "blockers": [
            issue.model_dump(mode="json") if hasattr(issue, "model_dump") else issue
            for issue in blockers
        ],
    })


def bad_state(message: str) -> ApiError:
    return ApiError(409, "bad_state", message)


def not_found(message: str) -> ApiError:
    return ApiError(404, "not_found", message)


def busy(message: str) -> ApiError:
    return ApiError(409, "busy", message)


def invalid(message: str, details: dict | None = None) -> ApiError:
    return ApiError(422, "invalid", message, details)


def internal(message: str) -> ApiError:
    return ApiError(500, "internal", message)


def not_supported(message: str) -> ApiError:
    """501: the operation is valid but this platform cannot do it (API.md, open-folder)."""
    return ApiError(501, "invalid", message)


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api")


def install_error_handlers(app: FastAPI) -> None:
    """Attach the envelope handlers. Non-``/api`` paths fall through unchanged."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content=exc.body())

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        if not _is_api(request):
            return await request_validation_exception_handler(request, exc)
        return JSONResponse(status_code=422, content={
            "error": {
                "code": "invalid",
                "message": "The request body is not valid for this endpoint.",
                "details": {"errors": exc.errors()},
            },
        })

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if not _is_api(request):
            return await http_exception_handler(request, exc)
        code = _STATUS_CODES.get(exc.status_code, "internal")
        message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": message, "details": None}},
            headers=getattr(exc, "headers", None),
        )

    @app.middleware("http")
    async def _unhandled(request: Request, call_next):
        # User middleware sits outside ExceptionMiddleware, so only truly unhandled
        # exceptions reach this point. Outside /api they keep the server's default 500.
        try:
            return await call_next(request)
        except Exception as exc:
            if not _is_api(request):
                raise
            logger.exception("unhandled error on %s %s", request.method, request.url.path)
            return JSONResponse(status_code=500, content={
                "error": {
                    "code": "internal",
                    "message": f"{type(exc).__name__}: {exc}",
                    "details": None,
                },
            })