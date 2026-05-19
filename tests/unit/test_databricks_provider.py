"""Unit tests for the Databricks request sanitiser.

The sanitiser is the only thing standing between Hermes' OpenAI-shaped
``chat.completions.create`` kwargs and the Databricks Model Serving
proxy, which is stricter than vanilla OpenAI. These tests pin down the
three current scrubs:

1. ``stream_options`` — Databricks 400s with
   ``Bad request: json: unknown field "stream_options"``.
2. Per-message ``_``-prefixed Hermes-internal flags (e.g.
   ``_empty_recovery_synthetic``, ``_empty_terminal_sentinel``,
   ``_thinking_prefill``) — same class of 400.
3. Integer-only JSON-Schema constraints (``minimum``/``maximum``/...) on
   tool definitions — Databricks 400s with
   ``Invalid JSON schema - integer types do not support minimum``.

Plus the patch-installation contract:

* ``_install_request_sanitiser`` is idempotent (a second call is a no-op
  and the marker attribute is set on the wrapped ``create``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from hermes_databricks import databricks_provider as dp

# ---------------------------------------------------------------------
# _sanitise_oai_kwargs — message-level scrub
# ---------------------------------------------------------------------


def test_sanitise_strips_underscore_message_keys():
    """All ``_``-prefixed keys must be removed from every message dict."""
    messages = [
        {"role": "system", "content": "you are helpful"},
        {
            "role": "assistant",
            "content": "(empty)",
            "_empty_recovery_synthetic": True,
            "_thinking_prefill": True,
        },
        {
            "role": "user",
            "content": "carry on",
            "_empty_recovery_synthetic": True,
        },
    ]
    out = dp._sanitise_oai_kwargs({"messages": messages, "model": "x"})

    out_messages = out["messages"]
    assert all(
        not any(k.startswith("_") for k in msg) for msg in out_messages if isinstance(msg, dict)
    )
    # Non-internal fields are preserved.
    assert out_messages[1]["role"] == "assistant"
    assert out_messages[1]["content"] == "(empty)"
    assert out_messages[2]["content"] == "carry on"


def test_sanitise_does_not_mutate_caller_messages():
    """Hermes still needs the flags on its in-memory ``messages`` list for its
    own scaffolding-pop logic. The sanitiser must copy-on-write, not mutate.
    """
    original_msg = {
        "role": "assistant",
        "content": "(empty)",
        "_empty_recovery_synthetic": True,
    }
    messages = [{"role": "user", "content": "hi"}, original_msg]
    snapshot_keys = set(original_msg.keys())

    out = dp._sanitise_oai_kwargs({"messages": messages, "model": "x"})

    assert set(original_msg.keys()) == snapshot_keys
    assert original_msg["_empty_recovery_synthetic"] is True
    assert messages[1] is original_msg  # caller's list untouched
    assert out["messages"][1] is not original_msg  # outbound copy


def test_sanitise_is_allocation_free_when_messages_are_clean():
    """No copy should happen when no message carries a stripping candidate."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    out = dp._sanitise_oai_kwargs({"messages": messages, "model": "x"})

    # Same list and same dicts: copy-on-write means no copy.
    assert out["messages"] is messages
    for i, m in enumerate(out["messages"]):
        assert m is messages[i]


def test_sanitise_handles_non_dict_message_entries_gracefully():
    """Defensive: a stray non-dict in ``messages`` must not crash the scrub."""
    messages = [
        {"role": "user", "content": "hi"},
        "not-a-dict",  # type: ignore[list-item]
        {"role": "assistant", "_thinking_prefill": True, "content": "ok"},
    ]
    out = dp._sanitise_oai_kwargs({"messages": messages, "model": "x"})

    assert out["messages"][1] == "not-a-dict"
    assert "_thinking_prefill" not in out["messages"][2]


# ---------------------------------------------------------------------
# _sanitise_oai_kwargs — stream_options strip
# ---------------------------------------------------------------------


def test_sanitise_strips_stream_options():
    kwargs = {
        "model": "x",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    out = dp._sanitise_oai_kwargs(kwargs)
    assert "stream_options" not in out
    assert out["stream"] is True  # other fields preserved
    # Caller kwargs untouched (copy-on-write).
    assert "stream_options" in kwargs


# ---------------------------------------------------------------------
# _sanitise_oai_kwargs — integer-schema-constraint strip
# ---------------------------------------------------------------------


def test_sanitise_strips_integer_schema_constraints():
    """Integer-typed tool parameters must lose ``minimum``/``maximum``/etc."""
    tool = {
        "type": "function",
        "function": {
            "name": "page",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "exclusiveMinimum": 0,
                        "exclusiveMaximum": 101,
                        "multipleOf": 1,
                    },
                    "name": {"type": "string", "minLength": 1},
                },
            },
        },
    }
    out = dp._sanitise_oai_kwargs(
        {"model": "x", "messages": [{"role": "user", "content": "go"}], "tools": [tool]}
    )

    cleaned_n = out["tools"][0]["function"]["parameters"]["properties"]["n"]
    for k in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        assert k not in cleaned_n
    # Non-integer constraints on other types are left alone.
    cleaned_name = out["tools"][0]["function"]["parameters"]["properties"]["name"]
    assert cleaned_name["minLength"] == 1

    # Caller tool dict untouched.
    assert tool["function"]["parameters"]["properties"]["n"]["minimum"] == 1


def test_sanitise_strips_integer_schema_constraints_in_array_type():
    """``type: ["integer", "null"]`` arrays count as integer for the strip."""
    tool = {
        "type": "function",
        "function": {
            "name": "x",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {"type": ["integer", "null"], "minimum": 0},
                },
            },
        },
    }
    out = dp._sanitise_oai_kwargs(
        {"model": "x", "messages": [{"role": "user", "content": "go"}], "tools": [tool]}
    )
    assert "minimum" not in out["tools"][0]["function"]["parameters"]["properties"]["n"]


# ---------------------------------------------------------------------
# _sanitise_oai_kwargs — array-schema-constraint strip
# ---------------------------------------------------------------------
#
# Motivated by the production 400 observed in the post-turn skill-review
# background agent on 2026-05-19:
#   ``Invalid JSON schema - array types do not support maxItems``
# Hermes' tool registry ships array-typed parameters with maxItems /
# minItems / uniqueItems constraints. Databricks rejects all of them.


def test_sanitise_strips_array_schema_constraints():
    """Array-typed tool parameters must lose ``maxItems``/``minItems``/etc."""
    tool = {
        "type": "function",
        "function": {
            "name": "search",
            "parameters": {
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 10,
                        "minItems": 1,
                        "uniqueItems": True,
                        "maxContains": 5,
                        "minContains": 1,
                    },
                    "query": {"type": "string"},
                },
            },
        },
    }
    out = dp._sanitise_oai_kwargs(
        {"model": "x", "messages": [{"role": "user", "content": "go"}], "tools": [tool]}
    )

    cleaned_tags = out["tools"][0]["function"]["parameters"]["properties"]["tags"]
    for k in ("maxItems", "minItems", "uniqueItems", "maxContains", "minContains"):
        assert k not in cleaned_tags, f"{k!r} should have been stripped from array type"
    # Non-constraint fields on the array node survive.
    assert cleaned_tags["type"] == "array"
    assert cleaned_tags["items"] == {"type": "string"}
    # Sibling string property untouched.
    assert out["tools"][0]["function"]["parameters"]["properties"]["query"] == {
        "type": "string"
    }
    # Caller tool dict untouched.
    assert tool["function"]["parameters"]["properties"]["tags"]["maxItems"] == 10


def test_sanitise_strips_array_schema_constraints_in_type_union():
    """``type: ["array", "null"]`` counts as array for the strip."""
    tool = {
        "type": "function",
        "function": {
            "name": "x",
            "parameters": {
                "type": "object",
                "properties": {
                    "tags": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "maxItems": 3,
                    },
                },
            },
        },
    }
    out = dp._sanitise_oai_kwargs(
        {"model": "x", "messages": [{"role": "user", "content": "go"}], "tools": [tool]}
    )
    cleaned = out["tools"][0]["function"]["parameters"]["properties"]["tags"]
    assert "maxItems" not in cleaned
    assert cleaned["type"] == ["array", "null"]
    assert cleaned["items"] == {"type": "string"}


def test_sanitise_does_not_strip_constraints_from_wrong_type():
    """Don't strip ``maxItems`` from a non-array node, even if the key exists.

    Real tool schemas should never put ``maxItems`` on a non-array node,
    but the recursive walk must be type-scoped — otherwise we'd start
    silently mutating schema keywords the validator does accept on other
    types, which would be much harder to debug than a 400.
    """
    tool = {
        "type": "function",
        "function": {
            "name": "x",
            "parameters": {
                "type": "object",
                "properties": {
                    "weird": {
                        "type": "string",
                        # Not legal on string per the JSON-Schema spec, but
                        # the sanitiser is not a schema validator; it
                        # narrowly removes only what triggers Databricks
                        # 400s and only on the types those 400s name.
                        "maxItems": 1,
                    },
                },
            },
        },
    }
    out = dp._sanitise_oai_kwargs(
        {"model": "x", "messages": [{"role": "user", "content": "go"}], "tools": [tool]}
    )
    cleaned_weird = out["tools"][0]["function"]["parameters"]["properties"]["weird"]
    assert cleaned_weird["maxItems"] == 1


def test_sanitise_strips_nested_array_and_integer_in_one_pass():
    """A schema that mixes both forbidden families must be cleaned in one walk."""
    tool = {
        "type": "function",
        "function": {
            "name": "page",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "minimum": 1, "maximum": 100},
                    "tags": {
                        "type": "array",
                        "items": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "maxItems": 5,
                    },
                },
            },
        },
    }
    out = dp._sanitise_oai_kwargs(
        {"model": "x", "messages": [{"role": "user", "content": "go"}], "tools": [tool]}
    )
    props = out["tools"][0]["function"]["parameters"]["properties"]
    assert "minimum" not in props["n"]
    assert "maximum" not in props["n"]
    assert "maxItems" not in props["tags"]
    # Items-recursion: nested integer constraint must also be gone.
    assert "minimum" not in props["tags"]["items"]


def test_scrub_helper_returns_total_removed_count():
    """``_scrub_unsupported_schema_constraints`` must report how many keys were
    removed, so the debug log line can stay informative.
    """
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "integer", "minimum": 1, "maximum": 2, "multipleOf": 3},
            "b": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        },
    }
    removed = dp._scrub_unsupported_schema_constraints(schema)
    assert removed == 4  # minimum, maximum, multipleOf, maxItems


def test_legacy_scrub_alias_still_works():
    """``_scrub_integer_schema_constraints`` is aliased to the unified scrubber
    for backwards compatibility with any downstream import path. Drop this
    test when the alias is removed (no callers remain in-repo today)."""
    assert (
        dp._scrub_integer_schema_constraints
        is dp._scrub_unsupported_schema_constraints
    )


# ---------------------------------------------------------------------
# _install_request_sanitiser — class patch contract
# ---------------------------------------------------------------------


class _FakeCompletions:
    """Stand-in for ``openai.resources.chat.completions.Completions``."""

    def create(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"args": args, "kwargs": kwargs}


class _FakeAsyncCompletions:
    async def create(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"args": args, "kwargs": kwargs}


@pytest.fixture
def fake_openai_completions(monkeypatch):
    """Swap the real openai Completions classes for fakes, then restore."""
    import openai.resources.chat.completions as oai_chat

    fake_sync = type("Completions", (_FakeCompletions,), {})
    fake_async = type("AsyncCompletions", (_FakeAsyncCompletions,), {})

    monkeypatch.setattr(oai_chat, "Completions", fake_sync, raising=True)
    monkeypatch.setattr(oai_chat, "AsyncCompletions", fake_async, raising=True)
    yield fake_sync, fake_async


def test_install_request_sanitiser_is_idempotent(fake_openai_completions):
    fake_sync, _fake_async = fake_openai_completions

    dp._install_request_sanitiser(SimpleNamespace())  # arg is unused by impl
    first_create = fake_sync.create
    assert getattr(first_create, "_hermes_databricks_sanitised", False) is True

    dp._install_request_sanitiser(SimpleNamespace())
    second_create = fake_sync.create
    assert second_create is first_create  # not re-wrapped
    assert getattr(second_create, "_hermes_databricks_sanitised", False) is True


def test_installed_sanitiser_actually_strips_outbound_kwargs(fake_openai_completions):
    fake_sync, _ = fake_openai_completions

    dp._install_request_sanitiser(SimpleNamespace())

    instance = fake_sync()
    result = instance.create(
        model="x",
        stream_options={"include_usage": True},
        messages=[
            {"role": "user", "content": "hi", "_empty_recovery_synthetic": True},
        ],
    )

    sent = result["kwargs"]
    assert "stream_options" not in sent
    assert "_empty_recovery_synthetic" not in sent["messages"][0]
    assert sent["messages"][0]["content"] == "hi"


# ---------------------------------------------------------------------
# apply_to_agent — flips _disable_streaming and the Databricks sentinel
# ---------------------------------------------------------------------


class _FakeAgent:
    """Minimal stand-in for AIAgent. Owns only the attributes apply_to_agent touches."""

    def __init__(self, provider: str = "databricks") -> None:
        self.provider = provider
        self.model = "placeholder"
        self.base_url = "https://placeholder.invalid/v1"
        self.api_key = "placeholder"
        self.client = None  # type: ignore[assignment]
        self._client_kwargs: dict[str, Any] = {}
        self._disable_streaming = False
        self.log_prefix = ""


class _FakeWorkspace:
    """Stand-in for ``WorkspaceClient`` returning a controllable OpenAI fake."""

    def __init__(self, base_url: str = "https://dbc-test.cloud.databricks.com/serving-endpoints"):
        self.config = SimpleNamespace(
            authenticate=lambda: {"Authorization": "Bearer dapi-FAKE-TOKEN"}
        )
        self._oai = SimpleNamespace(base_url=base_url, _client=SimpleNamespace())
        self.serving_endpoints = SimpleNamespace(get_open_ai_client=lambda: self._oai)


def test_apply_to_agent_marks_agent_and_disables_streaming(fake_openai_completions):
    """``apply_to_agent`` must flip ``_disable_streaming`` and the sentinel."""
    factory = dp.DatabricksOpenAIClientFactory(
        endpoint="databricks-test",
        workspace_factory=_FakeWorkspace,
    )
    agent = _FakeAgent()
    factory.apply_to_agent(agent)

    assert agent._disable_streaming is True
    assert getattr(agent, "_databricks_applied", False) is True
    assert agent.model == "databricks-test"
    assert agent.base_url and "serving-endpoints" in agent.base_url
    assert agent.api_key == "dapi-FAKE-TOKEN"
    assert agent._client_kwargs["base_url"] == agent.base_url
    assert agent._client_kwargs["api_key"] == "dapi-FAKE-TOKEN"


# ---------------------------------------------------------------------
# install_subagent_hook — patches AIAgent.__init__ so children inherit
# ---------------------------------------------------------------------


def test_install_subagent_hook_re_applies_to_fresh_databricks_agents(
    fake_openai_completions, monkeypatch
):
    """A fresh ``AIAgent(provider='databricks', ...)`` must get our overrides."""

    # Replace ``run_agent.AIAgent`` with a fake class so the hook has
    # something to patch without dragging the real (heavy) AIAgent in.
    class FakeAIAgent:
        def __init__(self, *, provider: str = "databricks", model: str = "any-model"):
            self.provider = provider
            self.model = model
            self.base_url = "https://placeholder.invalid/v1"
            self.api_key = "placeholder"
            self.client = None
            self._client_kwargs: dict[str, Any] = {}
            self._disable_streaming = False
            self.log_prefix = ""

    fake_run_agent = SimpleNamespace(AIAgent=FakeAIAgent)
    monkeypatch.setitem(__import__("sys").modules, "run_agent", fake_run_agent)

    factory = dp.DatabricksOpenAIClientFactory(
        endpoint="databricks-test",
        workspace_factory=_FakeWorkspace,
    )

    assert factory.install_subagent_hook() is True

    # Construct a "subagent" — the hook should run apply_to_agent for us.
    child = FakeAIAgent(provider="databricks", model="any-model")
    assert child._disable_streaming is True
    assert child._databricks_applied is True
    assert child.api_key == "dapi-FAKE-TOKEN"
    assert "serving-endpoints" in (child.base_url or "")


def test_install_subagent_hook_skips_non_databricks_children(
    fake_openai_completions, monkeypatch
):
    """Children whose provider isn't 'databricks' must be left alone."""

    class FakeAIAgent:
        def __init__(self, *, provider: str = "openrouter"):
            self.provider = provider
            self.model = "x"
            self.base_url = "https://other.invalid"
            self.api_key = "other-key"
            self.client = None
            self._client_kwargs: dict[str, Any] = {}
            self._disable_streaming = False
            self.log_prefix = ""

    fake_run_agent = SimpleNamespace(AIAgent=FakeAIAgent)
    monkeypatch.setitem(__import__("sys").modules, "run_agent", fake_run_agent)

    factory = dp.DatabricksOpenAIClientFactory(
        endpoint="databricks-test",
        workspace_factory=_FakeWorkspace,
    )
    factory.install_subagent_hook()

    child = FakeAIAgent(provider="openrouter")
    assert child._disable_streaming is False
    assert getattr(child, "_databricks_applied", False) is False
    assert child.api_key == "other-key"
    assert child.base_url == "https://other.invalid"


def test_install_subagent_hook_is_idempotent(fake_openai_completions, monkeypatch):
    """Second install on the same AIAgent class must be a no-op (no double-wrap)."""

    class FakeAIAgent:
        def __init__(self, *, provider: str = "databricks"):
            self.provider = provider
            self.model = "x"
            self.base_url = "x"
            self.api_key = "x"
            self.client = None
            self._client_kwargs: dict[str, Any] = {}
            self._disable_streaming = False
            self.log_prefix = ""

    fake_run_agent = SimpleNamespace(AIAgent=FakeAIAgent)
    monkeypatch.setitem(__import__("sys").modules, "run_agent", fake_run_agent)

    factory = dp.DatabricksOpenAIClientFactory(
        endpoint="databricks-test",
        workspace_factory=_FakeWorkspace,
    )

    assert factory.install_subagent_hook() is True
    first_init = FakeAIAgent.__init__
    assert getattr(first_init, "_hermes_databricks_subagent_hook", False) is True

    assert factory.install_subagent_hook() is True
    second_init = FakeAIAgent.__init__
    assert second_init is first_init  # not re-wrapped


def test_install_subagent_hook_skips_already_applied_agents(
    fake_openai_completions, monkeypatch
):
    """If __init__ leaves _databricks_applied=True (parent path), don't double-apply."""

    apply_calls: list[Any] = []

    class FakeAIAgent:
        def __init__(self, *, provider: str = "databricks"):
            self.provider = provider
            self.model = "x"
            self.base_url = "x"
            self.api_key = "x"
            self.client = None
            self._client_kwargs: dict[str, Any] = {}
            self._disable_streaming = False
            self._databricks_applied = True  # simulate parent's explicit wiring
            self.log_prefix = ""

    fake_run_agent = SimpleNamespace(AIAgent=FakeAIAgent)
    monkeypatch.setitem(__import__("sys").modules, "run_agent", fake_run_agent)

    factory = dp.DatabricksOpenAIClientFactory(
        endpoint="databricks-test",
        workspace_factory=_FakeWorkspace,
    )

    original_apply = factory.apply_to_agent

    def tracked_apply(agent):
        apply_calls.append(agent)
        return original_apply(agent)

    monkeypatch.setattr(factory, "apply_to_agent", tracked_apply)
    factory.install_subagent_hook()

    FakeAIAgent(provider="databricks")
    assert apply_calls == []  # never re-applied
