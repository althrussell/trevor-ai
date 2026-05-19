"""Unit tests for the write-path helpers in ``sql_guard``."""

from __future__ import annotations

import pytest

from hermes_databricks.tools.sql_guard import (
    DESTRUCTIVE_VERBS,
    find_destructive_verbs,
    normalise_for_execute,
    parse_targets,
)


class TestFindDestructiveVerbs:
    @pytest.mark.parametrize(
        "sql,expected",
        [
            ("SELECT 1", []),
            ("INSERT INTO main.s.t VALUES (1)", []),
            ("UPDATE main.s.t SET a=1 WHERE id=2", []),
            ("DELETE FROM main.s.t WHERE id=2", []),
            ("CREATE TABLE main.s.t (id INT)", []),
            ("ALTER TABLE main.s.t ADD COLUMN b INT", []),
        ],
    )
    def test_non_destructive(self, sql: str, expected: list[str]) -> None:
        assert find_destructive_verbs(sql) == expected

    @pytest.mark.parametrize(
        "sql,verb",
        [
            ("DROP TABLE main.s.t", "DROP"),
            ("TRUNCATE TABLE main.s.t", "TRUNCATE"),
            ("VACUUM main.s.t", "VACUUM"),
            ("PURGE main.s.t", "PURGE"),
            ("REVOKE SELECT ON main.s.t FROM `x`", "REVOKE"),
        ],
    )
    def test_destructive_lead_verbs(self, sql: str, verb: str) -> None:
        flagged = find_destructive_verbs(sql)
        assert verb in flagged

    def test_delete_without_where(self) -> None:
        flagged = find_destructive_verbs("DELETE FROM main.s.t")
        assert "DELETE_WITHOUT_WHERE" in flagged

    def test_delete_with_where_is_safe(self) -> None:
        assert find_destructive_verbs("DELETE FROM main.s.t WHERE id=1") == []

    def test_update_without_where(self) -> None:
        flagged = find_destructive_verbs("UPDATE main.s.t SET a=2")
        assert "UPDATE_WITHOUT_WHERE" in flagged

    def test_alter_drop_column(self) -> None:
        flagged = find_destructive_verbs("ALTER TABLE main.s.t DROP COLUMN b")
        assert "ALTER_DROP" in flagged

    def test_create_or_replace(self) -> None:
        flagged = find_destructive_verbs(
            "CREATE OR REPLACE TABLE main.s.t AS SELECT 1"
        )
        assert "CREATE_OR_REPLACE" in flagged

    def test_empty_input(self) -> None:
        assert find_destructive_verbs("") == []
        assert find_destructive_verbs("   \n") == []
        assert find_destructive_verbs("-- only a comment") == []

    def test_verbs_list_is_canonical(self) -> None:
        """If we ever change DESTRUCTIVE_VERBS, the docs must stay in sync."""
        assert "DROP" in DESTRUCTIVE_VERBS
        assert "TRUNCATE" in DESTRUCTIVE_VERBS
        assert "VACUUM" in DESTRUCTIVE_VERBS


class TestParseTargets:
    def test_picks_up_three_part_names(self) -> None:
        sql = "SELECT * FROM main.s.t JOIN other.s2.tt ON main.s.t.id = other.s2.tt.id"
        targets = parse_targets(sql)
        assert "main.s.t" in targets
        assert "other.s2.tt" in targets

    def test_backticked_identifiers(self) -> None:
        sql = "INSERT INTO `my-cat`.`my schema`.`tbl` VALUES (1)"
        targets = parse_targets(sql)
        assert "my-cat.my schema.tbl" in targets

    def test_no_fqn_returns_empty(self) -> None:
        assert parse_targets("SELECT 1") == []
        assert parse_targets("USE SCHEMA foo") == []

    def test_strips_comments(self) -> None:
        sql = "-- DROP TABLE evil.s.t\nSELECT * FROM real.s.t"
        targets = parse_targets(sql)
        assert "real.s.t" in targets
        assert "evil.s.t" not in targets


class TestNormaliseForExecute:
    def test_strips_trailing_semicolon(self) -> None:
        assert normalise_for_execute("SELECT 1;") == "SELECT 1"

    def test_strips_comments(self) -> None:
        assert "DROP" not in normalise_for_execute("-- DROP\nSELECT 1")

    def test_blank(self) -> None:
        assert normalise_for_execute("   ") == ""
