"""Regression guard for ``app/seeds/home/soul.md``.

This file is the system prompt seeded into HERMES_HOME on first boot.
The model relies on the bullets below to answer "list databases /
catalogs / schemas / tables" with a single ``databricks_uc_query_readonly``
call instead of:

  * spawning a subagent via ``delegate_task`` (which would crash on
    Databricks streaming — see ``install_subagent_hook``),
  * loading a CLI-flavoured skill and shelling out to the ``databricks``
    binary via ``databricks_terminal``,
  * hallucinating a ``execute_code`` tool that isn't registered.

If any of these bullets get accidentally edited out, the agent slides
back into the 38-call spiral the original log dump documented.
Update this test deliberately if you change the soul.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SOUL_PATH = (
    Path(__file__).resolve().parent.parent.parent / "app" / "seeds" / "home" / "soul.md"
)


@pytest.fixture(scope="module")
def soul_text() -> str:
    assert SOUL_PATH.exists(), f"soul.md missing at {SOUL_PATH}"
    return SOUL_PATH.read_text(encoding="utf-8")


def test_show_catalogs_routing_present(soul_text: str) -> None:
    """The 'list databases / catalogs' → SHOW CATALOGS rule must survive edits."""
    assert "SHOW CATALOGS" in soul_text
    assert "databricks_uc_query_readonly" in soul_text


def test_show_schemas_routing_present(soul_text: str) -> None:
    assert "SHOW SCHEMAS IN" in soul_text


def test_show_tables_routing_present(soul_text: str) -> None:
    assert "SHOW TABLES IN" in soul_text


def test_no_databricks_cli_for_listings(soul_text: str) -> None:
    """The soul must tell the agent NOT to shell out to the ``databricks`` CLI."""
    assert "databricks_terminal" in soul_text
    # The capitalised "NOT" is the explicit prohibition marker we set.
    lowered = soul_text.lower()
    assert "do **not** shell out" in lowered or "do not shell out" in lowered


def test_execute_code_is_marked_unavailable(soul_text: str) -> None:
    """``execute_code`` is not registered in this deployment; the soul must say so."""
    assert "execute_code" in soul_text
    lowered = soul_text.lower()
    assert "not available" in lowered or "do not call it" in lowered


def test_delegate_task_disabled_callout(soul_text: str) -> None:
    """``delegate_task`` is off; the soul must tell the agent not to reach for it."""
    assert "delegate_task" in soul_text
    lowered = soul_text.lower()
    assert "disabled" in lowered or "do not try to delegate" in lowered
