"""Unit tests for the Telegram polling client.

Tests focus on the synchronous-or-easy bits: allowlist enforcement,
status reporting, and the dispatch routing that decides whether
``send_message`` and ``on_message`` get called.

Full HTTP mocking is intentionally avoided — ``httpx.AsyncClient`` is
context-managed inside several methods and patching every call site
is more brittle than directly testing ``_dispatch`` with a stubbed
``send_message``.
"""

from __future__ import annotations

import asyncio
from typing import List, Tuple

import pytest

from hermes_databricks import telegram_polling as tp


def _client(allowed=("alice",), primary="alice") -> tp.TelegramClient:
    return tp.TelegramClient(
        token="1234567890:" + "x" * 35,
        primary_user_handle=primary,
        allowed_usernames=list(allowed),
    )


def test_constructor_rejects_empty_token():
    with pytest.raises(ValueError):
        tp.TelegramClient(token="")


def test_allowlist_normalises_at_prefix_and_case():
    c = tp.TelegramClient(token="t", primary_user_handle="@Alice", allowed_usernames=["@bob", "Carol"])
    # primary user joins the allowlist automatically
    assert "alice" in c.allowed_usernames
    assert "bob" in c.allowed_usernames
    assert "carol" in c.allowed_usernames


def test_is_allowed_rejects_empty_allowlist():
    c = tp.TelegramClient(token="t", primary_user_handle=None, allowed_usernames=[])
    assert c._is_allowed("anyone") is False
    assert c._is_allowed(None) is False


def test_is_allowed_accepts_primary():
    c = _client()
    assert c._is_allowed("alice") is True
    assert c._is_allowed("Alice") is True
    assert c._is_allowed("@alice") is False  # caller is expected to pass bare username


def test_is_allowed_rejects_others():
    c = _client()
    assert c._is_allowed("bob") is False


def test_status_default_shape():
    c = _client()
    s = c.status()
    assert s["enabled"] is True
    assert s["primary_user_handle"] == "alice"
    assert "alice" in s["allowed_usernames"]
    assert s["received_messages"] == 0
    assert s["delivered_messages"] == 0


@pytest.mark.asyncio
async def test_dispatch_rejects_unknown_user(monkeypatch):
    c = _client()
    sent: List[Tuple[int, str]] = []

    async def fake_send(chat_id, text, *, parse_mode=None):
        sent.append((chat_id, text))
        return True

    monkeypatch.setattr(c, "send_message", fake_send)

    received: List[str] = []

    async def cb(chat_id, username, text):
        received.append(text)
        return None

    await c._dispatch(None, {  # type: ignore[arg-type]
        "chat": {"id": 7},
        "from": {"username": "bob"},
        "text": "hello",
    }, cb)

    assert received == []
    assert sent and "not on the allowlist" in sent[-1][1]
    assert c.status()["rejected_messages"] == 1


@pytest.mark.asyncio
async def test_dispatch_calls_callback_for_allowed(monkeypatch):
    c = _client()
    sent: List[Tuple[int, str]] = []

    async def fake_send(chat_id, text, *, parse_mode=None):
        sent.append((chat_id, text))
        return True

    monkeypatch.setattr(c, "send_message", fake_send)

    received: List[Tuple[int, str, str]] = []

    async def cb(chat_id, username, text):
        received.append((chat_id, username, text))
        return f"echo: {text}"

    await c._dispatch(None, {  # type: ignore[arg-type]
        "chat": {"id": 9},
        "from": {"username": "alice"},
        "text": "ping",
    }, cb)

    assert received == [(9, "alice", "ping")]
    assert sent and "echo: ping" in sent[-1][1]


@pytest.mark.asyncio
async def test_dispatch_handles_callback_exception(monkeypatch):
    c = _client()
    sent: List[Tuple[int, str]] = []

    async def fake_send(chat_id, text, *, parse_mode=None):
        sent.append((chat_id, text))
        return True

    monkeypatch.setattr(c, "send_message", fake_send)

    async def cb(chat_id, username, text):
        raise RuntimeError("boom")

    await c._dispatch(None, {  # type: ignore[arg-type]
        "chat": {"id": 9},
        "from": {"username": "alice"},
        "text": "ping",
    }, cb)

    assert sent and "Agent error" in sent[-1][1]
    assert "boom" in sent[-1][1]


@pytest.mark.asyncio
async def test_dispatch_handles_non_text():
    c = _client()
    sent: List[Tuple[int, str]] = []

    async def fake_send(chat_id, text, *, parse_mode=None):
        sent.append((chat_id, text))
        return True

    c.send_message = fake_send  # type: ignore[assignment]

    async def cb(chat_id, username, text):
        return "should not run"

    await c._dispatch(None, {  # type: ignore[arg-type]
        "chat": {"id": 9},
        "from": {"username": "alice"},
        # no text, no caption
    }, cb)

    assert sent and "only handle text" in sent[-1][1]


@pytest.mark.asyncio
async def test_health_probe_starting_then_ok():
    import time
    c = _client()
    probe = await c.health_probe()
    assert probe["status"] == "starting"

    c._status.last_poll_at = time.time()
    probe = await c.health_probe()
    assert probe["status"] == "ok"
