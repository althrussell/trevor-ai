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
    "INSERT",
    "UPDATE",
    "DELETE",
    "MERGE",
    "COPY",
    "CREATE",
    "ALTER",
    "DROP",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "USE",
    "SET",
    "REFRESH",
    "VACUUM",
    "RESTORE",
    "ANALYZE",
    "OPTIMIZE",
    "REPLACE",
    "REASSIGN",
    "MSCK",
    "EXPLAIN",  # EXPLAIN runs queries on some engines — be safe
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


# ---------------------------------------------------------------------
# Write-path helpers (used by databricks_sql_execute, not by
# databricks_uc_query_readonly).
# ---------------------------------------------------------------------


# Verbs we consider unconditionally destructive: data is lost, ownership
# is revoked, or the world ends. These require YOLO mode.
DESTRUCTIVE_VERBS: tuple[str, ...] = (
    "DROP",
    "TRUNCATE",
    "REASSIGN",
    "REVOKE",
    "VACUUM",  # Delta VACUUM removes files past the retention horizon
    "PURGE",
)

# A token sequence (regex on the cleaned SQL) we also treat as destructive
# because it removes data from a live table without an explicit predicate.
_DELETE_WITHOUT_WHERE = re.compile(
    r"^\s*DELETE\s+FROM\s+[^;]*?(?<!WHERE\s)$",
    re.IGNORECASE | re.DOTALL,
)
_DELETE_HAS_WHERE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_UPDATE_HAS_WHERE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_ALTER_DROP = re.compile(
    r"^\s*ALTER\s+(TABLE|SCHEMA|CATALOG|VIEW)\b.*\bDROP\b", re.IGNORECASE | re.DOTALL
)

# Match the first 3-part name in the statement (catalog.schema.table).
# Best-effort: handles unquoted identifiers and backtick-quoted parts.
_FQTN_PATTERN = re.compile(
    r"(?<![\w`.])"
    r"(`[^`]+`|[A-Za-z_][\w]*)"
    r"\s*\.\s*"
    r"(`[^`]+`|[A-Za-z_][\w]*)"
    r"\s*\.\s*"
    r"(`[^`]+`|[A-Za-z_][\w]*)"
)


def _strip_backticks(part: str) -> str:
    return part.strip().strip("`")


def parse_targets(sql: str) -> list[str]:
    """Return the catalog.schema.table targets referenced in ``sql``.

    Best-effort. Used to enforce the write allowlist for
    ``databricks_sql_execute``. False positives (e.g. matches inside a
    string literal) are tolerated — the allowlist check fails closed,
    so a spurious match merely forces the operator to widen the allow
    list explicitly.
    """
    if not sql:
        return []
    cleaned = strip_comments(sql)
    seen: list[str] = []
    for match in _FQTN_PATTERN.finditer(cleaned):
        full = ".".join(_strip_backticks(p) for p in match.groups())
        if full and full not in seen:
            seen.append(full)
    return seen


def find_destructive_verbs(sql: str) -> list[str]:
    """Return a list of destructive verbs / patterns present in ``sql``.

    Empty list = safe to run when ``WRITES_ENABLED=true`` without
    YOLO. Non-empty list = YOLO required.
    """
    if not sql:
        return []
    cleaned = strip_comments(sql).strip().rstrip(";").strip()
    if not cleaned:
        return []

    head = cleaned.split(None, 1)[0].upper()
    flagged: list[str] = []

    if head in DESTRUCTIVE_VERBS:
        flagged.append(head)

    if head == "DELETE" and not _DELETE_HAS_WHERE.search(cleaned):
        flagged.append("DELETE_WITHOUT_WHERE")

    if head == "UPDATE" and not _UPDATE_HAS_WHERE.search(cleaned):
        flagged.append("UPDATE_WITHOUT_WHERE")

    if _ALTER_DROP.search(cleaned):
        flagged.append("ALTER_DROP")

    # CREATE OR REPLACE is a silent destructive op for tables/views (the
    # existing object is replaced). We flag it so the operator opts in.
    if re.match(r"^\s*CREATE\s+OR\s+REPLACE\b", cleaned, re.IGNORECASE):
        flagged.append("CREATE_OR_REPLACE")

    return flagged


def normalise_for_execute(sql: str) -> str:
    """Clean SQL for handoff to the SQL warehouse.

    Strips comments, trailing semicolons, and collapses leading/trailing
    whitespace. Does NOT validate; that's the caller's job (via
    ``validate_readonly`` for the read tool or
    ``find_destructive_verbs`` for the write tool).
    """
    return strip_comments(sql).strip().rstrip(";").strip()
