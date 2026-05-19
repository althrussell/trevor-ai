"""Unit tests for secret redaction in logging."""

from __future__ import annotations

import io
import json
import logging

from hermes_databricks.observability.logging import RedactingFormatter, redact


def _record(message: str, extras: dict | None = None) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="hermes_databricks.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    if extras is not None:
        rec.extras = extras
    return rec


def test_bearer_token_is_redacted() -> None:
    fmt = RedactingFormatter()
    out = fmt.format(_record("auth: Bearer abc123xyz_DEADBEEFAAA12345"))
    payload = json.loads(out)
    assert "DEADBEEFAAA12345" not in payload["message"]
    assert "redacted" in payload["message"]


def test_telegram_token_is_redacted() -> None:
    fmt = RedactingFormatter()
    out = fmt.format(_record("token: 1234567890:ABCDEFghijklmnopqrstuvwxyz0123456789"))
    payload = json.loads(out)
    assert "ABCDEFghijklmnopqrstuvwxyz0123456789" not in payload["message"]


def test_postgres_password_is_redacted() -> None:
    fmt = RedactingFormatter()
    raw = "postgres://user:supersecretpw@host:5432/db?sslmode=require"
    out = fmt.format(_record(raw))
    payload = json.loads(out)
    assert "supersecretpw" not in payload["message"]


def test_api_key_kv_is_redacted() -> None:
    fmt = RedactingFormatter()
    out = fmt.format(_record("setting api_key=XYZ_abcdefghij1234567890"))
    payload = json.loads(out)
    assert "XYZ_abcdefghij1234567890" not in payload["message"]


def test_extras_dict_is_redacted() -> None:
    fmt = RedactingFormatter()
    out = fmt.format(_record("hello", extras={"token": "Bearer SUPERSECRETOPENAITOKEN"}))
    payload = json.loads(out)
    assert "SUPERSECRETOPENAITOKEN" not in json.dumps(payload)


def test_redact_function_handles_nested() -> None:
    nested = {
        "list": ["Bearer SUPERSECRETOPENAITOKEN", "plain"],
        "tuple": ("postgres://u:topsecretpw@h/d",),
        "passthrough": 123,
    }
    redacted = redact(nested)
    serialised = json.dumps(redacted)
    assert "SUPERSECRETOPENAITOKEN" not in serialised
    assert "topsecretpw" not in serialised
    assert "plain" in serialised
    assert "123" in serialised
