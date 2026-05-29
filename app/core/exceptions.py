from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, request_id_ctx

log = get_logger(__name__)


class AppError(Exception):
    """Base for application errors. Subclasses set status + code."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str = "", **details: Any) -> None:
        self.message = message or self.code
        self.details = details
        super().__init__(self.message)


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"


class TargetDBError(AppError):
    status_code = 502
    code = "target_db_error"


class LLMError(AppError):
    status_code = 502
    code = "llm_error"


class LLMTimeout(AppError):
    status_code = 504
    code = "llm_timeout"


class LLMBudgetExceeded(AppError):
    status_code = 402
    code = "llm_budget_exceeded"


class PoolExhausted(AppError):
    status_code = 503
    code = "pool_exhausted"


def _envelope(code: str, message: str, **extra: Any) -> dict[str, Any]:
    body = {"error": {"code": code, "message": message, "request_id": request_id_ctx.get()}}
    if extra:
        body["error"]["details"] = extra
    return body


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        log.warning("app_error", extra={"code": exc.code, "detail": exc.message})
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, **exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic v2 stores the raw Python exception in error["ctx"]["error"].
        # Raw exceptions are not JSON-serializable — stringify them first.
        safe_errors = []
        for err in exc.errors():
            safe_err = dict(err)
            if "ctx" in safe_err and isinstance(safe_err["ctx"], dict):
                safe_err["ctx"] = {
                    k: str(v) if isinstance(v, Exception) else v
                    for k, v in safe_err["ctx"].items()
                }
            safe_errors.append(safe_err)
        return JSONResponse(
            status_code=422,
            content=_envelope("validation_failed", "Invalid request", errors=safe_errors),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope("http_error", str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_exception")
        return JSONResponse(
            status_code=500,
            content=_envelope("internal_error", "Internal server error"),
        )
