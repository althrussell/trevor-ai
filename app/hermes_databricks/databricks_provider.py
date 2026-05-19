"""Databricks Model Serving provider adapter (Phase 1 skeleton).

The full implementation lives in Phase 3 (next commit). This file
exists so the runtime can ``import`` and the import error is recorded
cleanly in degraded-mode boot.
"""

from __future__ import annotations

from hermes_databricks.config import Config


class DatabricksOpenAIClientFactory:
    """Will return an OpenAI-compatible client built from WorkspaceClient."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._client = None

    def client(self):  # pragma: no cover - Phase 1 stub
        raise NotImplementedError(
            "DatabricksOpenAIClientFactory.client() is implemented in Phase 3"
        )

    def apply_to_agent(self, agent) -> None:  # pragma: no cover - Phase 1 stub
        raise NotImplementedError(
            "DatabricksOpenAIClientFactory.apply_to_agent() is implemented in Phase 3"
        )


def register_provider_profile(cfg: Config) -> bool:  # pragma: no cover - Phase 1 stub
    return False
