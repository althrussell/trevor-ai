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
from typing import Any

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
        self._base_url: str | None = None
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
    def base_url(self) -> str | None:
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
        # Refresh the bearer token from the Databricks SDK on each apply
        # so the agent starts with a fresh credential. We do NOT pass
        # ``http_client=self._http_client`` here, because Hermes closes
        # per-request OpenAI clients (and the httpx client they wrap)
        # via ``_close_openai_client``. Sharing ours would tear our
        # client down after the first turn. Instead, Hermes builds its
        # own keepalive httpx.Client per request — see
        # ``run_agent._create_openai_client``.
        bearer = self._fresh_bearer_token()
        agent.api_key = bearer or "no-token"
        new_kwargs: dict[str, Any] = {
            "api_key": agent.api_key,
            "base_url": agent.base_url,
        }
        agent._client_kwargs = new_kwargs

        # Databricks Foundation Model serving's OpenAI-compatible proxy
        # rejects ``stream_options`` and is not a great fit for SSE
        # streaming from a long-running server-side App. Tell Hermes to
        # use the non-streaming chat.completions.create() path. Hermes
        # uses this same flag internally when a provider signals
        # "stream not supported".
        agent._disable_streaming = True

        # Mark this agent so the subagent hook can detect already-wired
        # instances (the parent goes through this path explicitly during
        # bootstrap; children flow through the hook installed below).
        agent._databricks_applied = True

        log.info(
            "Databricks OpenAI client applied to AIAgent (streaming disabled, fresh bearer)",
            extra={
                "extras": {
                    "endpoint": self.endpoint,
                    "base_url": agent.base_url,
                    "bearer_set": bool(bearer),
                }
            },
        )

    # -------------------- subagent hook --------------------

    def install_subagent_hook(self) -> bool:
        """Patch ``AIAgent.__init__`` so any fresh subagent inherits Databricks overrides.

        ``tools/delegate_tool._build_child_agent`` constructs a brand-new
        ``AIAgent(...)`` whose ``provider`` and ``model`` are inherited
        from the parent — but it never re-runs our ``apply_to_agent``
        step. Without this hook, the subagent:

          * keeps ``_disable_streaming = False`` (default), so it goes
            through Hermes' SSE streaming path. Databricks Foundation
            Model serving's stream shape then crashes Hermes' partial
            accumulator with ``sequence item 0: expected str instance,
            list found`` and the child's turn returns ``(empty)``.
          * builds its own ``OpenAI()`` client from the inherited
            ``api_key`` / ``base_url``, skipping the Databricks SDK's
            ``httpx.Auth`` flow — so the bearer never refreshes for the
            life of the subagent (rarely matters for short tasks, but
            it's a latent staleness bug).

        This hook calls ``apply_to_agent`` on every newly-constructed
        ``AIAgent`` whose provider is ``"databricks"``, unless the agent
        already carries the ``_databricks_applied`` sentinel (set by
        ``apply_to_agent`` itself, so re-application is a no-op).

        Idempotent: a class-level marker prevents double-patching when
        ``_init_hermes`` is called more than once (e.g. on retry).
        Returns ``True`` if the hook was installed (or was already
        installed), ``False`` if AIAgent is not importable.
        """
        try:
            from run_agent import AIAgent  # type: ignore
        except Exception:
            log.debug(
                "AIAgent not importable; subagent hook not installed",
                exc_info=True,
            )
            return False

        original_init = getattr(AIAgent, "__init__", None)
        if original_init is None:
            log.warning("AIAgent has no __init__; subagent hook not installed")
            return False
        if getattr(original_init, "_hermes_databricks_subagent_hook", False):
            return True  # already installed by an earlier bootstrap

        factory = self

        def init_with_databricks_hook(agent_self, *args, **kwargs):  # type: ignore[no-untyped-def]
            original_init(agent_self, *args, **kwargs)
            # Only re-wire children that resolve to our Databricks provider.
            # The parent path explicitly calls apply_to_agent during
            # bootstrap and sets _databricks_applied=True, so re-entry
            # here is harmless but skipped to avoid a redundant bearer
            # refresh log line on every parent construction.
            if getattr(agent_self, "_databricks_applied", False):
                return
            provider = (getattr(agent_self, "provider", "") or "").lower()
            if provider != "databricks":
                return
            try:
                factory.apply_to_agent(agent_self)
                log.info(
                    "Subagent re-wired with Databricks overrides via init hook "
                    "(model=%s, log_prefix=%s)",
                    getattr(agent_self, "model", "?"),
                    getattr(agent_self, "log_prefix", "") or "",
                )
            except Exception:
                log.exception(
                    "Subagent Databricks re-wiring failed; "
                    "child will run with default OpenAI client and may stream"
                )

        init_with_databricks_hook._hermes_databricks_subagent_hook = True  # type: ignore[attr-defined]
        try:
            AIAgent.__init__ = init_with_databricks_hook  # type: ignore[method-assign]
            log.info(
                "Installed Databricks subagent hook on AIAgent.__init__ "
                "(children inherit _disable_streaming + fresh bearer)"
            )
            return True
        except Exception:
            log.exception("Failed to install AIAgent.__init__ subagent hook")
            return False

    def _fresh_bearer_token(self) -> str | None:
        """Mint a fresh Databricks bearer token via the SDK auth flow.

        Uses ``WorkspaceConfig.authenticate()`` which returns the
        outgoing Authorization header for the configured auth strategy
        (App OAuth, M2M, PAT, etc.). Tokens are typically valid for
        ~1 hour, so a background refresher in the supervisor should
        call ``refresh_agent_token(agent)`` every ~30 minutes.
        """
        try:
            w = self._workspace
            if w is None:
                if self._workspace_factory is not None:
                    w = self._workspace_factory()
                else:
                    from databricks.sdk import WorkspaceClient  # type: ignore

                    w = WorkspaceClient()
                self._workspace = w
            headers = w.config.authenticate() or {}
            auth = headers.get("Authorization") or headers.get("authorization") or ""
            if isinstance(auth, str) and auth.lower().startswith("bearer "):
                return auth.split(None, 1)[1].strip()
            return auth.strip() or None
        except Exception:
            log.exception("Failed to mint Databricks bearer token")
            return None

    def refresh_agent_token(self, agent) -> bool:
        """Re-mint the bearer token and update the agent's client kwargs.

        Returns ``True`` if a new token was applied. Call periodically
        from a background task — the runtime supervisor wires this in.
        """
        bearer = self._fresh_bearer_token()
        if not bearer:
            return False
        try:
            agent.api_key = bearer
            kwargs = dict(getattr(agent, "_client_kwargs", {}) or {})
            kwargs["api_key"] = bearer
            agent._client_kwargs = kwargs
            log.debug("Refreshed Databricks bearer token on AIAgent")
            return True
        except Exception:
            log.exception("Failed to refresh agent bearer token")
            return False

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

        # Databricks Foundation Model serving's OpenAI-compatible proxy
        # is stricter than vanilla OpenAI. We sanitise outbound requests
        # in Python (before the SDK serialises them) to fix:
        #   - stream_options (Hermes always sends include_usage)
        #   - integer/array JSON-schema constraints in tool definitions
        #   - underscore-prefixed Hermes-internal message keys
        # All surfaces are described in detail in
        # ``_install_request_sanitiser`` below.
        _install_request_sanitiser(oai)
        return oai


def _extract_httpx_client(oai: Any) -> Any:
    for attr in ("_client", "_session", "_http_client", "http_client"):
        value = getattr(oai, attr, None)
        if value is not None:
            return value
    return None


# ---------------------------------------------------------------------
# Python-level request sanitiser
# ---------------------------------------------------------------------
#
# Databricks Foundation Model serving's OpenAI-compatible proxy is
# stricter than vanilla OpenAI. Two known incompatibilities surface
# with Hermes 0.14.0:
#
#   1. Hermes sends ``stream_options: {"include_usage": True}`` on every
#      streaming chat-completion request. Databricks responds with
#      ``400 BAD_REQUEST: json: unknown field "stream_options"``.
#
#   2. Hermes' tool-registry JSON schemas use type-specific constraint
#      keywords that Databricks' validator rejects entirely:
#
#        * integer / number — ``minimum`` / ``maximum`` /
#          ``exclusiveMinimum`` / ``exclusiveMaximum`` / ``multipleOf``
#          → ``Invalid JSON schema - integer types do not support minimum``
#
#        * array — ``maxItems`` / ``minItems`` / ``uniqueItems`` /
#          ``maxContains`` / ``minContains``
#          → ``Invalid JSON schema - array types do not support maxItems``
#
# We wrap the OpenAI client's ``chat.completions.create`` method so
# kwargs are sanitised *before* the SDK serialises them into an httpx
# Request. (An earlier version registered an httpx event hook on the
# underlying transport, but mutating ``request._content`` after the
# request object is built triggered "Connection error" retries inside
# the OpenAI SDK — wrapping the high-level method is more robust.)


# Per-type constraint keywords that Databricks Model Serving's
# JSON-Schema validator refuses. Add new entries only when a new
# ``Invalid JSON schema - <T> types do not support <K>`` 400 is
# observed in production logs.
_FORBIDDEN_SCHEMA_KEYS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "integer": (
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    ),
    "array": (
        "maxItems",
        "minItems",
        "uniqueItems",
        "maxContains",
        "minContains",
    ),
}


def _node_has_type(node: dict[str, Any], type_name: str) -> bool:
    """Return True if a JSON-Schema node's ``type`` field includes *type_name*.

    Handles both the scalar form (``"type": "integer"``) and the
    nullable-union form (``"type": ["integer", "null"]``) that pydantic
    emits for ``Optional[int]`` fields.
    """
    node_type = node.get("type")
    if node_type == type_name:
        return True
    return isinstance(node_type, list) and type_name in node_type


def _scrub_unsupported_schema_constraints(node: Any) -> int:
    """Recursively drop type-specific JSON-Schema keywords Databricks rejects.

    Walks dicts and lists in-place; returns the count of keys removed.
    This is the union of every constraint that has tripped a
    ``400 BAD_REQUEST: Invalid JSON schema`` from Databricks Foundation
    Model serving — see ``_FORBIDDEN_SCHEMA_KEYS_BY_TYPE`` for the
    current set and the comment block above for the failure modes that
    motivated each entry.
    """
    removed = 0
    if isinstance(node, dict):
        for type_name, forbidden_keys in _FORBIDDEN_SCHEMA_KEYS_BY_TYPE.items():
            if not _node_has_type(node, type_name):
                continue
            for key in forbidden_keys:
                if key in node:
                    node.pop(key, None)
                    removed += 1
        for v in list(node.values()):
            removed += _scrub_unsupported_schema_constraints(v)
    elif isinstance(node, list):
        for item in node:
            removed += _scrub_unsupported_schema_constraints(item)
    return removed


# Legacy alias preserved for any downstream import of the integer-only
# scrubber. New code should call ``_scrub_unsupported_schema_constraints``.
_scrub_integer_schema_constraints = _scrub_unsupported_schema_constraints


def _scrub_internal_message_fields(
    messages: list[Any],
) -> tuple[list[Any], int, set[str]]:
    """Strip Hermes-internal keys (any underscore-prefixed key) from each message.

    Returns ``(new_messages, removed_count, removed_keys)``. The OpenAI chat
    spec has no ``_``-prefixed message fields, but Hermes tags its scaffolding
    messages with private flags (e.g. ``_empty_recovery_synthetic``,
    ``_empty_terminal_sentinel``, ``_thinking_prefill``). Vanilla OpenAI
    silently ignores them, but Databricks Model Serving's Go proxy enforces
    ``json: DisallowUnknownFields`` and 400s with
    ``Bad request: json: unknown field "_empty_recovery_synthetic"``.

    Copy-on-write: only allocates a new list/dict when something is actually
    stripped, so the steady-state hot path stays allocation-free.
    """
    removed = 0
    removed_keys: set[str] = set()
    new_messages = messages
    list_copied = False
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        bad_keys = [k for k in msg if isinstance(k, str) and k.startswith("_")]
        if not bad_keys:
            continue
        if not list_copied:
            new_messages = list(messages)
            list_copied = True
        cleaned = {k: v for k, v in msg.items() if k not in bad_keys}
        new_messages[idx] = cleaned
        removed += len(bad_keys)
        removed_keys.update(bad_keys)
    return new_messages, removed, removed_keys


def _sanitise_oai_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *kwargs* safe to send to Databricks Model Serving."""
    if "stream_options" in kwargs:
        kwargs = dict(kwargs)
        kwargs.pop("stream_options", None)

    messages = kwargs.get("messages")
    if isinstance(messages, list) and messages:
        scrubbed, removed, removed_keys = _scrub_internal_message_fields(messages)
        if removed:
            kwargs = dict(kwargs)
            kwargs["messages"] = scrubbed
            log.debug(
                "Stripped %d internal message field(s) %s from %d outbound message(s)",
                removed,
                sorted(removed_keys),
                len(scrubbed),
            )

    tools = kwargs.get("tools")
    if isinstance(tools, list) and tools:
        # Deep-ish copy: only mutate tool schema dicts, not the message list.
        import copy

        kwargs = dict(kwargs)
        kwargs["tools"] = copy.deepcopy(tools)
        removed = _scrub_unsupported_schema_constraints(kwargs["tools"])
        if removed:
            log.debug(
                "Stripped %d unsupported JSON-schema constraint(s) "
                "(integer/array keywords) from %d tool definition(s)",
                removed,
                len(kwargs["tools"]),
            )
    return kwargs


def _install_request_sanitiser(oai_client: Any) -> None:
    """Patch ``Completions.create`` at the openai class level for Databricks.

    We patch *at the class* (``openai.resources.chat.completions.Completions``)
    rather than the instance, because Hermes' core constructs a fresh
    ``OpenAI`` client per chat-completion call via
    ``agent_runtime_helpers.create_openai_client`` (see run_agent.py
    ``_create_request_openai_client``). A per-instance patch would not
    survive that rebuild path.

    Idempotent. The wrapper is keyed by an attribute on the bound
    method so a second install is a no-op.
    """
    try:
        from openai.resources.chat.completions import Completions  # type: ignore
    except Exception:
        log.warning(
            "openai.resources.chat.completions.Completions not importable; "
            "Databricks request sanitiser NOT installed",
            exc_info=True,
        )
        return

    original = getattr(Completions, "create", None)
    if original is None:
        log.warning("openai Completions class has no .create; sanitiser not installed")
        return
    if getattr(original, "_hermes_databricks_sanitised", False):
        return  # already installed

    def create_sanitised(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        kwargs = _sanitise_oai_kwargs(kwargs)
        return original(self, *args, **kwargs)

    create_sanitised._hermes_databricks_sanitised = True  # type: ignore[attr-defined]
    try:
        Completions.create = create_sanitised  # type: ignore[attr-defined]
        log.info(
            "Installed Databricks request sanitiser on "
            "openai.resources.chat.completions.Completions.create "
            "(strips stream_options + integer/array schema constraints "
            "+ underscore-prefixed message keys)"
        )
    except Exception:
        log.exception("Failed to install Databricks OpenAI sanitiser")

    # Also patch AsyncCompletions just in case Hermes goes through the
    # async path on certain code paths.
    try:
        from openai.resources.chat.completions import AsyncCompletions  # type: ignore

        async_original = getattr(AsyncCompletions, "create", None)
        if async_original is not None and not getattr(
            async_original, "_hermes_databricks_sanitised", False
        ):

            async def acreate_sanitised(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
                kwargs = _sanitise_oai_kwargs(kwargs)
                return await async_original(self, *args, **kwargs)

            acreate_sanitised._hermes_databricks_sanitised = True  # type: ignore[attr-defined]
            AsyncCompletions.create = acreate_sanitised  # type: ignore[attr-defined]
            log.debug("Installed Databricks sanitiser on AsyncCompletions.create")
    except Exception:
        log.debug("AsyncCompletions patch skipped", exc_info=True)


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
    system: str | None = None,
    max_tokens: int = 256,
) -> dict[str, Any]:
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
    usage_dict = (
        {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }
        if usage is not None
        else {}
    )

    return {
        "text": text,
        "model": factory.endpoint,
        "usage": usage_dict,
        "finish_reason": getattr(choice, "finish_reason", None) if choice else None,
    }
