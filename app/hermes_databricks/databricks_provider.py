"""Databricks Model Serving provider adapter.

This module gives Hermes an OpenAI-compatible client whose Bearer
authentication is refreshed on every request by the Databricks Python
SDK. The integration is intentionally as isolated as possible:

* A single helper builds the OpenAI client using
  ``WorkspaceClient().serving_endpoints.get_open_ai_client()``. The SDK
  installs an ``httpx.Auth`` flow that calls ``WorkspaceConfig.authenticate``
  on every outgoing request, so long-running App containers never see a
  stale token.

* ``apply_to_agent(agent)`` swaps the freshly-constructed ``AIAgent``'s
  OpenAI client and patches ``_client_kwargs`` so that Hermes' own
  client-rebuild path (``agent.agent_runtime_helpers.create_openai_client``)
  reuses the Databricks-authenticated ``httpx.Client``.

* ``register_provider_profile(cfg)`` adds a ``databricks`` entry to the
  Hermes provider registry so introspection (`hermes model`,
  `/debug/runtime`) reports it as a first-class provider. This is
  informational — the actual wire path is controlled by the swapped
  ``agent.client``.

Nothing in Hermes core is patched.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

from hermes_databricks.config import Config


log = logging.getLogger("hermes_databricks.databricks_provider")


# ---------------------------------------------------------------------
# OpenAI client factory
# ---------------------------------------------------------------------


class DatabricksOpenAIClientFactory:
    """Build and cache an OpenAI-compatible client backed by Databricks Model Serving.

    Uses ``WorkspaceClient().serving_endpoints.get_open_ai_client()``,
    which is exactly the pattern Living-AI uses. The returned OpenAI
    client targets ``<workspace>/serving-endpoints`` and is wired with
    an ``httpx.Auth`` that mints a fresh Databricks bearer token on
    every request.

    Thread-safe: ``client()`` is guarded by a lock and the cached
    instance is shared across threads.
    """

    def __init__(self, endpoint: str, *, workspace_factory=None) -> None:
        self.endpoint = endpoint
        self._workspace_factory = workspace_factory  # for tests
        self._lock = threading.Lock()
        self._client: Any = None
        self._workspace: Any = None
        self._base_url: Optional[str] = None
        self._http_client: Any = None

    # -------------------- public API --------------------

    def client(self):
        """Return the cached OpenAI client; build it on first use."""
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            self._client = self._build_client()
            return self._client

    def reset(self) -> None:
        """Discard any cached client and httpx context. Tests use this."""
        with self._lock:
            self._client = None
            self._workspace = None
            self._base_url = None
            self._http_client = None

    @property
    def base_url(self) -> Optional[str]:
        return self._base_url

    @property
    def http_client(self) -> Any:
        return self._http_client

    # -------------------- AIAgent wiring --------------------

    def apply_to_agent(self, agent) -> None:
        """Swap ``agent.client`` with the Databricks-authenticated OpenAI client.

        Also overwrites ``agent._client_kwargs``, ``agent.base_url``,
        ``agent.api_key`` and ``agent.model`` so that:

        * Hermes' internal ``_create_openai_client`` rebuilds (used for
          retries / per-request transports) reuse our ``http_client``
          and therefore preserve token refresh.
        * Diagnostics / logging strings show the real Databricks
          endpoint instead of the placeholder we used at construction.
        """
        oai = self.client()
        agent.client = oai
        agent.model = self.endpoint
        agent.base_url = self._base_url or getattr(oai, "base_url", None)
        agent.api_key = "no-token"
        # _client_kwargs is consumed by agent_runtime_helpers.create_openai_client.
        # We add an http_client so any rebuild stays Databricks-authenticated.
        new_kwargs: Dict[str, Any] = {
            "api_key": "no-token",
            "base_url": agent.base_url,
        }
        if self._http_client is not None:
            new_kwargs["http_client"] = self._http_client
        agent._client_kwargs = new_kwargs

        log.info(
            "Databricks OpenAI client applied to AIAgent",
            extra={"extras": {
                "endpoint": self.endpoint,
                "base_url": agent.base_url,
            }},
        )

    # -------------------- construction --------------------

    def _build_client(self):
        if self._workspace_factory is not None:
            w = self._workspace_factory()
        else:
            from databricks.sdk import WorkspaceClient  # type: ignore
            w = WorkspaceClient()

        self._workspace = w
        try:
            oai = w.serving_endpoints.get_open_ai_client()
        except DeprecationWarning:  # raised by some patched test envs
            raise
        # The SDK's OpenAI() instance exposes `.base_url` and the
        # underlying httpx client through `._client`. We hang on to both
        # so rebuilds via Hermes' helper can reuse the authenticated
        # transport.
        self._base_url = str(getattr(oai, "base_url", "")).rstrip("/")
        self._http_client = getattr(oai, "_client", None)
        if self._http_client is None:
            # Some SDK versions store the httpx client on `_client_async` /
            # other attribute. Fall back to introspection.
            self._http_client = _extract_httpx_client(oai)
        return oai


def _extract_httpx_client(oai: Any) -> Any:
    for attr in ("_client", "_session", "_http_client", "http_client"):
        value = getattr(oai, attr, None)
        if value is not None:
            return value
    return None


# ---------------------------------------------------------------------
# Provider profile registration
# ---------------------------------------------------------------------


def register_provider_profile(cfg: Config) -> bool:
    """Register ``databricks`` with Hermes' provider registry.

    Returns ``True`` on success, ``False`` if Hermes is not importable
    (so callers can degrade gracefully).
    """
    try:
        from providers import register_provider  # type: ignore
        from providers.base import ProviderProfile  # type: ignore
    except Exception:
        log.debug("Hermes provider registry unavailable; skipping registration", exc_info=True)
        return False

    workspace_host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
    base_url = f"{workspace_host}/serving-endpoints" if workspace_host else ""

    try:
        profile = ProviderProfile(
            name="databricks",
            api_mode="chat_completions",
            aliases=("databricks-fmapi", "databricks-model-serving"),
            display_name="Databricks Model Serving",
            env_vars=("DATABRICKS_HOST", "DATABRICKS_CLIENT_ID"),
            base_url=base_url,
            models_url="",  # we don't expose a listing URL; SDK has one
            auth_type="oauth_workspace",
        )
        register_provider(profile)
        log.info("Registered 'databricks' provider profile with Hermes")
        return True
    except Exception:
        log.exception("Failed to register Databricks provider profile")
        return False


# ---------------------------------------------------------------------
# Diagnostic helpers
# ---------------------------------------------------------------------


def quick_chat(
    factory: DatabricksOpenAIClientFactory,
    *,
    message: str,
    system: Optional[str] = None,
    max_tokens: int = 256,
) -> Dict[str, Any]:
    """Direct one-shot call used by ``/debug/model-turn`` before Hermes is wired."""
    oai = factory.client()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": message})

    resp = oai.chat.completions.create(
        model=factory.endpoint,
        messages=messages,
        max_tokens=max_tokens,
    )

    choice = resp.choices[0] if resp.choices else None
    text = (choice.message.content if choice and getattr(choice, "message", None) else "") or ""
    usage = getattr(resp, "usage", None)
    usage_dict = {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    } if usage is not None else {}

    return {
        "text": text,
        "model": factory.endpoint,
        "usage": usage_dict,
        "finish_reason": getattr(choice, "finish_reason", None) if choice else None,
    }
