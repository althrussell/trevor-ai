"""MCP backend status helper.

When ``HERMES_DATABRICKS_MCP_ENABLED=true``, Hermes' own MCP
discovery (``tools/mcp_tool.py``) reads ``HERMES_HOME/config.yaml``
for ``mcp_servers``. We don't intercept that path — we just expose a
diagnostic for ``/debug/tools`` that explains what the operator needs
to do to wire it up.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _config_path() -> Path:
    home = os.environ.get("HERMES_HOME") or "/tmp/hermes_cache/hermes_home"
    return Path(home) / "config.yaml"


def describe(enabled: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "enabled": bool(enabled),
        "config_path": str(_config_path()),
    }
    if not enabled:
        out["status"] = "disabled"
        out["reason"] = (
            "MCP is disabled. Set HERMES_DATABRICKS_MCP_ENABLED=true and add "
            "an `mcp_servers:` block to HERMES_HOME/config.yaml. Remote-HTTP "
            "MCP servers work in the App container; stdio MCP servers need "
            "the binary present in the image."
        )
        return out

    servers: list[dict[str, Any]] = []
    try:
        import yaml  # type: ignore

        path = _config_path()
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            raw = cfg.get("mcp_servers") or []
            if isinstance(raw, list):
                for entry in raw:
                    if isinstance(entry, dict):
                        servers.append(
                            {
                                "name": entry.get("name"),
                                "transport": entry.get("transport")
                                or ("http" if entry.get("url") else "stdio"),
                                "url": entry.get("url"),
                                "command": entry.get("command"),
                            }
                        )
    except Exception as exc:
        out["status"] = "error"
        out["reason"] = f"failed to parse MCP config: {type(exc).__name__}: {exc}"
        return out

    out["status"] = "configured" if servers else "enabled-but-empty"
    out["servers"] = servers
    return out


def list_servers() -> dict[str, Any]:
    enabled = os.environ.get("HERMES_DATABRICKS_MCP_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    return describe(enabled)
