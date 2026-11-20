"""Typed errors rendered as RFC 7807 problem+json with error_code and request_id."""

from __future__ import annotations

from typing import Any, ClassVar

import asyncpg
import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = structlog.get_logger()


class AppError(Exception):
    status: ClassVar[int] = 400
    code: ClassVar[str] = "bad_request"
    title: ClassVar[str] = "Bad request"

    def __init__(self, detail: str, *, code: str | None = None, extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.error_code = code or self.code
        self.extra = extra or {}


class Unauthorized(AppError):
    status, code, title = 401, "unauthorized", "Unauthorized"


class PaymentRequired(AppError):
    status, code, title = 402, "payment_required", "Payment required"


class Forbidden(AppError):
    status, code, title = 403, "forbidden", "Forbidden"


class NotFound(AppError):
    status, code, title = 404, "not_found", "Not found"


class Conflict(AppError):
    status, code, title = 409, "conflict", "Conflict"


class PayloadTooLarge(AppError):
    status, code, title = 413, "payload_too_large", "Payload too large"


class Unprocessable(AppError):
    status, code, title = 422, "invalid_request", "Invalid request"


class UpstreamError(AppError):
    status, code, title = 502, "upstream_error", "Upstream service failed"


class ServiceUnavailable(AppError):
    status, code, title = 503, "service_unavailable", "Service unavailable"


class InternalError(AppError):
    status, code, title = 500, "internal_error", "Internal error"


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def problem(request: Request, status: int, title: str, detail: str, code: str, **extra: Any) -> JSONResponse:
    body = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": detail,
        "error_code": code,
        "request_id": _request_id(request),
        **extra,
    }
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        level = "error" if exc.status >= 500 else "info"
        getattr(log, level)("request.failed", error_code=exc.error_code, status=exc.status, detail=exc.detail)
        return problem(request, exc.status, exc.title, exc.detail, exc.error_code, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return problem(
            request, 422, "Invalid request", "Some fields are missing or invalid.", "invalid_request", errors=errors
        )

    # Database guards (RLS, grants, constraints) surface as clear client errors, never as 500s.
    @app.exception_handler(asyncpg.InsufficientPrivilegeError)
    async def _forbidden(request: Request, exc: asyncpg.InsufficientPrivilegeError) -> JSONResponse:
        log.warning("db.permission_denied", detail=str(exc))
        return problem(request, 403, "Forbidden", "You don't have permission to do that.", "forbidden")

    @app.exception_handler(asyncpg.CheckViolationError)
    async def _check(request: Request, exc: asyncpg.CheckViolationError) -> JSONResponse:
        log.info("db.check_violation", constraint=exc.constraint_name)
        return problem(
            request,
            422,
            "Invalid request",
            "That value isn't allowed.",
            "constraint_violation",
            constraint=exc.constraint_name,
        )

    @app.exception_handler(asyncpg.UniqueViolationError)
    async def _unique(request: Request, exc: asyncpg.UniqueViolationError) -> JSONResponse:
        log.info("db.unique_violation", constraint=exc.constraint_name)
        return problem(
            request, 409, "Conflict", "That already exists.", "already_exists", constraint=exc.constraint_name
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("request.unhandled", error_type=type(exc).__name__)
        return problem(request, 500, "Internal error", "Something went wrong on our side.", "internal_error")
