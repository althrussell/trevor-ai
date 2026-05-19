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
