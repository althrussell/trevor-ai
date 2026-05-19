#!/usr/bin/env python3
"""Stdlib-only smoke check that exercises the critical pure-Python
modules of hermes-on-databricks without requiring pytest or any
network-dependent SDK call.

Run from the repo root:

    PYTHONPATH=app python3 tests/run_offline_checks.py

Exits 0 on success, 1 on any assertion failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "app"
sys.path.insert(0, str(APP))


_failures: list[str] = []
_passes: list[str] = []


@contextmanager
def section(name: str):
    print(f"\n== {name} ==")
    yield
    print(f"-- {name} done --")


def assert_(cond, message: str) -> None:
    if cond:
        _passes.append(message)
        print(f"  PASS  {message}")
    else:
        _failures.append(message)
        print(f"  FAIL  {message}")


def main() -> int:
    # ------------------------------------------------------------------
    # SQL guard
    # ------------------------------------------------------------------
    with section("sql_guard"):
        from hermes_databricks.tools.sql_guard import validate_readonly

        assert_(validate_readonly("SELECT 1").ok, "SELECT 1 allowed")
        assert_(
            validate_readonly("WITH q AS (SELECT 1) SELECT * FROM q").ok, "WITH ... SELECT allowed"
        )
        assert_(not validate_readonly("INSERT INTO t VALUES (1)").ok, "INSERT rejected")
        assert_(not validate_readonly("DROP TABLE t").ok, "DROP rejected")
        assert_(not validate_readonly("SELECT 1; DROP TABLE x").ok, "stacked statements rejected")
        assert_(not validate_readonly("").ok, "empty rejected")
        assert_(not validate_readonly("-- only a comment").ok, "comment-only rejected")

    # ------------------------------------------------------------------
    # Terminal backend
    # ------------------------------------------------------------------
    with section("terminal_backend"):
        from hermes_databricks.tools import terminal_backend as tb

        with tempfile.TemporaryDirectory() as d:
            r = tb.run([sys.executable, "-c", "print('hi-from-terminal')"], hermes_home=d)
            assert_(r.ok and "hi-from-terminal" in r.stdout, "subprocess runs and captures stdout")

            r = tb.run([sys.executable, "-c", "import os; print(os.getcwd())"], hermes_home=d)
            assert_(
                r.ok and r.stdout.strip().endswith("workspace"), "cwd defaults to <home>/workspace"
            )

            r = tb.run([sys.executable, "-c", "print(1)"], hermes_home=d, cwd="../escape")
            assert_((not r.ok) and "escapes workspace" in r.note, "cwd escape rejected")

            r = tb.run(["rm", "-rf", "/"], hermes_home=d)
            assert_((not r.ok) and "denylist" in r.note, "rm in denylist")

            r = tb.run(["totally-not-a-real-binary-xyz"], hermes_home=d)
            assert_((not r.ok) and "not found on PATH" in r.note, "unknown binary diagnostic")

            r = tb.run(
                [sys.executable, "-c", "import time; time.sleep(5)"], hermes_home=d, timeout=1
            )
            assert_((not r.ok) and "timeout" in r.note.lower(), "timeout enforced")

            r = tb.run([sys.executable, "-c", "print(1)"], backend="disabled", hermes_home=d)
            assert_(not r.ok and "disabled" in r.note, "disabled backend returns diagnostic")

            r = tb.run([sys.executable, "-c", "print(1)"], backend="databricks_job", hermes_home=d)
            assert_(
                not r.ok and "databricks_job" in r.note, "databricks_job backend returns diagnostic"
            )

    # ------------------------------------------------------------------
    # Databricks toolset guards
    # ------------------------------------------------------------------
    with section("databricks_toolset"):
        from hermes_databricks.tools import databricks_toolset as dts

        names = set(dts.tool_names())
        expected = {
            "databricks_serving_endpoint_status",
            "databricks_uc_describe_table",
            "databricks_uc_query_readonly",
            "databricks_sql_execute",
            "databricks_volume_read",
            "databricks_volume_write_agent_note",
            "databricks_volume_write",
            "databricks_volume_list",
            "databricks_volume_delete",
            "databricks_volume_mkdir",
            "databricks_jobs_list",
            "databricks_jobs_run_allowlist",
            "databricks_terminal",
            "databricks_python_exec",
        }
        assert_(
            names == expected,
            f"tool_names() matches expected ({sorted(names) == sorted(expected)})",
        )

        payload = json.loads(dts._h_uc_query_readonly({"sql": "INSERT INTO t VALUES (1)"}))
        assert_("error" in payload and "INSERT" in payload["error"], "INSERT rejected by uc_query")

        payload = json.loads(dts._h_uc_query_readonly({"sql": "SELECT 1"}))
        assert_(
            "error" in payload and "HERMES_DATABRICKS_WAREHOUSE_ID" in payload["error"],
            "warehouse_id required",
        )

        payload = json.loads(
            dts._h_volume_write_agent_note({"relative_path": "../etc/passwd", "content": "x"})
        )
        assert_("error" in payload, "agent_note rejects ..")

        payload = json.loads(
            dts._h_volume_write_agent_note({"relative_path": "/etc/passwd", "content": "x"})
        )
        assert_("error" in payload, "agent_note rejects absolute")

        payload = json.loads(dts._h_jobs_run_allowlist({"job_id": 12345}))
        assert_(
            "error" in payload and "allowlist" in payload["error"].lower(),
            "jobs.run requires allowlist",
        )

        payload = json.loads(dts._h_jobs_run_allowlist({"job_id": "x"}))
        assert_(
            "error" in payload and "integer" in payload["error"], "jobs.run validates job_id type"
        )

        payload = json.loads(dts._h_serving_endpoint_status({}))
        assert_("error" in payload, "serving_endpoint_status requires endpoint_name")

    # ------------------------------------------------------------------
    # UCVolumeHome path guards (no SDK)
    # ------------------------------------------------------------------
    with section("volume_fs"):
        from hermes_databricks.fs.volume_fs import UCVolumeHome

        with tempfile.TemporaryDirectory() as d:
            home = UCVolumeHome(local_root=Path(d), volume_root="/Volumes/main/agents/hermes_home")
            try:
                home._resolve_local("/etc/passwd")
                assert_(False, "absolute path should raise")
            except ValueError:
                assert_(True, "absolute path rejected")
            try:
                home._resolve_local("../escape")
                assert_(False, "dotdot should raise")
            except ValueError:
                assert_(True, "dotdot rejected")
            try:
                home._resolve_volume("/Volumes/other")
                assert_(False, "volume absolute should raise")
            except ValueError:
                assert_(True, "volume absolute rejected")
            home.write_text("notes/hello.txt", "world", push=False)
            assert_(home.read_text("notes/hello.txt") == "world", "write/read round-trip")
            assert_(home._is_durable("config.yaml") is True, "config.yaml is durable")
            assert_(home._is_excluded("state.db") is True, "state.db excluded from upload")

    # ------------------------------------------------------------------
    # Logging redaction
    # ------------------------------------------------------------------
    with section("logging_redaction"):
        from hermes_databricks.observability.logging import RedactingFormatter, redact

        fmt = RedactingFormatter()
        rec = logging.LogRecord(
            "t", logging.INFO, "x.py", 1, "Bearer SUPERSECRETOPENAITOKEN", (), None
        )
        out = fmt.format(rec)
        assert_("SUPERSECRETOPENAITOKEN" not in out, "Bearer token redacted")

        rec = logging.LogRecord(
            "t", logging.INFO, "x.py", 1, "postgres://u:topsecretpw@h/d", (), None
        )
        out = fmt.format(rec)
        assert_("topsecretpw" not in out, "Postgres password redacted")

        red = redact({"list": ["Bearer ABCDEFGHIJABCDEFGH123"], "plain": "ok"})
        assert_("ABCDEFGHIJABCDEFGH123" not in json.dumps(red), "nested list redaction")
        assert_(red["plain"] == "ok", "plain values pass through")

    # ------------------------------------------------------------------
    # Backend registry / browser / mcp diagnostics
    # ------------------------------------------------------------------
    with section("backend_registry"):
        from hermes_databricks.config import Config
        from hermes_databricks.tools.backend_registry import (
            build_toolset_selection,
            describe_backends,
        )

        cfg = Config()
        enabled, disabled = build_toolset_selection(cfg)
        assert_("databricks" in enabled, "databricks toolset enabled by default")
        # Default browser_backend is 'disabled' → browser toolset disabled
        assert_("browser" in disabled, "browser toolset disabled by default")
        snap = describe_backends(cfg)
        assert_(snap["backends"]["terminal"]["available"] is True, "terminal backend available")
        assert_(snap["backends"]["browser"]["available"] is False, "browser backend unavailable")

    # ------------------------------------------------------------------
    # Telegram markdown → HTML rendering (stdlib only)
    # ------------------------------------------------------------------
    with section("telegram_format"):
        from hermes_databricks.telegram_format import (
            md_to_telegram_html,
            strip_markdown,
        )

        assert_(
            md_to_telegram_html("**bold**") == "<b>bold</b>",
            "double-asterisk renders as <b>",
        )
        assert_(
            md_to_telegram_html("snake_case_var") == "snake_case_var",
            "snake_case kept literal (not italicised)",
        )
        assert_(
            md_to_telegram_html("# Title") == "<b>Title</b>",
            "ATX heading renders as bold line",
        )
        assert_(
            md_to_telegram_html("- one\n- two") == "• one\n• two",
            "unordered bullets render as •",
        )
        assert_(
            md_to_telegram_html("see [docs](https://x.test)")
            == 'see <a href="https://x.test">docs</a>',
            "markdown link renders as anchor",
        )
        fenced = md_to_telegram_html("```\n<x>**b**\n```")
        assert_(
            fenced == "<pre>&lt;x&gt;**b**\n</pre>",
            "fenced code is HTML-escaped and not re-processed as markdown",
        )
        assert_(
            strip_markdown("**a** *b* _c_") == "a b c",
            "strip_markdown removes emphasis markers",
        )

        two_col_html = md_to_telegram_html("| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |")
        assert_(
            two_col_html == "• <b>1</b>: 2\n• <b>3</b>: 4",
            "2-col table renders as bullet list with bolded key",
        )

        three_col_html = md_to_telegram_html(
            "| name | type | count |\n|------|------|-------|\n| a | int | 5 |"
        )
        assert_(
            three_col_html.startswith("<pre>")
            and three_col_html.endswith("</pre>")
            and "name | type | count" in three_col_html,
            "3-col table renders as a single <pre> block with padded columns",
        )

        assert_(
            md_to_telegram_html("foo | bar | baz") == "foo | bar | baz",
            "plain pipes in prose are not detected as a table",
        )

    # ------------------------------------------------------------------
    # Telegram allowlist (needs httpx — skipped if not installed locally)
    # ------------------------------------------------------------------
    with section("telegram_polling"):
        try:
            import httpx  # noqa: F401
        except ImportError:
            print("  SKIP  httpx not installed in this venv")
            return _summarise()
        from hermes_databricks import telegram_polling as tp

        c = tp.TelegramClient(token="t", primary_user_handle="@Alice", allowed_usernames=["@Bob"])
        assert_(c._is_allowed("alice"), "primary user (alice) allowed")
        assert_(c._is_allowed("bob"), "explicit allowlist member (bob) allowed")
        assert_(c._is_allowed("BOB"), "allowlist comparison is case-insensitive")
        assert_(not c._is_allowed("carol"), "unknown user rejected")

        empty = tp.TelegramClient(token="t", primary_user_handle=None, allowed_usernames=[])
        assert_(not empty._is_allowed("anyone"), "empty allowlist rejects all (least privilege)")

        async def _probe():
            return await c.health_probe()

        probe = asyncio.run(_probe())
        assert_(probe["status"] == "starting", "health_probe reports starting before first poll")

    return _summarise()


def _summarise() -> int:
    print(f"\n=== {len(_passes)} passed, {len(_failures)} failed ===")
    if _failures:
        for f in _failures:
            print(f"  FAILED: {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
