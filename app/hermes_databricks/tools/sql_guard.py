"""Read-only SQL validator for ``databricks_uc_query_readonly``.

Rejects every statement that isn't a single ``SELECT`` or ``WITH`` query.
We do a token-level check rather than full parsing because Databricks
SQL supports a few dialect extensions we don't want to drop on the
floor. The validator is conservative: anything that looks remotely
mutating (INSERT/UPDATE/DELETE/MERGE/COPY/CREATE/ALTER/DROP/TRUNCATE/
GRANT/REVOKE/USE/SET) is rejected. Comments and trailing whitespace
are tolerated.

Multi-statement input is rejected to prevent stacked-queries bypass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_BANNED_LEADS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "COPY", "CREATE", "ALTER",
    "DROP", "TRUNCATE", "GRANT", "REVOKE", "USE", "SET", "REFRESH",
    "VACUUM", "RESTORE", "ANALYZE", "OPTIMIZE", "REPLACE", "REASSIGN",
    "MSCK", "EXPLAIN",  # EXPLAIN runs queries on some engines — be safe
}

_ALLOWED_LEADS = {"SELECT", "WITH"}


@dataclass
class GuardResult:
    ok: bool
    reason: str = ""
    normalised: str = ""


_COMMENT_LINE = re.compile(r"--.*?$", re.MULTILINE)
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)


def strip_comments(sql: str) -> str:
    sql = _COMMENT_BLOCK.sub(" ", sql)
    sql = _COMMENT_LINE.sub(" ", sql)
    return sql


def validate_readonly(sql: str) -> GuardResult:
    """Return ``GuardResult(ok=True)`` if ``sql`` is a single SELECT/WITH query."""
    if not sql or not sql.strip():
        return GuardResult(ok=False, reason="empty SQL")
    cleaned = strip_comments(sql).strip().rstrip(";").strip()
    if not cleaned:
        return GuardResult(ok=False, reason="empty after stripping comments")

    # Reject stacked queries. We do this BEFORE checking the lead token so
    # `SELECT 1; DROP TABLE x` is caught even if it starts with SELECT.
    if ";" in cleaned:
        return GuardResult(ok=False, reason="multiple statements are not allowed")

    head = cleaned.split(None, 1)[0].upper()
    if head in _BANNED_LEADS:
        return GuardResult(ok=False, reason=f"statements starting with {head} are not allowed")
    if head not in _ALLOWED_LEADS:
        return GuardResult(ok=False, reason=f"only SELECT/WITH queries are allowed; got {head}")

    return GuardResult(ok=True, reason="", normalised=cleaned)
