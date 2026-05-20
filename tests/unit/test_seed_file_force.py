"""Unit tests for ``UCVolumeHome.seed_file_force``.

Force-seeding is the only path that keeps the bundled ``soul.md`` in
sync with the UC volume mirror across redeploys. Without it, the first
deploy writes the volume copy and every subsequent deploy skips it via
``seed_file_if_missing``'s ``target_exists`` guard — which means our
system prompt updates never reach the model. The tests below pin down
the contract so a future refactor can't silently regress to the
"missing-only" behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trevor_databricks.fs.volume_fs import UCVolumeHome


@pytest.fixture
def home(tmp_path: Path) -> UCVolumeHome:
    """``UCVolumeHome`` whose workspace_factory would throw if used.

    Force-seeding stays local-only when ``push=False``, so no SDK call
    should ever be issued during these tests.
    """
    local = tmp_path / "cache"
    local.mkdir()
    return UCVolumeHome(
        local_root=local,
        volume_root="/Volumes/x/y/z",
        # soul.md is declared as durable in the production config; declare
        # the same here so the push branch is exercised but a stub
        # workspace would be needed to actually run it (push=False keeps
        # us out of that code path).
        durable_subpaths=("soul.md",),
        workspace_factory=lambda: (_ for _ in ()).throw(RuntimeError("not used in tests")),
    )


@pytest.fixture
def seed_file(tmp_path: Path) -> Path:
    src = tmp_path / "seed-soul.md"
    src.write_text("# bundled soul v2\nrouting rules: SHOW CATALOGS\n", encoding="utf-8")
    return src


class TestSeedFileForce:
    def test_writes_when_target_absent(self, home: UCVolumeHome, seed_file: Path) -> None:
        result = home.seed_file_force(seed_file, "soul.md", push=False)

        assert result["seeded"] is True
        assert result["overwritten"] is False
        assert result["bytes_changed"] is True
        assert result["bytes_copied"] == seed_file.stat().st_size
        landed = (home.local_root / "soul.md").read_text(encoding="utf-8")
        assert "SHOW CATALOGS" in landed

    def test_overwrites_when_target_already_exists(
        self, home: UCVolumeHome, seed_file: Path
    ) -> None:
        target = home.local_root / "soul.md"
        target.write_text("# stale soul v1\nold routing\n", encoding="utf-8")

        result = home.seed_file_force(seed_file, "soul.md", push=False)

        assert result["seeded"] is True
        assert result["overwritten"] is True
        assert result["bytes_changed"] is True
        landed = target.read_text(encoding="utf-8")
        assert "SHOW CATALOGS" in landed
        assert "stale soul v1" not in landed

    def test_is_a_no_op_when_content_unchanged(self, home: UCVolumeHome, seed_file: Path) -> None:
        target = home.local_root / "soul.md"
        target.write_text(seed_file.read_text(encoding="utf-8"), encoding="utf-8")
        before_mtime = target.stat().st_mtime_ns

        result = home.seed_file_force(seed_file, "soul.md", push=False)

        assert result["seeded"] is True
        assert result["overwritten"] is True
        assert result["bytes_changed"] is False
        # File on disk untouched — no rewrite churn.
        assert target.stat().st_mtime_ns == before_mtime

    def test_skip_when_seed_file_missing(self, home: UCVolumeHome, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.md"
        result = home.seed_file_force(missing, "soul.md", push=False)

        assert result["seeded"] is False
        assert result["overwritten"] is False
        assert result["skipped_reason"] == "seed_file_missing"

    def test_rejects_absolute_target(self, home: UCVolumeHome, seed_file: Path) -> None:
        result = home.seed_file_force(seed_file, "/etc/passwd", push=False)

        assert result["seeded"] is False
        assert result["skipped_reason"].startswith("invalid_target")

    def test_rejects_dotdot_target(self, home: UCVolumeHome, seed_file: Path) -> None:
        result = home.seed_file_force(seed_file, "../outside", push=False)

        assert result["seeded"] is False
        assert result["skipped_reason"].startswith("invalid_target")

    def test_seed_file_if_missing_does_not_overwrite(
        self, home: UCVolumeHome, seed_file: Path
    ) -> None:
        """Pin the contrast with the missing-only variant — this is the very
        bug that motivated ``seed_file_force``: bundled soul.md updates
        never reached the volume because the legacy seeder hit
        ``target_exists`` on every redeploy."""
        target = home.local_root / "soul.md"
        target.write_text("# stale soul v1\n", encoding="utf-8")

        if_missing_result = home.seed_file_if_missing(seed_file, "soul.md", push=False)
        assert if_missing_result["seeded"] is False
        assert if_missing_result["skipped_reason"] == "target_exists"
        assert "stale soul v1" in target.read_text(encoding="utf-8")

        force_result = home.seed_file_force(seed_file, "soul.md", push=False)
        assert force_result["seeded"] is True
        assert force_result["overwritten"] is True
        assert "SHOW CATALOGS" in target.read_text(encoding="utf-8")
