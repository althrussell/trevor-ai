"""Tool backend selectors and toolset selection helpers.

This module decides:
  * Which Hermes toolsets are enabled by default for the Databricks App.
  * Which backend variant each non-pure-Python tool uses
    (terminal_backend, browser_backend, mcp_backend).

It is intentionally small and stateless — the per-tool backends own
their own configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from hermes_databricks.config import Config


# Toolsets we enable by default for the App. Pure-Python + Databricks-
# friendly. (Heavy host-dependent toolsets stay disabled and emit
# explicit "backend required" diagnostics when invoked.)
DEFAULT_ENABLED_TOOLSETS: List[str] = [
    "core",
    "file",
    "web",
    "memory",
    "skills",
    "session",
    "cron",
    "planner",
    "delegation",
    "databricks",
]

# Toolsets we explicitly disable for the App container.
DEFAULT_DISABLED_TOOLSETS: List[str] = [
    "voice",          # no audio devices
    "computer-use",   # macOS-only
    "homeassistant",  # external network required + secret
]


def build_toolset_selection(cfg: Config) -> Tuple[List[str], List[str]]:
    """Return ``(enabled, disabled)`` for ``AIAgent(enabled_toolsets=..., disabled_toolsets=...)``."""
    enabled = list(DEFAULT_ENABLED_TOOLSETS)
    disabled = list(DEFAULT_DISABLED_TOOLSETS)

    # Browser backend: disabled by default; toolset stays registered
    # so the BackendUnavailable diagnostic shows up.
    if cfg.browser_backend == "disabled":
        disabled.append("browser")
    else:
        enabled.append("browser")

    # MCP backend: same pattern.
    if not cfg.mcp_enabled:
        disabled.append("mcp")
    else:
        enabled.append("mcp")

    # Terminal backend is always present (in-app subprocess is the
    # default); if explicitly disabled, skip it.
    if cfg.terminal_backend == "disabled":
        disabled.append("terminal")
    else:
        enabled.append("terminal")

    return enabled, disabled


@dataclass
class BackendStatus:
    backend: str
    selected: str
    available: bool
    reason: str = ""
    detail: Dict[str, Any] | None = None


def describe_backends(cfg: Config) -> Dict[str, Any]:
    """Summarise the configured backends — drives ``/debug/tools``."""
    terminal = BackendStatus(
        backend="terminal",
        selected=cfg.terminal_backend,
        available=cfg.terminal_backend in {"in_app_subprocess"},
        reason="" if cfg.terminal_backend == "in_app_subprocess" else "Only in_app_subprocess is wired in Phase 7",
    )
    browser = BackendStatus(
        backend="browser",
        selected=cfg.browser_backend,
        available=False,
        reason=("disabled by HERMES_DATABRICKS_BROWSER_BACKEND=disabled"
                if cfg.browser_backend == "disabled" else "backend not yet wired"),
    )
    mcp = BackendStatus(
        backend="mcp",
        selected="remote_http" if cfg.mcp_enabled else "disabled",
        available=False,
        reason=("disabled by HERMES_DATABRICKS_MCP_ENABLED=false"
                if not cfg.mcp_enabled else "remote HTTP backend not yet wired"),
    )

    return {
        "backends": {
            b.backend: {
                "selected": b.selected,
                "available": b.available,
                "reason": b.reason,
                "detail": b.detail or {},
            }
            for b in (terminal, browser, mcp)
        },
        "toolset_selection": dict(zip(
            ("enabled", "disabled"),
            build_toolset_selection(cfg),
        )),
    }
