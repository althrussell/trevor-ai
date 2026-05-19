"""HermesRuntime — owns the embedded Hermes AIAgent inside the App.

This module is grown across phases. The Phase 1 baseline only:
  * resolves config
  * reports its own status
  * does NOT yet import Hermes itself

Phase 2 adds the real ``AIAgent`` bootstrap.
Phase 3 wires the Databricks model provider.
Phase 4 attaches the Lakebase SessionDB.
Phase 5 mounts the UC Volume HERMES_HOME mirror.
Phase 7 attaches the Databricks tools + backend selectors.
Phase 8 surfaces the cron scheduler.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_databricks.config import Config


log = logging.getLogger("hermes_databricks.runtime")


class HermesRuntime:
    """The single owner of the embedded Hermes AIAgent inside the App."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._started_at = time.time()
        self._initialized = False

        # Subsystems (populated by bootstrap in later phases)
        self.home_fs = None  # UCVolumeHome
        self.session_db = None  # LakebaseSessionDB
        self.provider = None  # DatabricksOpenAIClientFactory
        self.agent = None  # hermes AIAgent (Phase 2)
        self.scheduler = None  # Hermes cron scheduler (Phase 8)
        self.cron_available: bool = False

        # Error capture for diagnostics
        self.errors: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def bootstrap(self) -> None:
        """Bring the runtime online. Safe to call once; idempotent on retry."""
        if self._initialized:
            return

        # Phase 5: UC Volume HERMES_HOME mirror
        try:
            from hermes_databricks.fs.volume_fs import UCVolumeHome
            self.home_fs = UCVolumeHome.from_config(self.cfg)
            self.home_fs.ensure_local_dirs()
            try:
                self.home_fs.sync_from_volume()
            except Exception:
                log.exception("UC Volume initial sync failed (continuing with empty cache)")
                self.errors["uc_volume_sync_initial"] = "see logs"
        except Exception as exc:
            log.exception("UCVolumeHome unavailable")
            self.errors["uc_volume"] = f"{type(exc).__name__}: {exc}"

        # Ensure HERMES_HOME env is set before anything in Hermes touches it.
        os.environ["HERMES_HOME"] = str(self.cfg.hermes_home)
        self.cfg.hermes_home.mkdir(parents=True, exist_ok=True)

        # Phase 4: Lakebase SessionDB
        try:
            from hermes_databricks.state.lakebase_session_db import LakebaseSessionDB
            self.session_db = LakebaseSessionDB.from_config(self.cfg)
            self.session_db.ensure_schema()
        except Exception as exc:
            log.exception("LakebaseSessionDB unavailable")
            self.errors["lakebase"] = f"{type(exc).__name__}: {exc}"

        # Phase 3: Databricks model provider
        try:
            from hermes_databricks.databricks_provider import DatabricksOpenAIClientFactory
            self.provider = DatabricksOpenAIClientFactory(endpoint=self.cfg.llm_endpoint)
            # Force a client construction so we surface obvious config errors early.
            _ = self.provider.client()
        except Exception as exc:
            log.exception("Databricks model provider unavailable")
            self.errors["model_provider"] = f"{type(exc).__name__}: {exc}"

        # Phase 2: Hermes AIAgent
        try:
            self._init_hermes()
        except Exception as exc:
            log.exception("Hermes runtime unavailable")
            self.errors["hermes"] = f"{type(exc).__name__}: {exc}"

        self._initialized = True

    def _init_hermes(self) -> None:
        """Construct the real Hermes ``AIAgent``.

        Wrapped so that import failures don't crash the App; the
        supervisor reports degraded mode through /ready and /debug/runtime.
        """
        # Late imports so Phase 1 deployments without hermes-agent
        # installed still boot a degraded App.
        try:
            from hermes_constants import (  # type: ignore
                set_hermes_home_override,
            )
            from hermes_cli.config import ensure_hermes_home  # type: ignore
            from hermes_logging import setup_logging as hermes_setup_logging  # type: ignore
            from run_agent import AIAgent  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"hermes-agent not importable: {exc}") from exc

        # 1. Pin Hermes' home to our cache (UC Volume mirror).
        try:
            set_hermes_home_override(str(self.cfg.hermes_home))
        except Exception:
            log.debug("set_hermes_home_override raised", exc_info=True)
        os.environ["HERMES_HOME"] = str(self.cfg.hermes_home)

        # 2. Make sure the home directory tree exists.
        try:
            ensure_hermes_home()
        except Exception:
            log.exception("ensure_hermes_home failed")

        # 3. Initialise Hermes' own logging in 'cli' mode (file rotation).
        try:
            hermes_setup_logging(
                hermes_home=self.cfg.hermes_home,
                mode="cli",
                force=True,
            )
        except Exception:
            log.exception("hermes_setup_logging failed (continuing)")

        # 4. Optionally register the 'databricks' provider profile so
        #    Hermes' CLI/introspection sees us as a first-class provider.
        try:
            from hermes_databricks.databricks_provider import register_provider_profile
            register_provider_profile(self.cfg)
        except Exception:
            log.debug("register_provider_profile failed (non-fatal)", exc_info=True)

        # 5. Construct AIAgent. We pass placeholder api_key/base_url so
        #    Hermes' OpenAI client construction succeeds; we then swap
        #    .client with the Workspace-authenticated one in Phase 3.
        from hermes_databricks.tools.backend_registry import build_toolset_selection
        enabled, disabled = build_toolset_selection(self.cfg)

        agent = AIAgent(
            base_url="https://placeholder.invalid/v1",
            api_key="placeholder-databricks",
            provider="databricks",
            model=self.cfg.llm_endpoint,
            enabled_toolsets=enabled,
            disabled_toolsets=disabled,
            session_db=self.session_db,
            quiet_mode=True,
            verbose_logging=False,
        )

        # 6. Phase 3: swap the OpenAI client for the workspace-authed one.
        if self.provider is not None:
            try:
                self.provider.apply_to_agent(agent)
            except Exception:
                log.exception("Failed to apply Databricks provider to AIAgent")
                self.errors["model_provider_apply"] = "see logs"

        # 7. Phase 7: register the Databricks-native toolset.
        try:
            from hermes_databricks.tools.databricks_toolset import register_databricks_toolset
            register_databricks_toolset(self.cfg, home_fs=self.home_fs)
        except Exception:
            log.exception("databricks toolset registration failed")
            self.errors["databricks_toolset"] = "see logs"

        # 8. Phase 8: surface the cron scheduler.
        try:
            from cron.scheduler import tick as _cron_tick  # type: ignore
            self.scheduler = _cron_tick
            self.cron_available = True
        except Exception:
            log.debug("cron scheduler unavailable (non-fatal)", exc_info=True)
            self.cron_available = False

        self.agent = agent
        log.info(
            "Hermes runtime initialised",
            extra={
                "extras": {
                    "model": self.cfg.llm_endpoint,
                    "hermes_home": str(self.cfg.hermes_home),
                    "session_db": "lakebase" if self.session_db is not None else "none",
                    "tools": len(enabled or []),
                }
            },
        )

    def close(self) -> None:
        if self.session_db is not None:
            try:
                self.session_db.close()
            except Exception:
                log.exception("SessionDB close failed")
        if self.home_fs is not None:
            try:
                self.home_fs.close()
            except Exception:
                log.exception("UCVolumeHome close failed")

    # ------------------------------------------------------------------
    # Public surface used by app.py
    # ------------------------------------------------------------------

    def run_turn(
        self,
        user_message: str,
        session_id: Optional[str] = None,
        system_message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if session_id is None:
            session_id = "debug:adhoc"

        # If Hermes itself isn't initialised, fall back to a direct
        # one-shot model call so /debug/model-turn still proves the
        # Databricks endpoint is reachable. The fallback uses no tools
        # and writes no Hermes-shaped session history.
        if self.agent is None:
            if self.provider is None:
                raise RuntimeError(
                    "Neither the Hermes AIAgent nor the Databricks "
                    "provider is initialised. See /debug/runtime.errors."
                )
            from hermes_databricks.databricks_provider import quick_chat
            payload = quick_chat(self.provider, message=user_message, system=system_message)
            payload["session_id"] = session_id
            payload["mode"] = "direct"
            payload["metadata"] = metadata or {}
            self._persist_direct_turn(session_id, user_message, payload, metadata or {})
            return payload

        # Persist source/user info first so the session row exists in
        # Lakebase regardless of Hermes' create-on-first-use behaviour.
        if self.session_db is not None:
            try:
                self.session_db.ensure_session(
                    session_id=session_id,
                    source=(metadata or {}).get("source", "debug"),
                    model=self.cfg.llm_endpoint,
                )
            except Exception:
                log.exception("ensure_session failed")

        agent = self.agent
        agent.session_id = session_id  # type: ignore[attr-defined]

        result = agent.run_conversation(
            user_message=user_message,
            system_message=system_message,
            conversation_history=None,
        )

        if isinstance(result, dict):
            text = result.get("response") or result.get("text") or result.get("content") or ""
            usage = result.get("usage") or {}
        else:
            text = str(result)
            usage = {}

        return {
            "session_id": session_id,
            "text": text,
            "usage": usage,
            "mode": "hermes",
            "metadata": metadata or {},
        }

    def _persist_direct_turn(
        self,
        session_id: str,
        user_message: str,
        payload: Dict[str, Any],
        metadata: Dict[str, Any],
    ) -> None:
        """Best-effort persistence for fallback direct-turn calls."""
        if self.session_db is None:
            return
        try:
            self.session_db.ensure_session(
                session_id=session_id,
                source=metadata.get("source", "direct"),
                model=self.cfg.llm_endpoint,
            )
            self.session_db.append_message(session_id, role="user", content=user_message)
            self.session_db.append_message(
                session_id,
                role="assistant",
                content=payload.get("text", ""),
                finish_reason=payload.get("finish_reason"),
            )
            usage = payload.get("usage") or {}
            self.session_db.update_token_counts(
                session_id,
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
                model=self.cfg.llm_endpoint,
                api_call_count=1,
            )
        except Exception:
            log.exception("direct-turn persistence failed")

    def status(self) -> Dict[str, Any]:
        try:
            import hermes_constants  # type: ignore
            hermes_version = getattr(hermes_constants, "__version__", None)
        except Exception:
            hermes_version = None

        try:
            from importlib.metadata import version as _pkg_version
            hermes_pkg_version = _pkg_version("hermes-agent")
        except Exception:
            hermes_pkg_version = None

        return {
            "initialized": self._initialized,
            "uptime_seconds": round(time.time() - self._started_at, 2),
            "errors": dict(self.errors),
            "hermes_version_module": hermes_version,
            "hermes_version_pkg": hermes_pkg_version,
            "provider": "databricks",
            "model_endpoint": self.cfg.llm_endpoint,
            "hermes_home": str(self.cfg.hermes_home),
            "agent_loaded": self.agent is not None,
            "session_db": type(self.session_db).__name__ if self.session_db else None,
            "home_fs": type(self.home_fs).__name__ if self.home_fs else None,
            "cron_available": self.cron_available,
            "python": sys.version.split()[0],
        }

    def tool_summary(self) -> Dict[str, Any]:
        from hermes_databricks.tools.backend_registry import describe_backends
        out = describe_backends(self.cfg)
        try:
            from tools.registry import registry as _registry  # type: ignore
            out["registered_tools"] = len(getattr(_registry, "_tools", {}))
        except Exception:
            out["registered_tools"] = None
        return out

    def cron_summary(self) -> Dict[str, Any]:
        if not self.cron_available:
            return {"available": False, "reason": "hermes cron unavailable"}
        try:
            from cron.jobs import load_jobs  # type: ignore
            jobs = load_jobs()
        except Exception as exc:
            return {"available": True, "error": f"{type(exc).__name__}: {exc}", "jobs": []}
        return {"available": True, "count": len(jobs), "jobs": jobs}

    def run_cron_job(self, job_id: str) -> Dict[str, Any]:
        if not self.cron_available:
            raise RuntimeError("Hermes cron scheduler unavailable")
        try:
            from cron.jobs import load_jobs, run_job  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"cron API unavailable: {exc}") from exc
        jobs = {j.get("id"): j for j in load_jobs() if isinstance(j, dict)}
        if job_id not in jobs:
            raise KeyError(job_id)
        result = run_job(jobs[job_id])
        return {"job_id": job_id, "result": result}

    # ------------------------------------------------------------------
    # Health probes
    # ------------------------------------------------------------------

    async def health_probe(self) -> Dict[str, Any]:
        return {
            "initialized": self._initialized,
            "agent_loaded": self.agent is not None,
            "errors": dict(self.errors),
        }

    async def health_probe_model(self) -> Dict[str, Any]:
        if self.provider is None:
            return {"status": "unavailable", "reason": "provider not initialised"}
        try:
            self.provider.client()  # cached but raises if creds bad
            return {"status": "ok", "endpoint": self.cfg.llm_endpoint}
        except Exception as exc:
            return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
