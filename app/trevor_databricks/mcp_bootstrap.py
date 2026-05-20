"""Bootstrap the Databricks managed MCP servers into HERMES_HOME/config.yaml.

Hermes' MCP discovery (``hermes_tools/mcp_tool.py``) reads
``HERMES_HOME/config.yaml`` for an ``mcp_servers:`` block. On
Databricks Apps we want the managed **SQL MCP** server
(``https://<workspace-hostname>/api/2.0/mcp/sql``) wired up by default
so that the agent prefers Unity Catalog for "list databases"-style
asks instead of Lakebase.

This module is responsible for:

* Resolving the workspace hostname from ``WorkspaceClient().config.host``.
* Minting a fresh Databricks bearer token via
  ``WorkspaceClient().config.authenticate()`` (same pattern as
  ``databricks_provider.DatabricksOpenAIClientFactory``).
* Merging an ``mcp_servers:`` entry into ``HERMES_HOME/config.yaml``
  while preserving any operator-supplied servers under different
  names.

The token rotates on each call. The supervisor heartbeat invokes
``refresh_mcp_config()`` at the same cadence as it refreshes the
OpenAI client's bearer (≤30 minutes) so the SQL MCP credential never
goes stale.

Failure mode: if the bearer mint fails, the previous config.yaml is
left untouched and the runtime records the error under
``runtime.errors["mcp_bootstrap"]``. The agent boots cleanly without
MCP; the SP just won't have SQL MCP tools registered for that turn.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from trevor_databricks.config import Config

log = logging.getLogger("trevor_databricks.mcp_bootstrap")


DATABRICKS_SQL_MCP_SERVER_NAME = "databricks-sql"


def _fresh_bearer_token() -> str | None:
    """Mint a fresh Databricks bearer token via the SDK auth flow."""
    try:
        from databricks.sdk import WorkspaceClient  # type: ignore
    except Exception:
        log.debug("databricks-sdk unavailable; cannot mint bearer for MCP")
        return None

    try:
        w = WorkspaceClient()
        headers = w.config.authenticate() or {}
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        if isinstance(auth, str) and auth.lower().startswith("bearer "):
            return auth.split(" ", 1)[1].strip() or None
    except Exception:
        log.exception("Failed to mint Databricks bearer token for MCP bootstrap")
    return None


def _workspace_host() -> str | None:
    try:
        from databricks.sdk import WorkspaceClient  # type: ignore

        w = WorkspaceClient()
        host = (w.config.host or "").rstrip("/")
        return host or None
    except Exception:
        log.exception("Failed to resolve Databricks workspace host for MCP bootstrap")
        return None


def _config_path(cfg: Config) -> Path:
    return cfg.trevor_home / "config.yaml"


def _load_existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore

        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, dict):
            return data
    except Exception:
        log.exception("Failed to parse existing HERMES_HOME/config.yaml; will rewrite from scratch")
    return {}


def _dump(path: Path, data: dict[str, Any]) -> None:
    import yaml  # type: ignore

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)


def _build_sql_mcp_entry(host: str, bearer: str) -> dict[str, Any]:
    """Compose the ``mcp_servers`` YAML entry for the managed SQL MCP."""
    return {
        "name": DATABRICKS_SQL_MCP_SERVER_NAME,
        "transport": "http",
        "url": f"{host}/api/2.0/mcp/sql",
        "headers": {
            "Authorization": f"Bearer {bearer}",
        },
        "description": (
            "Databricks managed SQL MCP server. Use this to list "
            "Unity Catalog catalogs/schemas/tables and run SELECT/SHOW "
            "statements against the configured SQL warehouse. Prefer "
            "this over Lakebase tools for general 'list databases' / "
            "'describe table' asks."
        ),
    }


def refresh_mcp_config(cfg: Config) -> dict[str, Any]:
    """(Re-)write ``HERMES_HOME/config.yaml`` with the managed SQL MCP entry.

    Returns a small status dict for logging / event ledger purposes.
    Idempotent: safe to call on every heartbeat. Preserves any
    operator-supplied servers in ``mcp_servers`` that do not collide
    with the ``databricks-sql`` name.
    """
    status: dict[str, Any] = {
        "enabled": bool(cfg.mcp_enabled),
        "config_path": str(_config_path(cfg)),
        "applied": False,
    }
    if not cfg.mcp_enabled:
        status["reason"] = "TREVOR_DATABRICKS_MCP_ENABLED=false"
        return status

    host = _workspace_host()
    if not host:
        status["reason"] = "workspace_host_unresolved"
        return status

    bearer = _fresh_bearer_token()
    if not bearer:
        status["reason"] = "bearer_mint_failed"
        return status

    path = _config_path(cfg)
    data = _load_existing(path)

    servers = data.get("mcp_servers")
    if not isinstance(servers, list):
        servers = []

    sql_entry = _build_sql_mcp_entry(host, bearer)
    merged: list[dict[str, Any]] = []
    replaced = False
    for entry in servers:
        if isinstance(entry, dict) and entry.get("name") == DATABRICKS_SQL_MCP_SERVER_NAME:
            merged.append(sql_entry)
            replaced = True
        else:
            merged.append(entry)
    if not replaced:
        merged.insert(0, sql_entry)

    data["mcp_servers"] = merged

    try:
        _dump(path, data)
    except Exception as exc:
        log.exception("Failed to write HERMES_HOME/config.yaml")
        status["reason"] = f"{type(exc).__name__}: {exc}"
        return status

    status["applied"] = True
    status["server_count"] = len(merged)
    status["host"] = host
    return status
