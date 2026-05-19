"""Health and readiness reporting for the Databricks App."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class HealthCheck:
    """A single named subsystem probe."""

    name: str
    fn: Callable[[], Awaitable[dict[str, Any]] | dict[str, Any]]
    description: str = ""
    last_status: str = "unknown"
    last_detail: dict[str, Any] = field(default_factory=dict)
    last_ms: float = 0.0
    last_checked_at: float = 0.0

    async def run(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            result = self.fn()
            if asyncio.iscoroutine(result):
                result = await result
            status = "ok"
            detail = result if isinstance(result, dict) else {"value": result}
        except Exception as exc:
            status = "error"
            detail = {"error": type(exc).__name__, "message": str(exc)}
        finally:
            self.last_ms = (time.perf_counter() - started) * 1000.0
            self.last_checked_at = time.time()

        self.last_status = status
        self.last_detail = detail
        return {
            "name": self.name,
            "status": status,
            "detail": detail,
            "elapsed_ms": round(self.last_ms, 2),
            "checked_at": self.last_checked_at,
        }


class HealthRegistry:
    """A registry of named health checks the supervisor populates at boot."""

    def __init__(self) -> None:
        self._checks: dict[str, HealthCheck] = {}
        self._started_at = time.time()

    def register(
        self,
        name: str,
        fn: Callable[[], Awaitable[dict[str, Any]] | dict[str, Any]],
        description: str = "",
    ) -> HealthCheck:
        check = HealthCheck(name=name, fn=fn, description=description)
        self._checks[name] = check
        return check

    def unregister(self, name: str) -> None:
        self._checks.pop(name, None)

    def get(self, name: str) -> HealthCheck | None:
        return self._checks.get(name)

    async def liveness(self) -> dict[str, Any]:
        return {
            "status": "alive",
            "uptime_seconds": round(time.time() - self._started_at, 2),
        }

    async def readiness(self) -> dict[str, Any]:
        results = []
        if self._checks:
            results = await asyncio.gather(*(c.run() for c in self._checks.values()))
        overall = "ok" if all(r["status"] == "ok" for r in results) else "degraded"
        if not results:
            overall = "starting"
        return {
            "status": overall,
            "uptime_seconds": round(time.time() - self._started_at, 2),
            "checks": results,
        }
