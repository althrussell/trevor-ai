"""Structured JSON logging and secret redaction for the Hermes-on-Databricks runtime.

Goals:

* Single-line JSON per log record, easy for Databricks App log capture.
* All bearer tokens, Telegram bot tokens, Postgres URIs with embedded
  passwords, and obvious "secret-like" strings are redacted before
  the record reaches stderr.
* Preserve the message structure for ``logger.exception(...)``.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

_REDACT_PATTERNS = [
    # Bearer tokens / standard Authorization headers
    re.compile(r"(?i)(Bearer\s+)([A-Za-z0-9._\-+/=]{16,})"),
    # Telegram bot tokens: <digits>:<base64-ish>
    re.compile(r"\b(\d{6,12}):([A-Za-z0-9_\-]{30,})\b"),
    # Postgres URIs with embedded credentials
    re.compile(r"(postgres(?:ql)?://[^:]+:)([^@/\s]+)(@)"),
    # AWS-style key=secret pairs in query strings
    re.compile(r"(?i)((?:secret|password|api[_-]?key|token)\s*=\s*[\"']?)([A-Za-z0-9._\-+/=]{8,})"),
]

_REPLACEMENT = r"\1<redacted>"


def _redact_str(s: str) -> str:
    out = s
    for pat in _REDACT_PATTERNS:
        out = pat.sub(_REPLACEMENT, out)
    return out


def redact(value: Any) -> Any:
    """Best-effort redaction of strings and stringifiable values."""
    if isinstance(value, str):
        return _redact_str(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        out = [redact(v) for v in value]
        return type(value)(out)
    return value


class RedactingFormatter(logging.Formatter):
    """Emit one-line JSON records with redaction applied to common fields."""

    def __init__(self, *, level_field: str = "level") -> None:
        super().__init__()
        self.level_field = level_field

    def format(self, record: logging.LogRecord) -> str:
        try:
            base_message = record.getMessage()
        except Exception:
            base_message = str(record.msg)

        payload: dict[str, Any] = {
            "ts": round(time.time(), 3),
            self.level_field: record.levelname,
            "logger": record.name,
            "message": _redact_str(base_message),
        }

        if record.exc_info:
            payload["exc"] = _redact_str(self.formatException(record.exc_info))

        extras = getattr(record, "extras", None)
        if isinstance(extras, dict):
            payload["extras"] = redact(extras)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(cfg) -> None:
    """Install a single JSON stderr handler on the root + hermes loggers.

    Idempotent: re-running just replaces the existing handler.
    """
    level_name = getattr(cfg, "log_level", "INFO") or "INFO"
    level = getattr(logging, level_name.upper(), logging.INFO)

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(RedactingFormatter())

    for logger_name in (
        "",  # root
        "hermes_databricks",
        "hermes",
        "agent",
        "gateway",
        "tools",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
    ):
        logger = logging.getLogger(logger_name)
        logger.handlers = [handler]
        logger.setLevel(level)
        logger.propagate = False
