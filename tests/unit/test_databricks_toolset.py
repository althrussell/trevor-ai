"""Unit tests for the Databricks-native toolset registration and guards.

These tests deliberately avoid touching the real Databricks SDK — the
toolset's only hard dependency is ``hermes_databricks.tools.sql_guard``
plus optional helpers. SDK-backed tools are exercised via dependency
injection on the module-level ``_WORKSPACE_CLIENT`` singleton.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from hermes_databricks.tools import databricks_toolset as dts


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    # Reset shared lazy client and env var state between tests.
    monkeypatch.setattr(dts, "_WORKSPACE_CLIENT", None, raising=False)
    for k in [
        "HERMES_DATABRICKS_QUERY_ALLOWED_TABLES",
        "HERMES_DATABRICKS_JOB_ID_ALLOWLIST",
        "HERMES_DATABRICKS_VOLUME_READ_PREFIXES",
        "HERMES_DATABRICKS_AGENT_NOTES_SUBPATH",
        "HERMES_DATABRICKS_WAREHOUSE_ID",
    ]:
        monkeypatch.delenv(k, raising=False)
    yield


def _set_client(monkeypatch, **methods):
    """Install a fake ``WorkspaceClient`` exposing the requested attrs."""
    monkeypatch.setattr(dts, "_WORKSPACE_CLIENT", SimpleNamespace(**methods))


def test_tool_names_are_stable():
    expected = {
        "databricks_serving_endpoint_status",
        "databricks_uc_describe_table",
        "databricks_uc_query_readonly",
        "databricks_volume_read",
        "databricks_volume_write_agent_note",
        "databricks_jobs_list",
        "databricks_jobs_run_allowlist",
        "databricks_terminal",
    }
    assert set(dts.tool_names()) == expected


def test_uc_query_rejects_mutating_sql():
    raw = dts._h_uc_query_readonly({"sql": "INSERT INTO t VALUES (1)"})
    payload = json.loads(raw)
    assert "error" in payload
    assert "INSERT" in payload["error"]


def test_uc_query_requires_warehouse():
    raw = dts._h_uc_query_readonly({"sql": "SELECT 1"})
    payload = json.loads(raw)
    assert "error" in payload
    assert "HERMES_DATABRICKS_WAREHOUSE_ID" in payload["error"]


def test_uc_describe_requires_three_parts():
    raw = dts._h_uc_describe_table({"full_table_name": "main.t"})
    payload = json.loads(raw)
    assert "error" in payload


def test_uc_describe_enforces_allowlist(monkeypatch):
    monkeypatch.setenv("HERMES_DATABRICKS_QUERY_ALLOWED_TABLES", "main.allowed.*")
    raw = dts._h_uc_describe_table({"full_table_name": "main.banned.x"})
    payload = json.loads(raw)
    assert "error" in payload
    assert "allowlist" in payload["error"].lower()


def test_uc_describe_allowlist_wildcard_allows(monkeypatch):
    monkeypatch.setenv("HERMES_DATABRICKS_QUERY_ALLOWED_TABLES", "main.allowed.*")
    table = SimpleNamespace(comment="x", table_type="MANAGED", owner="me", columns=[])
    fake_tables = SimpleNamespace(get=lambda _name: table)
    _set_client(monkeypatch, tables=fake_tables)
    raw = dts._h_uc_describe_table({"full_table_name": "main.allowed.x"})
    payload = json.loads(raw)
    assert payload.get("ok") is True


def test_volume_write_rejects_path_escape():
    raw = dts._h_volume_write_agent_note({"relative_path": "../etc/passwd", "content": "x"})
    payload = json.loads(raw)
    assert "error" in payload


def test_volume_write_rejects_absolute_path():
    raw = dts._h_volume_write_agent_note({"relative_path": "/etc/passwd", "content": "x"})
    payload = json.loads(raw)
    assert "error" in payload


def test_volume_read_enforces_prefix(monkeypatch):
    monkeypatch.setenv("HERMES_DATABRICKS_VOLUME_READ_PREFIXES", "/Volumes/x/y/z")
    raw = dts._h_volume_read({"path": "/Volumes/other/place/file.txt"})
    payload = json.loads(raw)
    assert "error" in payload
    assert "prefixes" in payload["error"].lower()


def test_jobs_run_requires_allowlist():
    raw = dts._h_jobs_run_allowlist({"job_id": 12345})
    payload = json.loads(raw)
    assert "error" in payload
    assert "allowlist" in payload["error"].lower()


def test_jobs_run_rejects_non_integer():
    raw = dts._h_jobs_run_allowlist({"job_id": "not-an-int"})
    payload = json.loads(raw)
    assert "error" in payload
    assert "integer" in payload["error"]


def test_jobs_run_calls_sdk_when_allowed(monkeypatch):
    monkeypatch.setenv("HERMES_DATABRICKS_JOB_ID_ALLOWLIST", "42, 43")
    called = {}

    def _run_now(job_id, notebook_params):
        called["job_id"] = job_id
        called["params"] = notebook_params
        return SimpleNamespace(run_id=999, number_in_job=1)

    _set_client(monkeypatch, jobs=SimpleNamespace(run_now=_run_now))
    raw = dts._h_jobs_run_allowlist({"job_id": 42, "parameters": {"foo": "bar"}})
    payload = json.loads(raw)
    assert payload.get("ok") is True
    assert payload["run_id"] == 999
    assert called == {"job_id": 42, "params": {"foo": "bar"}}


def test_serving_endpoint_status_requires_name():
    raw = dts._h_serving_endpoint_status({})
    payload = json.loads(raw)
    assert "error" in payload


def test_serving_endpoint_status_returns_state(monkeypatch):
    state = SimpleNamespace(as_dict=lambda: {"ready": "READY"})
    cfg = SimpleNamespace(as_dict=lambda: {"name": "ep"})
    ep = SimpleNamespace(
        name="ep", state=state, config=cfg, last_updated_timestamp=12345
    )
    _set_client(monkeypatch, serving_endpoints=SimpleNamespace(get=lambda _: ep))
    raw = dts._h_serving_endpoint_status({"endpoint_name": "ep"})
    payload = json.loads(raw)
    assert payload.get("ok") is True
    assert payload["name"] == "ep"
    assert payload["state"] == {"ready": "READY"}
