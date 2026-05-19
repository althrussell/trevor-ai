"""Telegram long-polling channel (Phase 1 skeleton; full in Phase 6)."""

from __future__ import annotations

from typing import Awaitable, Callable, Optional


OnMessage = Callable[[int, Optional[str], str], Awaitable[Optional[str]]]


class TelegramClient:  # pragma: no cover - Phase 1 stub
    @classmethod
    def from_secrets(cls, secrets_scope: str) -> Optional["TelegramClient"]:
        return None

    async def poll_loop(self, on_message: OnMessage) -> None:
        return None

    async def send_message(self, chat_id: int, text: str) -> None:
        return None

    def status(self) -> dict:
        return {"status": "unavailable", "reason": "Phase 6 not yet wired"}

    async def health_probe(self) -> dict:
        return {"status": "unavailable", "reason": "Phase 6 not yet wired"}
