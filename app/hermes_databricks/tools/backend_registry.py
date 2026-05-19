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
from typing import Any

from hermes_databricks.config import Config

# Toolsets we enable by default for the App. Pure-Python + Databricks-
# friendly. (Heavy host-dependent toolsets stay disabled and emit
# explicit "backend required" diagnostics when invoked.)
DEFAULT_ENABLED_TOOLSETS: list[str] = [
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
DEFAULT_DISABLED_TOOLSETS: list[str] = [
    "voice",  # no audio devices
    "computer-use",  # macOS-only
    "homeassistant",  # external network required + secret
]


def build_toolset_selection(cfg: Config) -> tuple[list[str], list[str]]:
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
    detail: dict[str, Any] | None = None


def describe_backends(cfg: Config) -> dict[str, Any]:
    """Summarise the configured backends — drives ``/debug/tools``."""
    from hermes_databricks.tools import browser_backend, mcp_backend

    terminal = BackendStatus(
        backend="terminal",
        selected=cfg.terminal_backend,
        available=cfg.terminal_backend == "in_app_subprocess",
        reason=(
            "Default in-app subprocess backend (cwd restricted, 60s default timeout)."
            if cfg.terminal_backend == "in_app_subprocess"
            else (f"Only in_app_subprocess is wired today; selected={cfg.terminal_backend!r}")
        ),
    )

    b_desc = browser_backend.describe(cfg.browser_backend)
    browser = BackendStatus(
        backend="browser",
        selected=cfg.browser_backend,
        available=bool(b_desc.get("available")),
        reason=b_desc.get("reason", ""),
        detail=b_desc,
    )

    m_desc = mcp_backend.describe(cfg.mcp_enabled)
    mcp = BackendStatus(
        backend="mcp",
        selected="enabled" if cfg.mcp_enabled else "disabled",
        available=bool(cfg.mcp_enabled),
        reason=m_desc.get("reason", ""),
        detail=m_desc,
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
        "toolset_selection": dict(
            zip(
                ("enabled", "disabled"),
                build_toolset_selection(cfg),
                strict=False,
            )
        ),
    }
