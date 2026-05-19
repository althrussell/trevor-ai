"""Environment-driven configuration for the Hermes-on-Databricks runtime.

Resolution rules:

* Required values come from the Databricks App environment (set via
  ``app.yaml``, bundle variables, or operator overrides).
* Secrets (Telegram token, optional provider keys) are read lazily
  from Databricks Secrets via ``WorkspaceClient().secrets.get_secret``
  the first time they are needed; values are base64-decoded.
* All env var names share the ``HERMES_DATABRICKS_`` prefix so they
  don't clash with Hermes' own ``HERMES_*`` configuration surface.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _str_env(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip() or default


@dataclass(frozen=True)
class Config:
    # Identity
    agent_name: str = "Hermes"

    # Databricks
    catalog: str = "workspace"
    schema: str = "hermes_agent"
    hermes_home_volume: str = "hermes_home"
    artifacts_volume: str = "hermes_artifacts"
    secrets_scope: str = "hermes_agent"
    warehouse_id: str = ""

    # Lakebase
    lakebase_instance: str = "hermes-db"
    lakebase_database: str = "databricks_postgres"
    lakebase_schema: str = "hermes_session"

    # Model serving
    llm_endpoint: str = "databricks-qwen3-next-80b-a3b-instruct"

    # Runtime knobs
    daily_token_cap: int = 100_000
    heartbeat_seconds: int = 180

    # Channels
    telegram_enabled: bool = True

    # Tool backends
    terminal_backend: str = "in_app_subprocess"
    browser_backend: str = "disabled"
    mcp_enabled: bool = False

    # Local cache + Hermes home
    cache_root: Path = field(default_factory=lambda: Path("/tmp/hermes_cache"))
    hermes_home: Path = field(default_factory=lambda: Path("/tmp/hermes_cache/hermes_home"))

    # Misc
    log_level: str = "INFO"

    @property
    def hermes_home_volume_path(self) -> str:
        """Absolute UC Volume root for the Hermes home mirror."""
        return f"/Volumes/{self.catalog}/{self.schema}/{self.hermes_home_volume}"

    @property
    def artifacts_volume_path(self) -> str:
        """Absolute UC Volume root for agent artifacts."""
        return f"/Volumes/{self.catalog}/{self.schema}/{self.artifacts_volume}"


@lru_cache(maxsize=1)
def load() -> Config:
    """Load the immutable runtime config from the environment."""
    cache_root = Path(_str_env("HERMES_DATABRICKS_CACHE_ROOT", "/tmp/hermes_cache"))
    hermes_home = Path(_str_env("HERMES_HOME", str(cache_root / "hermes_home")))

    return Config(
        agent_name=_str_env("HERMES_DATABRICKS_AGENT_NAME", "Hermes"),
        catalog=_str_env("HERMES_DATABRICKS_CATALOG", "workspace"),
        schema=_str_env("HERMES_DATABRICKS_SCHEMA", "hermes_agent"),
        hermes_home_volume=_str_env("HERMES_DATABRICKS_HERMES_HOME_VOLUME", "hermes_home"),
        artifacts_volume=_str_env("HERMES_DATABRICKS_ARTIFACTS_VOLUME", "hermes_artifacts"),
        secrets_scope=_str_env("HERMES_DATABRICKS_SECRETS_SCOPE", "hermes_agent"),
        warehouse_id=_str_env("HERMES_DATABRICKS_WAREHOUSE_ID", ""),
        lakebase_instance=_str_env("HERMES_DATABRICKS_LAKEBASE_INSTANCE", "hermes-db"),
        lakebase_database=_str_env("HERMES_DATABRICKS_LAKEBASE_DATABASE", "databricks_postgres"),
        lakebase_schema=_str_env("HERMES_DATABRICKS_LAKEBASE_SCHEMA", "hermes_session"),
        llm_endpoint=_str_env(
            "HERMES_DATABRICKS_LLM_ENDPOINT",
            "databricks-qwen3-next-80b-a3b-instruct",
        ),
        daily_token_cap=_int_env("HERMES_DATABRICKS_DAILY_TOKEN_CAP", 100_000),
        heartbeat_seconds=_int_env("HERMES_DATABRICKS_HEARTBEAT_SECONDS", 180),
        telegram_enabled=_bool_env("HERMES_DATABRICKS_TELEGRAM_ENABLED", True),
        terminal_backend=_str_env("HERMES_DATABRICKS_TERMINAL_BACKEND", "in_app_subprocess"),
        browser_backend=_str_env("HERMES_DATABRICKS_BROWSER_BACKEND", "disabled"),
        mcp_enabled=_bool_env("HERMES_DATABRICKS_MCP_ENABLED", False),
        cache_root=cache_root,
        hermes_home=hermes_home,
        log_level=_str_env("HERMES_DATABRICKS_LOG_LEVEL", "INFO"),
    )


def reset_for_tests() -> None:
    """Clear the cached config (used by the test suite only)."""
    load.cache_clear()  # type: ignore[attr-defined]


# ----------------------------------------------------------------------
# Secrets helpers
# ----------------------------------------------------------------------


def _safe_b64decode(value: str) -> str:
    """Decode the base64-encoded value returned by ``secrets.get_secret``.

    The Databricks API returns ``string_value`` as a base64 string; the
    SDK passes it through unchanged. Some bindings (e.g. App-injected
    secret env vars) deliver a decoded value already, so we try both.
    """
    try:
        return base64.b64decode(value).decode("utf-8")
    except Exception:
        return value


def get_secret(scope: str, key: str) -> Optional[str]:
    """Best-effort secret resolution.

    1. Env var ``DATABRICKS_SECRET_<SCOPE>_<KEY>`` if the App resource
       binding injected it (rare, but supported).
    2. Env var ``<KEY>`` for local-dev convenience.
    3. ``WorkspaceClient().secrets.get_secret(scope, key)`` lazily.
    """
    env_specific = os.environ.get(f"DATABRICKS_SECRET_{scope.upper()}_{key.upper()}")
    if env_specific:
        return _safe_b64decode(env_specific)

    env_plain = os.environ.get(key.upper())
    if env_plain:
        return env_plain

    try:
        from databricks.sdk import WorkspaceClient  # local import: heavy
    except Exception:
        return None

    try:
        w = WorkspaceClient()
        resp = w.secrets.get_secret(scope=scope, key=key)
        raw = getattr(resp, "value", None)
        if raw is None:
            return None
        return _safe_b64decode(raw)
    except Exception:
        return None
