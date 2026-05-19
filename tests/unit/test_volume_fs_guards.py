"""Unit tests for ``UCVolumeHome`` path guards.

We construct the home directly (no SDK), exercise the guards, and
verify the read/write/list/delete cache operations work in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_databricks.fs.volume_fs import UCVolumeHome


@pytest.fixture()
def home(tmp_path: Path) -> UCVolumeHome:
    return UCVolumeHome(
        local_root=tmp_path / "hermes_home",
        volume_root="/Volumes/main/agents/hermes_home",
    )


def test_rejects_invalid_volume_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        UCVolumeHome(local_root=tmp_path, volume_root="/not/a/volume")


def test_local_resolution_basic(home: UCVolumeHome) -> None:
    p = home._resolve_local("config.yaml")
    assert p.name == "config.yaml"
    assert str(p).startswith(str(home.local_root))


def test_local_rejects_absolute_path(home: UCVolumeHome) -> None:
    with pytest.raises(ValueError):
        home._resolve_local("/etc/passwd")


def test_local_rejects_windows_drive(home: UCVolumeHome) -> None:
    with pytest.raises(ValueError):
        home._resolve_local("C:/Windows/system32")


def test_local_rejects_dotdot(home: UCVolumeHome) -> None:
    with pytest.raises(ValueError):
        home._resolve_local("../escape.txt")


def test_local_rejects_nested_dotdot(home: UCVolumeHome) -> None:
    with pytest.raises(ValueError):
        home._resolve_local("skills/../../escape.txt")


def test_volume_resolution_basic(home: UCVolumeHome) -> None:
    p = home._resolve_volume("skills/example.md")
    assert p == "/Volumes/main/agents/hermes_home/skills/example.md"


def test_volume_rejects_absolute(home: UCVolumeHome) -> None:
    with pytest.raises(ValueError):
        home._resolve_volume("/Volumes/other/hijack")


def test_write_and_read_text_round_trip(home: UCVolumeHome) -> None:
    home.write_text("notes/hello.txt", "world", push=False)
    assert home.exists("notes/hello.txt")
    assert home.read_text("notes/hello.txt") == "world"


def test_durable_classification(home: UCVolumeHome) -> None:
    assert home._is_durable("skills/sub/file.md") is True
    assert home._is_durable("config.yaml") is True
    assert home._is_durable("tmp/scratch.txt") is False


def test_exclude_classification(home: UCVolumeHome) -> None:
    assert home._is_excluded("state.db") is True
    assert home._is_excluded("logs/agent.log") is True
    assert home._is_excluded("skills/keep.md") is False


def test_list_returns_sorted_entries(home: UCVolumeHome) -> None:
    home.write_text("notes/a.txt", "1", push=False)
    home.write_text("notes/b.txt", "2", push=False)
    listing = home.list("notes")
    names = [entry["name"] for entry in listing]
    assert names == sorted(names)
    assert {"a.txt", "b.txt"}.issubset(set(names))


def test_delete_local_only(home: UCVolumeHome) -> None:
    home.write_text("notes/zap.txt", "x", push=False)
    home.delete("notes/zap.txt", also_volume=False)
    assert not home.exists("notes/zap.txt")
