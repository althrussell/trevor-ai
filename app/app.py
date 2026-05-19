"""Hermes-on-Databricks FastAPI entrypoint.

This module is the entrypoint declared in ``app.yaml`` (``uvicorn
app:app``). The lifespan manager constructs the runtime supervisor,
which is responsible for:

  * Mounting the UC Volume HERMES_HOME mirror
  * Constructing the LakebaseSessionDB (Phase 4)
  * Bootstrapping the full Hermes runtime (Phase 2)
  * Wiring the Databricks model provider (Phase 3)
  * Starting the Telegram poller (Phase 6)
  * Scheduling cron + heartbeat tasks (Phases 8/9)

Each phase plugs into the same ``HermesSupervisor`` so we always boot
to the most-complete state available — even if Hermes itself is not
installed yet, the app still serves /health and /debug/runtime.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from hermes_databricks import config as cfg_mod
from hermes_databricks.health import HealthRegistry
from hermes_databricks.observability.logging import configure_logging

# Supervisor is lazy-imported in lifespan so the module imports cleanly
# even when some optional deps (Hermes itself, databricks-sdk) are
# missing in the local-dev environment.

log = logging.getLogger("hermes_databricks.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = cfg_mod.load()
    configure_logging(cfg)

    health = HealthRegistry()
    app.state.config = cfg
    app.state.health = health
    app.state.started_at = time.time()
    app.state.runtime = None
    app.state.supervisor = None
    app.state.errors: dict[str, str] = {}

    try:
        from hermes_databricks.supervisor import HermesSupervisor
    except Exception as exc:
        log.error("Supervisor import failed; serving in degraded mode", exc_info=True)
        app.state.errors["supervisor_import"] = f"{type(exc).__name__}: {exc}"
        try:
            yield
        finally:
            pass
        return

    supervisor = HermesSupervisor(cfg=cfg, health=health)
    app.state.supervisor = supervisor

    try:
        await supervisor.start()
        app.state.runtime = supervisor.runtime
    except Exception as exc:
        log.error("Supervisor.start() failed; serving in degraded mode", exc_info=True)
        app.state.errors["supervisor_start"] = f"{type(exc).__name__}: {exc}"

    try:
        yield
    finally:
        if app.state.supervisor is not None:
            try:
                await app.state.supervisor.stop()
            except Exception:
                log.exception("Supervisor.stop() raised")


app = FastAPI(
    title="hermes-on-databricks",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------
# Core endpoints
# ---------------------------------------------------------------------


@app.get("/")
async def root(request: Request) -> dict[str, Any]:
    cfg = request.app.state.config
    return {
        "app": "hermes-on-databricks",
        "agent_name": cfg.agent_name,
        "model_endpoint": cfg.llm_endpoint,
        "lakebase_instance": cfg.lakebase_instance,
        "lakebase_schema": cfg.lakebase_schema,
        "hermes_home_path": str(cfg.hermes_home),
        "hermes_home_volume_path": cfg.hermes_home_volume_path,
        "artifacts_volume_path": cfg.artifacts_volume_path,
        "version": "0.1.0",
    }


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    return await request.app.state.health.liveness()


@app.get("/ready")
async def ready(request: Request) -> JSONResponse:
    payload = await request.app.state.health.readiness()
    status_code = 200 if payload["status"] in ("ok", "starting") else 503
    return JSONResponse(payload, status_code=status_code)


@app.get("/config")
async def config_endpoint(request: Request) -> dict[str, Any]:
    cfg = request.app.state.config
    return {
        "agent_name": cfg.agent_name,
        "catalog": cfg.catalog,
        "schema": cfg.schema,
        "hermes_home_volume": cfg.hermes_home_volume,
        "artifacts_volume": cfg.artifacts_volume,
        "secrets_scope": cfg.secrets_scope,
        "lakebase_instance": cfg.lakebase_instance,
        "lakebase_schema": cfg.lakebase_schema,
        "llm_endpoint": cfg.llm_endpoint,
        "warehouse_id": cfg.warehouse_id,
        "daily_token_cap": cfg.daily_token_cap,
        "heartbeat_seconds": cfg.heartbeat_seconds,
        "telegram_enabled": cfg.telegram_enabled,
        "terminal_backend": cfg.terminal_backend,
        "browser_backend": cfg.browser_backend,
        "mcp_enabled": cfg.mcp_enabled,
        "hermes_home": str(cfg.hermes_home),
    }


# ---------------------------------------------------------------------
# Debug endpoints (read-only; degrade gracefully)
# ---------------------------------------------------------------------


def _runtime_or_none(request: Request):
    return getattr(request.app.state, "runtime", None)


def _supervisor_or_none(request: Request):
    return getattr(request.app.state, "supervisor", None)


@app.get("/debug/runtime")
async def debug_runtime(request: Request) -> dict[str, Any]:
    runtime = _runtime_or_none(request)
    info: dict[str, Any] = {
        "started_at": getattr(request.app.state, "started_at", None),
        "uptime_seconds": round(
            time.time() - getattr(request.app.state, "started_at", time.time()), 2
        ),
        "errors": getattr(request.app.state, "errors", {}),
        "python_version": sys.version,
        "pid": os.getpid(),
    }
    if runtime is None:
        info["hermes"] = {"status": "not_initialized"}
        return info
    info["hermes"] = runtime.status()
    return info


@app.get("/hermes/status")
async def hermes_status(request: Request) -> dict[str, Any]:
    runtime = _runtime_or_none(request)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Hermes runtime not initialized")
    return runtime.status()


@app.post("/debug/model-turn")
async def debug_model_turn(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    runtime = _runtime_or_none(request)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Hermes runtime not initialized")
    message = payload.get("message")
    if not message:
        raise HTTPException(status_code=400, detail="payload.message is required")
    session_id = payload.get("session_id")
    system = payload.get("system")
    try:
        result = await asyncio.to_thread(
            runtime.run_turn,
            user_message=str(message),
            session_id=session_id,
            system_message=system,
        )
    except Exception as exc:
        log.exception("model turn failed")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    return result


@app.get("/debug/sessions")
async def debug_sessions(request: Request, limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    runtime = _runtime_or_none(request)
    if runtime is None or runtime.session_db is None:
        raise HTTPException(status_code=503, detail="Session DB not initialized")
    sessions = await asyncio.to_thread(runtime.session_db.list_sessions_rich, None, None, limit, 0)
    return {"count": len(sessions), "sessions": sessions}


@app.get("/debug/session/{session_id}")
async def debug_session(session_id: str, request: Request) -> dict[str, Any]:
    runtime = _runtime_or_none(request)
    if runtime is None or runtime.session_db is None:
        raise HTTPException(status_code=503, detail="Session DB not initialized")
    session = await asyncio.to_thread(runtime.session_db.get_session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")
    messages = await asyncio.to_thread(runtime.session_db.get_messages, session_id)
    return {"session": session, "messages": messages}


@app.get("/debug/tools")
async def debug_tools(request: Request) -> dict[str, Any]:
    runtime = _runtime_or_none(request)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Hermes runtime not initialized")
    return runtime.tool_summary()


@app.get("/debug/events")
async def debug_events(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    kind: str | None = Query(
        None, description="Filter by event kind (e.g. cron_tick, telegram_update)."
    ),
) -> dict[str, Any]:
    runtime = _runtime_or_none(request)
    if runtime is None or runtime.session_db is None:
        raise HTTPException(status_code=503, detail="Session DB not initialized")
    events = await asyncio.to_thread(runtime.session_db.recent_events, limit, kind)
    return {"count": len(events), "events": events, "kind_filter": kind}


@app.get("/debug/usage")
async def debug_usage(request: Request, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    runtime = _runtime_or_none(request)
    if runtime is None or runtime.session_db is None:
        raise HTTPException(status_code=503, detail="Session DB not initialized")
    rows = await asyncio.to_thread(runtime.session_db.recent_usage, limit)
    return {"count": len(rows), "usage": rows}


@app.get("/debug/fs")
async def debug_fs(request: Request) -> dict[str, Any]:
    runtime = _runtime_or_none(request)
    if runtime is None or runtime.home_fs is None:
        raise HTTPException(status_code=503, detail="UC Volume FS not initialized")
    return runtime.home_fs.status()


@app.get("/debug/fs/list")
async def debug_fs_list(
    request: Request, path: str = Query("", description="Relative path under HERMES_HOME")
) -> dict[str, Any]:
    runtime = _runtime_or_none(request)
    if runtime is None or runtime.home_fs is None:
        raise HTTPException(status_code=503, detail="UC Volume FS not initialized")
    entries = await asyncio.to_thread(runtime.home_fs.list, path)
    return {"path": path, "entries": entries}


@app.get("/debug/cron")
async def debug_cron(request: Request) -> dict[str, Any]:
    runtime = _runtime_or_none(request)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Hermes runtime not initialized")
    return runtime.cron_summary()


@app.post("/debug/cron/run/{job_id}")
async def debug_cron_run(job_id: str, request: Request) -> dict[str, Any]:
    runtime = _runtime_or_none(request)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Hermes runtime not initialized")
    try:
        return await asyncio.to_thread(runtime.run_cron_job, job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"cron job {job_id} not found") from None
    except Exception as exc:
        log.exception("cron job run failed")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@app.get("/debug/telegram")
async def debug_telegram(request: Request) -> dict[str, Any]:
    supervisor = _supervisor_or_none(request)
    if supervisor is None or supervisor.telegram is None:
        return {"status": "disabled", "reason": "Telegram client not configured"}
    return supervisor.telegram.status()


@app.get("/debug/supervisor")
async def debug_supervisor(request: Request) -> dict[str, Any]:
    supervisor = _supervisor_or_none(request)
    if supervisor is None:
        return {"status": "not_initialized"}
    tasks = []
    for t in getattr(supervisor, "_tasks", []):
        tasks.append(
            {
                "name": t.get_name(),
                "done": t.done(),
                "cancelled": t.cancelled(),
                "exception": (
                    type(t.exception()).__name__
                    if t.done() and not t.cancelled() and t.exception() is not None
                    else None
                ),
            }
        )
    return {
        "stopped": getattr(supervisor, "_stopped", False),
        "runtime_loaded": getattr(supervisor, "runtime", None) is not None,
        "telegram_loaded": getattr(supervisor, "telegram", None) is not None,
        "tasks": tasks,
    }


@app.get("/debug/health-checks")
async def debug_health_checks(request: Request) -> dict[str, Any]:
    health = request.app.state.health
    out = []
    for name, check in health._checks.items():  # type: ignore[attr-defined]
        out.append(
            {
                "name": name,
                "description": check.description,
                "last_status": check.last_status,
                "last_detail": check.last_detail,
                "last_ms": round(check.last_ms, 2),
                "last_checked_at": check.last_checked_at,
            }
        )
    return {"count": len(out), "checks": out}
