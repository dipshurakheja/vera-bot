"""Structured JSON logging. Never log secrets or customer PII (names/phones)."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_SECRET_KEYS = {"api_key", "llm_api_key", "authorization", "x-api-key", "password", "token", "phone", "phone_redacted"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "event": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for k, v in fields.items():
                payload[k] = "<redacted>" if k.lower() in _SECRET_KEYS else v
        if record.exc_info and record.levelno >= logging.ERROR:
            # exception type only — no stack traces in logs shipped off-box
            payload["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
        return json.dumps(payload, ensure_ascii=False, default=str)


_configured = False


def configure(level: str = "INFO") -> None:
    global _configured
    root = logging.getLogger("vera")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not _configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
        root.propagate = False
        _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"vera.{name}")


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={"fields": fields})
