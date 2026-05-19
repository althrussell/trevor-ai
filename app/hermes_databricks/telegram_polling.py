"""Telegram long-polling channel for Hermes-on-Databricks.

Databricks Apps reject anonymous inbound traffic (workspace OAuth
gate), so Telegram webhooks aren't viable. Instead we run an outbound
``getUpdates`` long-poll loop in the supervisor's task group.

Capabilities:
  * Reads bot token and the primary user handle from Databricks Secrets.
  * Calls ``deleteWebhook`` at boot to neutralise any stale webhook.
  * Long-polls ``getUpdates`` with ``timeout=25`` and ``allowed_updates=
    ["message","edited_message"]``.
  * Allowlist enforcement: messages from non-allowed Telegram usernames
    are rejected with a friendly reply.
  * Hermes-side dispatcher invokes the runtime in ``asyncio.to_thread``.
  * Outbound ``sendMessage`` renders LLM markdown into Telegram's HTML
    subset (see ``telegram_format.md_to_telegram_html``) and falls back
    to markdown-stripped plain text if Telegram rejects the HTML.
  * Exponential backoff on transport errors; the loop never dies unless
    cancelled.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from hermes_databricks import config as cfg_mod
from hermes_databricks.telegram_format import md_to_telegram_html, strip_markdown

log = logging.getLogger("hermes_databricks.telegram_polling")

OnMessage = Callable[[int, str | None, str], Awaitable[str | None]]


@dataclass
class TelegramStatus:
    enabled: bool = True
    last_update_id: int | None = None
    last_poll_at: float | None = None
    last_send_at: float | None = None
    poll_iterations: int = 0
    poll_errors: int = 0
    rejected_messages: int = 0
    delivered_messages: int = 0
    received_messages: int = 0
    bot_username: str | None = None
    allowed_usernames: list[str] = field(default_factory=list)
    primary_user_handle: str | None = None
    last_error: str | None = None


class TelegramClient:
    """Outbound long-polling client for Telegram Bot API."""

    BASE_URL = "https://api.telegram.org/bot{token}"

    def __init__(
        self,
        token: str,
        *,
        primary_user_handle: str | None = None,
        allowed_usernames: list[str] | None = None,
        long_poll_timeout: int = 25,
    ) -> None:
        if not token:
            raise ValueError("Telegram bot token is required")
        self.token = token
        self.primary_user_handle = (primary_user_handle or "").lstrip("@") or None
        # Normalise the allowlist: lowercase, drop leading '@'.
        raw_allowed = allowed_usernames or []
        if isinstance(raw_allowed, str):
            raw_allowed = [u.strip() for u in raw_allowed.split(",")]
        self.allowed_usernames = sorted({u.lstrip("@").lower() for u in raw_allowed if u})
        if self.primary_user_handle:
            self.allowed_usernames = sorted(
                set(self.allowed_usernames) | {self.primary_user_handle.lower()}
            )
        self.long_poll_timeout = long_poll_timeout
        self.api = self.BASE_URL.format(token=self.token)
        self._status = TelegramStatus(
            primary_user_handle=self.primary_user_handle,
            allowed_usernames=self.allowed_usernames,
        )
        self._stopping = False

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_secrets(cls, secrets_scope: str) -> TelegramClient | None:
        token = cfg_mod.get_secret(secrets_scope, "telegram_bot_token")
        if not token:
            log.info("telegram_bot_token not in scope %s; Telegram disabled", secrets_scope)
            return None
        primary = cfg_mod.get_secret(secrets_scope, "telegram_primary_user_handle")
        allowed_raw = cfg_mod.get_secret(secrets_scope, "telegram_allowed_users") or ""
        allowed = [u for u in (s.strip() for s in allowed_raw.split(",")) if u]
        return cls(token=token, primary_user_handle=primary, allowed_usernames=allowed)

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    async def get_me(self, client: httpx.AsyncClient) -> dict[str, Any] | None:
        try:
            r = await client.get(f"{self.api}/getMe")
            r.raise_for_status()
            data = r.json()
            if data.get("ok"):
                return data.get("result")
        except Exception:
            log.exception("Telegram getMe failed")
        return None

    async def delete_webhook(self, client: httpx.AsyncClient | None = None) -> None:
        async def _do(c: httpx.AsyncClient) -> None:
            try:
                r = await c.get(
                    f"{self.api}/deleteWebhook", params={"drop_pending_updates": "false"}
                )
                r.raise_for_status()
            except Exception:
                log.exception("Telegram deleteWebhook failed (continuing)")

        if client is not None:
            await _do(client)
        else:
            async with httpx.AsyncClient(timeout=10) as c:
                await _do(c)

    async def send_message(
        self, chat_id: int, text: str, *, parse_mode: str | None = "HTML"
    ) -> bool:
        """Send a text message, falling back to plain text on parse errors.

        When ``parse_mode`` is ``"HTML"`` (the default), the input is
        treated as LLM-style markdown and rendered into Telegram's HTML
        subset. Pass ``parse_mode=None`` to send literal plain text
        (used for system-generated messages like allowlist denials).
        """
        if not text:
            return False
        # Telegram caps messages at 4096 characters. We chunk the
        # *rendered* output so HTML tags don't get sliced; tags are
        # small so a conservative 4000-char ceiling is safe enough.
        if parse_mode == "HTML":
            rendered = md_to_telegram_html(text)
            fallback = strip_markdown(text)
        else:
            rendered = text
            fallback = text

        max_chunk = 4000
        rendered_chunks = [
            rendered[i : i + max_chunk] for i in range(0, len(rendered), max_chunk)
        ] or [""]
        fallback_chunks = [
            fallback[i : i + max_chunk] for i in range(0, len(fallback), max_chunk)
        ] or [""]

        ok_all = True
        async with httpx.AsyncClient(timeout=30) as client:
            for idx, chunk in enumerate(rendered_chunks):
                payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                ok = await self._post_send(client, payload)
                if not ok and parse_mode:
                    # Telegram rejected our formatted payload — retry the
                    # same chunk's plain-text equivalent with markdown
                    # markers stripped so the user doesn't see raw ``**``.
                    plain = fallback_chunks[idx] if idx < len(fallback_chunks) else chunk
                    payload = {"chat_id": chat_id, "text": plain}
                    ok = await self._post_send(client, payload)
                ok_all = ok_all and ok
        if ok_all:
            self._status.last_send_at = time.time()
            self._status.delivered_messages += 1
        return ok_all

    async def _post_send(self, client: httpx.AsyncClient, payload: dict[str, Any]) -> bool:
        try:
            r = await client.post(f"{self.api}/sendMessage", json=payload)
            if r.status_code >= 400:
                self._status.last_error = f"sendMessage HTTP {r.status_code}: {r.text[:200]}"
                log.warning("Telegram sendMessage failed: %s", self._status.last_error)
                return False
            data = r.json()
            if not data.get("ok"):
                self._status.last_error = f"sendMessage not ok: {data}"
                log.warning("Telegram sendMessage not ok: %s", data)
                return False
            return True
        except Exception as exc:
            self._status.last_error = f"sendMessage exc: {exc}"
            log.exception("Telegram sendMessage raised")
            return False

    # ------------------------------------------------------------------
    # Long polling
    # ------------------------------------------------------------------

    async def poll_loop(self, on_message: OnMessage) -> None:
        await self.delete_webhook()
        offset: int | None = None
        backoff = 1.0

        async with httpx.AsyncClient(timeout=self.long_poll_timeout + 10) as client:
            me = await self.get_me(client)
            if me:
                self._status.bot_username = me.get("username")

            while not self._stopping:
                try:
                    params: dict[str, Any] = {
                        "timeout": self.long_poll_timeout,
                        "allowed_updates": '["message","edited_message"]',
                    }
                    if offset is not None:
                        params["offset"] = offset

                    r = await client.get(f"{self.api}/getUpdates", params=params)
                    if r.status_code >= 400:
                        raise RuntimeError(f"getUpdates HTTP {r.status_code}: {r.text[:200]}")
                    data = r.json()
                    if not data.get("ok"):
                        raise RuntimeError(f"getUpdates not ok: {data}")

                    self._status.poll_iterations += 1
                    self._status.last_poll_at = time.time()
                    backoff = 1.0  # success → reset

                    updates = data.get("result", []) or []
                    for update in updates:
                        update_id = update.get("update_id")
                        if update_id is not None:
                            offset = update_id + 1
                            self._status.last_update_id = update_id

                        msg = update.get("message") or update.get("edited_message")
                        if not msg:
                            continue
                        await self._dispatch(client, msg, on_message)

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._status.poll_errors += 1
                    self._status.last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("telegram poll iteration failed; backing off")
                    await asyncio.sleep(min(backoff, 60))
                    backoff = min(backoff * 2, 60)

    async def _dispatch(
        self, client: httpx.AsyncClient, msg: dict[str, Any], on_message: OnMessage
    ) -> None:
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        text = msg.get("text") or msg.get("caption") or ""
        sender = msg.get("from") or {}
        username = sender.get("username")
        self._status.received_messages += 1

        if not self._is_allowed(username):
            self._status.rejected_messages += 1
            log.warning(
                "Rejecting Telegram message from non-allowlisted user",
                extra={
                    "extras": {
                        "username": username,
                        "primary": self.primary_user_handle,
                        "allowed": self.allowed_usernames,
                    }
                },
            )
            await self.send_message(
                chat_id,
                "Sorry — this Hermes instance is restricted. Your username "
                "is not on the allowlist.",
                parse_mode=None,
            )
            return

        if not text:
            await self.send_message(
                chat_id,
                "I can only handle text messages right now.",
                parse_mode=None,
            )
            return

        try:
            reply = await on_message(chat_id, username, text)
        except Exception as exc:
            log.exception("on_message handler raised")
            reply = f"Agent error: {type(exc).__name__}: {exc}"
        if reply:
            await self.send_message(chat_id, str(reply))

    def _is_allowed(self, username: str | None) -> bool:
        if not self.allowed_usernames and not self.primary_user_handle:
            # No allowlist configured → reject by default (least privilege).
            return False
        if not username:
            return False
        return username.lower() in {u.lower() for u in self.allowed_usernames}

    # ------------------------------------------------------------------
    # Lifecycle / status
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        self._stopping = True

    def status(self) -> dict[str, Any]:
        s = self._status
        return {
            "enabled": s.enabled,
            "bot_username": s.bot_username,
            "primary_user_handle": s.primary_user_handle,
            "allowed_usernames": s.allowed_usernames,
            "last_update_id": s.last_update_id,
            "last_poll_at": s.last_poll_at,
            "last_send_at": s.last_send_at,
            "poll_iterations": s.poll_iterations,
            "poll_errors": s.poll_errors,
            "received_messages": s.received_messages,
            "delivered_messages": s.delivered_messages,
            "rejected_messages": s.rejected_messages,
            "last_error": s.last_error,
            "stopping": self._stopping,
        }

    async def health_probe(self) -> dict[str, Any]:
        s = self._status
        # Healthy if we polled in the last 2x long_poll_timeout window.
        threshold = (self.long_poll_timeout or 25) * 2 + 5
        now = time.time()
        if s.last_poll_at is None:
            return {"status": "starting", "reason": "no poll iterations yet"}
        if now - s.last_poll_at > threshold:
            return {
                "status": "degraded",
                "reason": f"last poll was {round(now - s.last_poll_at, 1)}s ago",
                "errors": s.poll_errors,
            }
        return {
            "status": "ok",
            "bot_username": s.bot_username,
            "polled_seconds_ago": round(now - s.last_poll_at, 1),
        }
