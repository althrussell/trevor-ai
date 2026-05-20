"""Unit tests for the in-app subprocess terminal backend."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from trevor_databricks.tools import terminal_backend as tb


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "trevor_home"
    h.mkdir()
    return h


def test_validates_list_only_cmd(home: Path) -> None:
    with pytest.raises(ValueError):
        tb.run("echo hi", trevor_home=home)  # type: ignore[arg-type]


def test_validates_non_empty_cmd(home: Path) -> None:
    with pytest.raises(ValueError):
        tb.run([], trevor_home=home)


def test_runs_simple_python_command(home: Path) -> None:
    result = tb.run([sys.executable, "-c", "print('hello-from-test')"], trevor_home=home)
    assert result.ok is True, result.note
    assert result.exit_code == 0
    assert "hello-from-test" in result.stdout
    assert result.cwd.endswith("workspace")


def test_workspace_is_cwd(home: Path) -> None:
    result = tb.run([sys.executable, "-c", "import os; print(os.getcwd())"], trevor_home=home)
    assert result.ok is True
    assert result.stdout.strip().endswith("workspace")


def test_cwd_escape_is_rejected(home: Path) -> None:
    result = tb.run([sys.executable, "-c", "print(1)"], trevor_home=home, cwd="../../etc")
    assert result.ok is False
    # _validate_cwd raises ValueError → caught and surfaced in note
    assert "escapes workspace" in result.note


def test_unknown_executable_returns_diagnostic(home: Path) -> None:
    result = tb.run(["definitely-not-a-real-binary-xyz"], trevor_home=home)
    assert result.ok is False
    assert "not found on PATH" in result.note


def test_denylist_blocks_rm(home: Path) -> None:
    result = tb.run(["rm", "-rf", "/"], trevor_home=home)
    assert result.ok is False
    assert "denylist" in result.note


def test_timeout_short(home: Path) -> None:
    result = tb.run(
        [sys.executable, "-c", "import time; time.sleep(5)"], trevor_home=home, timeout=1
    )
    assert result.ok is False
    assert "timeout" in result.note.lower()


def test_disabled_backend(home: Path) -> None:
    result = tb.run([sys.executable, "-c", "print(1)"], backend="disabled", trevor_home=home)
    assert result.ok is False
    assert "disabled" in result.note


def test_databricks_job_backend_returns_diagnostic(home: Path) -> None:
    result = tb.run([sys.executable, "-c", "print(1)"], backend="databricks_job", trevor_home=home)
    assert result.ok is False
    assert "databricks_job" in result.note


def test_external_sandbox_backend_returns_diagnostic(home: Path) -> None:
    result = tb.run(
        [sys.executable, "-c", "print(1)"], backend="external_sandbox", trevor_home=home
    )
    assert result.ok is False
    assert "external_sandbox" in result.note
