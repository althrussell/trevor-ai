"""Unit tests for ``UCVolumeHome.seed_if_empty``."""

from __future__ import annotations

from pathlib import Path

import pytest

from trevor_databricks.fs.volume_fs import UCVolumeHome


@pytest.fixture
def home(tmp_path: Path) -> UCVolumeHome:
    """An ``UCVolumeHome`` with no live workspace_factory."""
    local = tmp_path / "cache"
    local.mkdir()
    return UCVolumeHome(
        local_root=local,
        volume_root="/Volumes/x/y/z",
        durable_subpaths=("skills",),
        workspace_factory=lambda: (_ for _ in ()).throw(RuntimeError("not used in tests")),
    )


@pytest.fixture
def seed_dir(tmp_path: Path) -> Path:
    src = tmp_path / "seed"
    (src / "skill_a").mkdir(parents=True)
    (src / "skill_a" / "SKILL.md").write_text("# A\n")
    (src / "skill_a" / "example.py").write_text("print('a')\n")
    (src / "skill_b").mkdir()
    (src / "skill_b" / "SKILL.md").write_text("# B\n")
    return src


class TestSeedIfEmpty:
    def test_seeds_into_empty_target(self, home: UCVolumeHome, seed_dir: Path) -> None:
        result = home.seed_if_empty(seed_dir, "skills/databricks", push=False)
        assert result["seeded"] is True
        assert result["files_copied"] == 3
        assert result["bytes_copied"] > 0
        landed = list((home.local_root / "skills" / "databricks").rglob("*"))
        files = [p for p in landed if p.is_file()]
        assert len(files) == 3

    def test_skip_when_target_not_empty(self, home: UCVolumeHome, seed_dir: Path) -> None:
        target = home.local_root / "skills" / "databricks"
        target.mkdir(parents=True, exist_ok=True)
        (target / "existing.md").write_text("# pre-existing")
        result = home.seed_if_empty(seed_dir, "skills/databricks", push=False)
        assert result["seeded"] is False
        assert result["skipped_reason"] == "target_not_empty"
        assert (target / "existing.md").exists()

    def test_skip_when_seed_dir_missing(self, home: UCVolumeHome, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        result = home.seed_if_empty(missing, "skills/databricks", push=False)
        assert result["seeded"] is False
        assert result["skipped_reason"] == "seed_dir_missing"

    def test_rejects_absolute_target(self, home: UCVolumeHome, seed_dir: Path) -> None:
        result = home.seed_if_empty(seed_dir, "/etc/passwd", push=False)
        assert result["seeded"] is False
        assert result["skipped_reason"].startswith("invalid_target")

    def test_idempotent_after_seeding(self, home: UCVolumeHome, seed_dir: Path) -> None:
        first = home.seed_if_empty(seed_dir, "skills/databricks", push=False)
        assert first["seeded"] is True
        second = home.seed_if_empty(seed_dir, "skills/databricks", push=False)
        assert second["seeded"] is False
        assert second["skipped_reason"] == "target_not_empty"
