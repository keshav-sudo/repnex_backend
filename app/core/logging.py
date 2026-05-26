from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

from pythonjsonlogger import jsonlogger

from app.core.config import get_settings

REDACT_KEYS = {
    "password",
    "hashed_password",
    "encrypted_username",
    "encrypted_password",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "api_key",
    "openai_api_key",
    "fernet_key",
    "jwt_secret",
}

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
org_id_ctx: ContextVar[str | None] = ContextVar("org_id", default=None)
user_id_ctx: ContextVar[str | None] = ContextVar("user_id", default=None)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        record.org_id = org_id_ctx.get()
        record.user_id = user_id_ctx.get()
        return True


class _RedactingFormatter(jsonlogger.JsonFormatter):
    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record["level"] = record.levelname
        log_record["logger"] = record.name
        _redact(log_record)


def _redact(obj: Any) -> None:
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            if k.lower() in REDACT_KEYS:
                obj[k] = "***REDACTED***"
            else:
                _redact(obj[k])
    elif isinstance(obj, list):
        for item in obj:
            _redact(item)


def setup_logging() -> None:
    settings = get_settings()
    root = logging.getLogger()
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _RedactingFormatter(
            "%(timestamp)s %(level)s %(logger)s %(message)s",
            timestamp=True,
            rename_fields={"asctime": "timestamp"},
        )
    )
    handler.addFilter(_ContextFilter())
    root.addHandler(handler)
    root.setLevel(settings.LOG_LEVEL.upper())

    # Quiet noisy libs
    for name in ("uvicorn.access", "sqlalchemy.engine.Engine"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
