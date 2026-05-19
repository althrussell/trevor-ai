"""MCP backend (Phase 1 skeleton; default disabled, opt-in in Phase 7)."""

from __future__ import annotations

from typing import Any, Dict


def list_servers() -> Dict[str, Any]:  # pragma: no cover - Phase 1 stub
    return {
        "status": "disabled",
        "reason": (
            "MCP support is disabled. Set HERMES_DATABRICKS_MCP_ENABLED=true "
            "and configure mcp_servers in HERMES_HOME/config.yaml."
        ),
        "servers": [],
    }
