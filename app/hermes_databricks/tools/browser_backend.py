"""Browser tool backend selectors (Phase 1 skeleton; default disabled)."""

from __future__ import annotations

from typing import Any, Dict


def navigate(url: str, *, backend: str = "disabled") -> Dict[str, Any]:  # pragma: no cover - Phase 1 stub
    return {
        "status": "backend_unavailable",
        "backend": backend,
        "reason": (
            "No browser backend is configured for this Databricks App. "
            "Set HERMES_DATABRICKS_BROWSER_BACKEND to one of "
            "'in_app_playwright', 'databricks_job_browser', or "
            "'external_browser' (e.g. Browserbase) and supply the "
            "corresponding secrets."
        ),
    }
