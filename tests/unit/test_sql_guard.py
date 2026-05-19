"""Unit tests for the read-only SQL guard."""

from __future__ import annotations

import pytest

from hermes_databricks.tools.sql_guard import validate_readonly


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select 1",
        "SELECT * FROM main.default.t LIMIT 10",
        "WITH q AS (SELECT 1) SELECT * FROM q",
        "  -- a comment\n  SELECT 1  ;  ",
        "/* block */ SELECT 1",
    ],
)
def test_allows_select_and_with(sql: str) -> None:
    assert validate_readonly(sql).ok is True


@pytest.mark.parametrize(
    "sql,reason_frag",
    [
        ("INSERT INTO t VALUES (1)", "INSERT"),
        ("UPDATE t SET a=1", "UPDATE"),
        ("DELETE FROM t", "DELETE"),
        ("MERGE INTO t USING s ON 1=1", "MERGE"),
        ("DROP TABLE t", "DROP"),
        ("CREATE TABLE t (a INT)", "CREATE"),
        ("ALTER TABLE t ADD COLUMN b INT", "ALTER"),
        ("COPY INTO t FROM '/Volumes/x'", "COPY"),
        ("USE catalog main", "USE"),
        ("SET spark.sql.shuffle=10", "SET"),
        ("TRUNCATE TABLE t", "TRUNCATE"),
        ("GRANT SELECT ON t TO u", "GRANT"),
    ],
)
def test_rejects_mutating_statements(sql: str, reason_frag: str) -> None:
    result = validate_readonly(sql)
    assert result.ok is False
    assert reason_frag in result.reason


def test_rejects_stacked_queries() -> None:
    result = validate_readonly("SELECT 1; DROP TABLE x")
    assert result.ok is False
    assert "multiple statements" in result.reason


def test_rejects_empty() -> None:
    assert validate_readonly("").ok is False
    assert validate_readonly("   \n  ").ok is False


def test_rejects_comment_only() -> None:
    assert validate_readonly("-- just a comment").ok is False
