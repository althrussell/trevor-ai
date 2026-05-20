"""Smoke tests for the vendored ai-dev-kit skills bundle."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SEED_ROOT = Path(__file__).resolve().parents[2] / "app" / "seeds" / "skills" / "databricks"

REQUIRED_TOP_LEVEL_FILES = (
    "SKILLS_VERSION",
    "VENDOR_README.md",
    "INDEX.md",
    "UPSTREAM_LICENSE.md",
    "UPSTREAM_NOTICE.txt",
)


def test_seed_root_exists() -> None:
    assert SEED_ROOT.exists(), f"vendored skills root missing: {SEED_ROOT}"


@pytest.mark.parametrize("fname", REQUIRED_TOP_LEVEL_FILES)
def test_required_top_level_file_present(fname: str) -> None:
    assert (SEED_ROOT / fname).is_file(), f"required file missing: {fname}"


def test_skills_version_pin_is_resolved() -> None:
    pin = (SEED_ROOT / "SKILLS_VERSION").read_text()
    assert re.search(r"^tag:\s*\S+", pin, re.MULTILINE), "tag missing from SKILLS_VERSION"
    assert re.search(r"^commit:\s*[0-9a-f]{7,40}", pin, re.MULTILINE), (
        "commit SHA missing from SKILLS_VERSION"
    )


def test_index_lists_at_least_one_skill_per_row() -> None:
    index_text = (SEED_ROOT / "INDEX.md").read_text()
    rows = [line for line in index_text.splitlines() if line.startswith("| `")]
    skill_dirs = [
        p.parent.name for p in SEED_ROOT.glob("*/SKILL.md") if p.parent.name != "TEMPLATE"
    ]
    assert len(skill_dirs) >= 20, "expected at least 20 vendored skills"
    indexed = {row.split("`")[1] for row in rows}
    for skill in skill_dirs:
        assert skill in indexed, f"skill {skill} missing from INDEX.md"


@pytest.mark.parametrize(
    "skill_md",
    [p for p in SEED_ROOT.glob("*/SKILL.md") if p.parent.name != "TEMPLATE"],
    ids=lambda p: p.parent.name,
)
def test_skill_md_has_frontmatter_and_description(skill_md: Path) -> None:
    text = skill_md.read_text()
    fm = re.search(r"^---\s*\n(.*?)\n---", text, re.DOTALL | re.MULTILINE)
    assert fm is not None, f"missing YAML frontmatter in {skill_md}"
    body = fm.group(1)
    assert re.search(r"^name:\s*\S+", body, re.MULTILINE), f"missing 'name:' in {skill_md}"
    assert re.search(r"^description:", body, re.MULTILINE), f"missing 'description:' in {skill_md}"


def test_upstream_license_is_databricks_license() -> None:
    text = (SEED_ROOT / "UPSTREAM_LICENSE.md").read_text()
    assert "Databricks Services" in text, "upstream license text changed shape"
