"""Async supervisor that owns the Hermes runtime lifecycle inside the App.

The supervisor is built incrementally across phases:

* Phase 1: minimal start/stop, registers a baseline health check.
* Phase 2: bootstraps Hermes (``HermesRuntime``).
* Phase 3: wires the Databricks model provider.
* Phase 4: constructs the LakebaseSessionDB and registers its health check.
* Phase 5: pulls the UC Volume mirror into the local cache and starts
  the periodic UC sync task.
* Phase 6: starts the Telegram poller as an asyncio task.
* Phase 8: starts the cron tick task.
* Phase 9: registers all subsystem health checks against /ready.
"""

from __future__ import annotations

import asyncio
import logging

from hermes_databricks.config import Config
from hermes_databricks.health import HealthRegistry

log = logging.getLogger("hermes_databricks.supervisor")


class HermesSupervisor:
    """Owns the runtime + background tasks for the Databricks App."""

    def __init__(self, cfg: Config, health: HealthRegistry) -> None:
        self.cfg = cfg
        self.health = health
        self.runtime = None  # type: ignore[assignment]
        self.telegram = None  # type: ignore[assignment]
        self._tasks: list[asyncio.Task] = []
        self._stopped = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        log.info("Supervisor starting", extra={"extras": {"phase": "boot"}})
        self.health.register(
            "supervisor",
            self._check_self,
            description="The supervisor task itself is alive.",
        )

        await self._start_runtime()
        await self._start_telegram()
        await self._start_periodic_tasks()

        log.info(
            "Supervisor started",
            extra={
                "extras": {
                    "telegram": self.telegram is not None,
                    "runtime": self.runtime is not None,
                    "tasks": len(self._tasks),
                }
            },
        )

        await self._record_event(
            "app_startup",
            {
                "agent": self.cfg.agent_name,
                "model": self.cfg.llm_endpoint,
                "telegram_enabled": bool(self.telegram),
                "cron_available": bool(getattr(self.runtime, "cron_available", False))
                if self.runtime
                else False,
                "tasks": [t.get_name() for t in self._tasks],
            },
        )

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        log.info("Supervisor stopping")

        await self._record_event(
            "app_shutdown",
            {"agent": self.cfg.agent_name, "tasks": [t.get_name() for t in self._tasks]},
        )

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("background task raised on shutdown")
        self._tasks.clear()

        # Best-effort UC Volume sync on shutdown.
        if self.runtime is not None and getattr(self.runtime, "home_fs", None) is not None:
            try:
                await asyncio.to_thread(self.runtime.home_fs.sync_to_volume)
            except Exception:
                log.exception("UC Volume final sync failed")

        if self.runtime is not None:
            try:
                self.runtime.close()
            except Exception:
                log.exception("Runtime close failed")

        log.info("Supervisor stopped")

    # ------------------------------------------------------------------
    # Sub-tasks (each one is best-effort; failures are logged but don't
    # crash the app — degraded operation is still observable).
    # ------------------------------------------------------------------

    async def _start_runtime(self) -> None:
        try:
            from hermes_databricks.runtime import HermesRuntime
        except Exception:
            log.exception("HermesRuntime import failed; degraded mode")
            return

        try:
            runtime = HermesRuntime(self.cfg)
            await asyncio.to_thread(runtime.bootstrap)
            self.runtime = runtime
            self.health.register(
                "hermes_runtime",
                runtime.health_probe,
                description="Hermes AIAgent is initialised.",
            )
            if getattr(runtime, "session_db", None) is not None:
                self.health.register(
                    "lakebase",
                    runtime.session_db.health_probe,  # type: ignore[attr-defined]
                    description="Lakebase Postgres reachable.",
                )
            if getattr(runtime, "home_fs", None) is not None:
                self.health.register(
                    "uc_volume_home",
                    runtime.home_fs.health_probe,  # type: ignore[attr-defined]
                    description="UC Volume HERMES_HOME mirror reachable.",
                )
            self.health.register(
                "model_endpoint",
                runtime.health_probe_model,
                description="Databricks Model Serving endpoint is queryable.",
            )
        except Exception:
            log.exception("HermesRuntime bootstrap failed; degraded mode")

    async def _start_telegram(self) -> None:
        if not self.cfg.telegram_enabled:
            log.info("Telegram disabled by config")
            return
        if self.runtime is None:
            log.warning("Telegram cannot start: runtime not initialised")
            return

        try:
            from hermes_databricks.telegram_polling import TelegramClient
        except Exception:
            log.exception("Telegram client import failed")
            return

        try:
            client = TelegramClient.from_secrets(self.cfg.secrets_scope)
        except Exception:
            log.exception("Telegram client construction failed")
            return

        if client is None:
            log.info("Telegram secrets missing; Telegram will stay disabled")
            return

        self.telegram = client

        async def _on_message(chat_id: int, username: str | None, text: str) -> str | None:
            session_id = f"telegram:{chat_id}"
            await self._record_event(
                "telegram_update",
                {
                    "chat_id": chat_id,
                    "username": username,
                    "session_id": session_id,
                    "preview": (text or "")[:256],
                },
            )
            try:
                result = await asyncio.to_thread(
                    self.runtime.run_turn,
                    user_message=text,
                    session_id=session_id,
                    metadata={"telegram_chat_id": chat_id, "telegram_user": username},
                )
            except Exception as exc:
                log.exception("run_turn failed for telegram message")
                await self._record_event(
                    "telegram_run_error",
                    {
                        "chat_id": chat_id,
                        "session_id": session_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                return f"Agent error: {type(exc).__name__}: {exc}"
            await self._record_event(
                "telegram_reply",
                {
                    "chat_id": chat_id,
                    "session_id": session_id,
                    "mode": (result or {}).get("mode") if isinstance(result, dict) else None,
                    "usage": (result or {}).get("usage") if isinstance(result, dict) else None,
                },
            )
            return result.get("text") if isinstance(result, dict) else str(result)

        task = asyncio.create_task(client.poll_loop(_on_message), name="telegram_poll")
        self._tasks.append(task)
        self.health.register(
            "telegram_poll",
            client.health_probe,
            description="Telegram long-polling loop is alive.",
        )

    async def _start_periodic_tasks(self) -> None:
        # Heartbeat (lightweight self-check + UC Volume sync + token refresh)
        async def _heartbeat() -> None:
            # Refresh the Databricks bearer token at half the heartbeat
            # interval (capped at 30 minutes) so long-running App
            # containers never hand Hermes a stale token. Databricks
            # tokens are typically valid for ~1h, so 30m is a safe
            # default.
            token_refresh_interval = min(1800, max(60, self.cfg.heartbeat_seconds * 5))
            last_token_refresh = 0.0
            while True:
                await asyncio.sleep(self.cfg.heartbeat_seconds)
                if self.runtime is not None and getattr(self.runtime, "home_fs", None) is not None:
                    try:
                        await asyncio.to_thread(self.runtime.home_fs.sync_to_volume)
                    except Exception:
                        log.exception("UC Volume heartbeat sync failed")
                # Refresh Databricks bearer token if due.
                try:
                    import time as _t

                    now = _t.time()
                    if now - last_token_refresh >= token_refresh_interval and (
                        self.runtime is not None
                        and getattr(self.runtime, "provider", None) is not None
                        and getattr(self.runtime, "agent", None) is not None
                    ):
                        refreshed = await asyncio.to_thread(
                            self.runtime.provider.refresh_agent_token,
                            self.runtime.agent,
                        )
                        if refreshed:
                            last_token_refresh = now
                            log.debug("Databricks bearer token refreshed via heartbeat")
                            # Re-mint the MCP bearer too so the
                            # managed SQL MCP server keeps working
                            # past the 1h token expiry.
                            if self.cfg.mcp_enabled:
                                try:
                                    from hermes_databricks.mcp_bootstrap import (
                                        refresh_mcp_config,
                                    )

                                    mcp_status = await asyncio.to_thread(
                                        refresh_mcp_config, self.cfg
                                    )
                                    log.debug(
                                        "MCP config rewritten via heartbeat",
                                        extra={"extras": mcp_status},
                                    )
                                except Exception:
                                    log.exception("MCP config refresh failed")
                except Exception:
                    log.exception("Bearer-token refresh failed")
                if (
                    self.runtime is not None
                    and getattr(self.runtime, "session_db", None) is not None
                ):
                    try:
                        await asyncio.to_thread(
                            self.runtime.session_db.append_event,  # type: ignore[attr-defined]
                            "heartbeat",
                            {"agent": self.cfg.agent_name},
                        )
                    except Exception:
                        log.exception("heartbeat event persist failed")

        self._tasks.append(asyncio.create_task(_heartbeat(), name="heartbeat"))

        # Cron tick (Phase 8)
        if self.runtime is not None and getattr(self.runtime, "cron_available", False):

            async def _cron_tick() -> None:
                while True:
                    try:
                        await asyncio.to_thread(self.runtime.cron_tick)
                    except Exception:
                        log.exception("cron tick failed")
                    await asyncio.sleep(60)

            self._tasks.append(asyncio.create_task(_cron_tick(), name="cron_tick"))

    # ------------------------------------------------------------------
    # Event persistence (best-effort)
    # ------------------------------------------------------------------

    async def _record_event(self, kind: str, payload: dict) -> None:
        if self.runtime is None or getattr(self.runtime, "session_db", None) is None:
            return
        try:
            await asyncio.to_thread(
                self.runtime.session_db.append_event,  # type: ignore[attr-defined]
                kind,
                payload,
            )
        except Exception:
            log.exception("event persist failed: %s", kind)

    # ------------------------------------------------------------------
    # Health probes
    # ------------------------------------------------------------------

    async def _check_self(self) -> dict:
        return {
            "tasks": [t.get_name() for t in self._tasks if not t.done()],
            "stopped": self._stopped,
        }
