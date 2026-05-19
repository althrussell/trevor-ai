"""Browser tool backend selectors.

Hermes' own browser tools call ``check_browser_requirements()`` from
``tools/browser_tool.py``. In the Databricks App we run with no
browser binaries by default, so that check fails and the entire
``browser`` toolset is reported as unavailable to the model — exactly
the explicit-diagnostic behaviour the requirements call for.

This module exposes a `describe()` helper that the `/debug/tools`
endpoint surfaces, plus three backend selectors:

* ``in_app_playwright`` — only viable if the App image ships Chromium
  binaries (it doesn't by default).
* ``databricks_job_browser`` — delegates navigation to a Databricks
  Job that has playwright pre-installed. Not yet wired.
* ``external_browser`` — Browserbase/Steel/etc. via HTTP; needs
  ``browserbase_api_key`` / ``browserbase_project_id`` secrets.

Setting ``HERMES_DATABRICKS_BROWSER_BACKEND`` to anything other than
``disabled`` flips the diagnostic to "configure the missing secrets"
so the operator gets pointed at the right thing.
"""

from __future__ import annotations

from typing import Any, Dict


_BACKENDS = {"disabled", "in_app_playwright", "databricks_job_browser", "external_browser"}


def describe(backend: str = "disabled") -> Dict[str, Any]:
    backend = (backend or "disabled").strip()
    if backend not in _BACKENDS:
        return {
            "backend": backend,
            "available": False,
            "reason": f"unknown backend; expected one of {sorted(_BACKENDS)}",
        }
    if backend == "disabled":
        return {
            "backend": "disabled",
            "available": False,
            "reason": (
                "Browser tools are disabled. Set HERMES_DATABRICKS_BROWSER_BACKEND "
                "to one of 'in_app_playwright', 'databricks_job_browser', or "
                "'external_browser' to enable them."
            ),
        }
    if backend == "in_app_playwright":
        try:
            import playwright  # noqa: F401  type: ignore
            return {
                "backend": backend,
                "available": True,
                "reason": "playwright importable; chromium binary still required at runtime.",
            }
        except ImportError:
            return {
                "backend": backend,
                "available": False,
                "reason": (
                    "playwright is not installed in the Databricks App image. "
                    "Add 'playwright' to app/requirements.txt and ensure the "
                    "Databricks App base image ships chromium, or switch to "
                    "'databricks_job_browser' / 'external_browser'."
                ),
            }
    if backend == "databricks_job_browser":
        return {
            "backend": backend,
            "available": False,
            "reason": (
                "databricks_job_browser is not wired yet. Configure a "
                "Databricks Job with playwright installed and set "
                "HERMES_DATABRICKS_BROWSER_JOB_ID."
            ),
        }
    if backend == "external_browser":
        return {
            "backend": backend,
            "available": False,
            "reason": (
                "external_browser (Browserbase/Steel/etc.) requires "
                "browserbase_api_key / browserbase_project_id in Databricks "
                "Secrets and the corresponding Hermes config block."
            ),
        }
    return {"backend": backend, "available": False, "reason": "unhandled backend"}


def navigate(url: str, *, backend: str = "disabled") -> Dict[str, Any]:
    """Return the same status as ``describe(backend)`` plus the requested url.

    The browser tool itself stays inside Hermes — this function is only
    used by ``/debug/browser`` for operator-facing diagnostics.
    """
    out = describe(backend)
    out["url"] = url
    return out
