"""TrevorRuntime — owns the embedded Hermes AIAgent inside the App.

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
from typing import Any

from trevor_databricks.config import Config

log = logging.getLogger("trevor_databricks.runtime")


class TrevorRuntime:
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
        self._cron_tick_count: int = 0
        self._cron_last_tick_at: float | None = None
        self._cron_last_executed: int = 0
        self._cron_last_error: str | None = None

        # Error capture for diagnostics
        self.errors: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def bootstrap(self) -> None:
        """Bring the runtime online. Safe to call once; idempotent on retry."""
        if self._initialized:
            return

        # Phase 5: UC Volume HERMES_HOME mirror
        try:
            from pathlib import Path as _Path

            from trevor_databricks.fs.volume_fs import UCVolumeHome

            self.home_fs = UCVolumeHome.from_config(self.cfg)
            self.home_fs.ensure_local_dirs()
            try:
                self.home_fs.sync_from_volume()
            except Exception:
                log.exception("UC Volume initial sync failed (continuing with empty cache)")
                self.errors["uc_volume_sync_initial"] = "see logs"

            try:
                seed_root = _Path(__file__).resolve().parent.parent / "seeds" / "skills"
                if seed_root.exists():
                    for seed_dir in sorted(p for p in seed_root.iterdir() if p.is_dir()):
                        target = f"skills/{seed_dir.name}"
                        seed_result = self.home_fs.seed_if_empty(seed_dir, target)
                        log.info(
                            "skill seed pass",
                            extra={"extras": seed_result},
                        )
            except Exception:
                log.exception("seed_if_empty pass failed (continuing without seeded skills)")
                self.errors["uc_volume_skill_seed"] = "see logs"

            # Seed top-level HERMES_HOME files (soul.md, etc.) shipped
            # under ``app/seeds/home/``.
            #
            # ``soul.md`` is the system prompt and is treated as a code
            # artifact — it lives in git, ships with the bundle, and
            # must always reflect the bundled copy on the next boot
            # after a redeploy. We force-overwrite that file. Every
            # other ``seeds/home/*`` file keeps the "seed if missing"
            # semantics so an operator can edit ``MEMORY.md`` or
            # ``config.yaml`` overrides on the volume without them being
            # clobbered.
            try:
                home_seed_root = _Path(__file__).resolve().parent.parent / "seeds" / "home"
                if home_seed_root.exists():
                    force_overwrite = {"soul.md"}
                    for seed_file in sorted(p for p in home_seed_root.iterdir() if p.is_file()):
                        if seed_file.name in force_overwrite:
                            result = self.home_fs.seed_file_force(seed_file, seed_file.name)
                        else:
                            result = self.home_fs.seed_file_if_missing(seed_file, seed_file.name)
                        log.info("HERMES_HOME file seed pass", extra={"extras": result})
            except Exception:
                log.exception("HERMES_HOME root file seed failed (continuing)")
                self.errors["uc_volume_home_seed"] = "see logs"
        except Exception as exc:
            log.exception("UCVolumeHome unavailable")
            self.errors["uc_volume"] = f"{type(exc).__name__}: {exc}"

        # Ensure HERMES_HOME env is set before anything in Hermes touches it.
        os.environ["HERMES_HOME"] = str(self.cfg.trevor_home)
        self.cfg.trevor_home.mkdir(parents=True, exist_ok=True)

        # MCP bootstrap: when enabled, write the Databricks managed
        # SQL MCP server into HERMES_HOME/config.yaml so the agent can
        # discover Unity Catalog tables/catalogs/schemas via MCP. Token
        # rotation is handled by the supervisor heartbeat.
        try:
            from trevor_databricks.mcp_bootstrap import refresh_mcp_config

            mcp_status = refresh_mcp_config(self.cfg)
            log.info("MCP bootstrap pass", extra={"extras": mcp_status})
            if self.cfg.mcp_enabled and not mcp_status.get("applied") and mcp_status.get("reason"):
                self.errors["mcp_bootstrap"] = str(mcp_status.get("reason"))
        except Exception as exc:
            log.exception("MCP bootstrap failed")
            self.errors["mcp_bootstrap"] = f"{type(exc).__name__}: {exc}"

        # Phase 4: Lakebase SessionDB
        try:
            from trevor_databricks.state.lakebase_session_db import LakebaseSessionDB

            self.session_db = LakebaseSessionDB.from_config(self.cfg)
            self.session_db.ensure_schema()
        except Exception as exc:
            log.exception("LakebaseSessionDB unavailable")
            self.errors["lakebase"] = f"{type(exc).__name__}: {exc}"

        # Phase 3: Databricks model provider
        try:
            from trevor_databricks.databricks_provider import DatabricksOpenAIClientFactory

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
        #
        # IMPORTANT: ``HERMES_HOME`` MUST be set in the environment
        # before importing any ``hermes_*`` module, because
        # ``hermes_constants.get_trevor_home()`` is called eagerly at
        # import time from several submodules.
        os.environ["HERMES_HOME"] = str(self.cfg.trevor_home)
        try:
            self.cfg.trevor_home.mkdir(parents=True, exist_ok=True)
        except Exception:
            log.debug("trevor_home mkdir failed", exc_info=True)

        try:
            from hermes_cli.config import ensure_trevor_home  # type: ignore
            from hermes_logging import setup_logging as hermes_setup_logging  # type: ignore
            from run_agent import AIAgent  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"hermes-agent not importable: {exc}") from exc

        # 2. Make sure the home directory tree exists.
        try:
            ensure_trevor_home()
        except Exception:
            log.exception("ensure_trevor_home failed")

        # 3. Initialise Hermes' own logging in 'cli' mode (file rotation).
        try:
            hermes_setup_logging(
                trevor_home=self.cfg.trevor_home,
                mode="cli",
                force=True,
            )
        except Exception:
            log.exception("hermes_setup_logging failed (continuing)")

        # 4. Optionally register the 'databricks' provider profile so
        #    Hermes' CLI/introspection sees us as a first-class provider.
        try:
            from trevor_databricks.databricks_provider import register_provider_profile

            register_provider_profile(self.cfg)
        except Exception:
            log.debug("register_provider_profile failed (non-fatal)", exc_info=True)

        # 5. Construct AIAgent. We pass placeholder api_key/base_url so
        #    Hermes' OpenAI client construction succeeds; we then swap
        #    .client with the Workspace-authenticated one in Phase 3.
        from trevor_databricks.tools.backend_registry import build_toolset_selection

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

            # 6b. Install the AIAgent.__init__ hook so any subagent
            # spawned by ``delegate_task`` re-runs ``apply_to_agent``
            # automatically. Without this, child agents stream against
            # Databricks Foundation Model serving (Hermes' SSE
            # accumulator crashes) and return ``(empty)`` after 3
            # retries — see the "list databases" 38-call spiral
            # post-mortem. Idempotent: the hook is class-level and
            # self-marked.
            try:
                self.provider.install_subagent_hook()
            except Exception:
                log.exception("Failed to install Databricks subagent hook")
                self.errors["model_provider_subagent_hook"] = "see logs"

        # 7. Phase 7: register the Databricks-native toolset.
        try:
            from trevor_databricks.tools.databricks_toolset import register_databricks_toolset

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
                    "trevor_home": str(self.cfg.trevor_home),
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
        session_id: str | None = None,
        system_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
            from trevor_databricks.databricks_provider import quick_chat

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

        # Load prior conversation history from Lakebase so the agent
        # actually remembers previous turns. Hermes' AIAgent will not
        # auto-load history from ``session_db`` in this wiring — we
        # have to feed it explicitly via ``conversation_history``.
        # We strip role=="system" because the caller (or the agent's
        # own prompt assembler) supplies the system message separately.
        prior_history: list[dict[str, Any]] | None = None
        if self.session_db is not None:
            try:
                history = self.session_db.get_messages_as_conversation(session_id)
                prior_history = [m for m in history if m.get("role") != "system"] or None
            except Exception:
                log.exception("Failed to load prior conversation history; continuing empty")

        agent = self.agent
        agent.session_id = session_id  # type: ignore[attr-defined]

        result = agent.run_conversation(
            user_message=user_message,
            system_message=system_message,
            conversation_history=prior_history,
        )

        # NOTE: Hermes' AIAgent persists its own messages via the
        # wired ``session_db`` (writing user/assistant/tool rows as
        # the turn progresses). We intentionally don't append them
        # again here to avoid duplicates. The prior_history load
        # above is what actually restores context across turns.

        if isinstance(result, dict):
            # Hermes' canonical key is ``final_response``. Older code
            # paths and our direct fallback use ``response``/``text``.
            text = (
                result.get("final_response")
                or result.get("response")
                or result.get("text")
                or result.get("content")
                or ""
            )
            usage = result.get("usage") or {}
            extra = {
                "api_calls": result.get("api_calls"),
                "completed": result.get("completed"),
                "partial": result.get("partial"),
                "error": result.get("error"),
                "messages_count": (
                    len(result["messages"]) if isinstance(result.get("messages"), list) else None
                ),
            }
        else:
            text = str(result)
            usage = {}
            extra = {}

        return {
            "session_id": session_id,
            "text": text,
            "usage": usage,
            "mode": "hermes",
            "metadata": metadata or {},
            "hermes": extra,
        }

    def _persist_direct_turn(
        self,
        session_id: str,
        user_message: str,
        payload: dict[str, Any],
        metadata: dict[str, Any],
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

    def status(self) -> dict[str, Any]:
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
            "trevor_home": str(self.cfg.trevor_home),
            "agent_loaded": self.agent is not None,
            "session_db": type(self.session_db).__name__ if self.session_db else None,
            "home_fs": type(self.home_fs).__name__ if self.home_fs else None,
            "cron_available": self.cron_available,
            "python": sys.version.split()[0],
        }

    def tool_summary(self) -> dict[str, Any]:
        from trevor_databricks.tools.backend_registry import describe_backends
        from trevor_databricks.tools.databricks_toolset import tool_names as _db_tool_names

        out = describe_backends(self.cfg)
        out["databricks_native_tools"] = _db_tool_names()

        try:
            from tools.registry import registry as _registry  # type: ignore
        except Exception:
            out["registry"] = {"available": False, "reason": "Hermes tool registry not importable"}
            return out

        try:
            all_names = _registry.get_all_tool_names()
            tool_to_ts = _registry.get_tool_to_toolset_map()
            ts_avail = _registry.check_toolset_requirements()
            grouped: dict[str, list[str]] = {}
            for name in all_names:
                ts = tool_to_ts.get(name, "unknown")
                grouped.setdefault(ts, []).append(name)
            out["registry"] = {
                "available": True,
                "tool_count": len(all_names),
                "toolset_count": len(grouped),
                "toolset_availability": ts_avail,
                "tools_by_toolset": grouped,
            }
            if self.agent is not None:
                enabled_tools = sorted(getattr(self.agent, "selected_tools", []) or [])
                out["registry"]["selected_tools"] = enabled_tools
                out["registry"]["selected_tool_count"] = len(enabled_tools)
        except Exception as exc:
            out["registry"] = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
        return out

    def cron_summary(self) -> dict[str, Any]:
        if not self.cron_available:
            return {"available": False, "reason": "hermes cron unavailable"}
        try:
            from cron.jobs import load_jobs  # type: ignore

            jobs = load_jobs()
        except Exception as exc:
            return {"available": True, "error": f"{type(exc).__name__}: {exc}", "jobs": []}

        out: dict[str, Any] = {
            "available": True,
            "count": len(jobs),
            "jobs": jobs,
            "tick_count": self._cron_tick_count,
            "tick_last_at": self._cron_last_tick_at,
            "tick_last_executed": self._cron_last_executed,
            "tick_last_error": self._cron_last_error,
        }
        return out

    def cron_tick(self) -> dict[str, Any]:
        """Run one cron scheduler tick. Idempotent; safe to call frequently.

        Persists a ``cron_tick`` event to Lakebase regardless of how many
        jobs actually ran, so the operator can audit liveness from
        ``/debug/events``.
        """
        if not self.cron_available or self.scheduler is None:
            return {"available": False}
        executed = 0
        err: str | None = None
        try:
            executed = int(self.scheduler(verbose=False) or 0)
        except Exception as exc:
            log.exception("cron tick failed")
            err = f"{type(exc).__name__}: {exc}"
            self._cron_last_error = err

        self._cron_tick_count += 1
        self._cron_last_tick_at = time.time()
        self._cron_last_executed = executed
        if err is None:
            self._cron_last_error = None

        # Persist to Lakebase if available.
        if self.session_db is not None:
            try:
                self.session_db.append_event(  # type: ignore[attr-defined]
                    "cron_tick",
                    {"executed": executed, "error": err},
                )
            except Exception:
                log.exception("cron_tick event persist failed")

        # Touch the cron subpath so the next heartbeat sync uploads
        # any state mutations Hermes made (next_run_at advances, etc.).
        if self.home_fs is not None:
            try:
                self.home_fs.touch_subpath("cron")  # type: ignore[attr-defined]
            except Exception:
                log.debug("home_fs.touch_subpath('cron') unavailable", exc_info=True)

        return {"executed": executed, "error": err, "tick_count": self._cron_tick_count}

    def run_cron_job(self, job_id: str) -> dict[str, Any]:
        if not self.cron_available:
            raise RuntimeError("Hermes cron scheduler unavailable")
        try:
            from cron.jobs import load_jobs  # type: ignore
            from cron.scheduler import run_job  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"cron API unavailable: {exc}") from exc
        jobs = {j.get("id"): j for j in load_jobs() if isinstance(j, dict)}
        if job_id not in jobs:
            raise KeyError(job_id)
        result = run_job(jobs[job_id])

        # Persist the run to Lakebase.
        success, output, final_response, error = (
            result if isinstance(result, tuple) and len(result) == 4 else (None, None, None, None)
        )
        if self.session_db is not None:
            try:
                self.session_db.append_event(  # type: ignore[attr-defined]
                    "cron_run",
                    {
                        "job_id": job_id,
                        "success": success,
                        "error": error,
                        "preview": (output or "")[:512] if isinstance(output, str) else None,
                    },
                )
            except Exception:
                log.exception("cron_run event persist failed")

        return {
            "job_id": job_id,
            "success": success,
            "output": output,
            "final_response": final_response,
            "error": error,
        }

    # ------------------------------------------------------------------
    # Health probes
    # ------------------------------------------------------------------

    async def health_probe(self) -> dict[str, Any]:
        return {
            "initialized": self._initialized,
            "agent_loaded": self.agent is not None,
            "errors": dict(self.errors),
        }

    async def health_probe_model(self) -> dict[str, Any]:
        if self.provider is None:
            return {"status": "unavailable", "reason": "provider not initialised"}
        try:
            self.provider.client()  # cached but raises if creds bad
            return {"status": "ok", "endpoint": self.cfg.llm_endpoint}
        except Exception as exc:
            return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
