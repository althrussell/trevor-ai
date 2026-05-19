"""Databricks-native Hermes toolset (Phase 1 skeleton; full in Phase 7)."""

from __future__ import annotations

from typing import Optional


def register_databricks_toolset(cfg, home_fs: Optional[object] = None) -> int:  # pragma: no cover - Phase 1 stub
    """Register the ``databricks`` toolset against Hermes' tool registry.

    Phase 7 fills this in. Returning 0 lets the runtime continue with
    no Databricks-native tools registered.
    """
    return 0
