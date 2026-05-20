"""Unit tests for the WRITES_ENABLED / YOLO master switches and allowlists."""

from __future__ import annotations

import json

import pytest

from trevor_databricks import config as cfg_mod
from trevor_databricks.tools import databricks_toolset as ts


@pytest.fixture(autouse=True)
def _clear_config_cache() -> None:
    """Each test sees a fresh load() — env vars below mutate config."""
    cfg_mod.load.cache_clear()
    yield
    cfg_mod.load.cache_clear()


@pytest.fixture
def writes_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREVOR_DATABRICKS_WRITES_ENABLED", "false")
    monkeypatch.setenv("TREVOR_DATABRICKS_YOLO", "false")


@pytest.fixture
def writes_on_no_yolo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREVOR_DATABRICKS_WRITES_ENABLED", "true")
    monkeypatch.setenv("TREVOR_DATABRICKS_YOLO", "false")
    monkeypatch.setenv("TREVOR_DATABRICKS_WAREHOUSE_ID", "WH-TEST")
    monkeypatch.setenv(
        "TREVOR_DATABRICKS_WRITE_ALLOWED_SCHEMAS",
        "workspace.trevor_agent.*,workspace.scratch.*",
    )


@pytest.fixture
def writes_on_with_yolo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREVOR_DATABRICKS_WRITES_ENABLED", "true")
    monkeypatch.setenv("TREVOR_DATABRICKS_YOLO", "true")
    monkeypatch.setenv("TREVOR_DATABRICKS_WAREHOUSE_ID", "WH-TEST")
    monkeypatch.setenv(
        "TREVOR_DATABRICKS_WRITE_ALLOWED_SCHEMAS",
        "workspace.trevor_agent.*",
    )


def _fake_execute_response():
    """A minimal stand-in for the SDK's StatementResponse."""
    status = type("Status", (), {"state": "SUCCEEDED"})()
    schema = type("Schema", (), {"columns": ()})()
    manifest = type("Manifest", (), {"schema": schema})()
    result = type("Result", (), {"data_array": ()})()
    return type("Resp", (), {"status": status, "manifest": manifest, "result": result})()


class TestWritesEnabledFlag:
    def test_default_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TREVOR_DATABRICKS_WRITES_ENABLED", raising=False)
        assert cfg_mod.load().writes_enabled is False

    def test_writes_off_blocks_sql_execute(self, writes_off: None) -> None:
        result = json.loads(ts._h_sql_execute({"sql": "INSERT INTO main.s.t VALUES (1)"}))
        assert "error" in result
        assert "disabled" in result["error"].lower()

    def test_writes_off_blocks_volume_write(self, writes_off: None) -> None:
        result = json.loads(
            ts._h_volume_write(
                {"path": "/Volumes/workspace/trevor_agent/trevor_home/x.txt", "content": "y"}
            )
        )
        assert "error" in result
        assert "disabled" in result["error"].lower()

    def test_writes_off_blocks_volume_mkdir(self, writes_off: None) -> None:
        result = json.loads(
            ts._h_volume_mkdir({"path": "/Volumes/workspace/trevor_agent/trevor_home/new"})
        )
        assert "error" in result

    def test_writes_off_still_allows_readonly_path(self, writes_off: None) -> None:
        """`databricks_sql_execute` for a SELECT bypasses the write check."""
        # The actual call will fail with "no warehouse configured" because
        # we deliberately don't set TREVOR_DATABRICKS_WAREHOUSE_ID. What
        # matters is that it doesn't reject with the writes-disabled error.
        result = json.loads(ts._h_sql_execute({"sql": "SELECT 1"}))
        assert "error" in result
        assert "writes" not in result["error"].lower()


class TestYoloFlag:
    def test_writes_on_allows_non_destructive_dml(
        self, writes_on_no_yolo: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: dict = {}

        def _fake_execute_statement(**kwargs):
            sent.update(kwargs)
            return _fake_execute_response()

        class _FakeWC:
            statement_execution = type(
                "SE", (), {"execute_statement": staticmethod(_fake_execute_statement)}
            )

        monkeypatch.setattr(ts, "_client", lambda: _FakeWC)
        result = json.loads(
            ts._h_sql_execute(
                {
                    "sql": "INSERT INTO workspace.trevor_agent.t VALUES (1)",
                }
            )
        )
        assert result["ok"] is True, result
        assert sent["warehouse_id"] == "WH-TEST"

    def test_writes_on_blocks_destructive_without_yolo(self, writes_on_no_yolo: None) -> None:
        result = json.loads(ts._h_sql_execute({"sql": "DROP TABLE workspace.trevor_agent.t"}))
        assert "error" in result
        assert "yolo" in result["error"].lower()
        assert "DROP" in result.get("destructive", [])

    def test_yolo_allows_destructive(
        self, writes_on_with_yolo: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakeWC:
            statement_execution = type(
                "SE",
                (),
                {"execute_statement": staticmethod(lambda **_kw: _fake_execute_response())},
            )

        monkeypatch.setattr(ts, "_client", lambda: _FakeWC)
        result = json.loads(ts._h_sql_execute({"sql": "DROP TABLE workspace.trevor_agent.t"}))
        assert result["ok"] is True, result


class TestAllowedSchemaList:
    def test_target_outside_allowlist_rejected(self, writes_on_no_yolo: None) -> None:
        result = json.loads(ts._h_sql_execute({"sql": "INSERT INTO other.cat.tbl VALUES (1)"}))
        assert "error" in result
        assert "WRITE_ALLOWED_SCHEMAS" in result["error"]
        assert "other.cat.tbl" in result["denied"]


class TestVolumeAllowlist:
    @pytest.fixture
    def vol_writes_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TREVOR_DATABRICKS_WRITES_ENABLED", "true")
        monkeypatch.setenv("TREVOR_DATABRICKS_YOLO", "false")
        monkeypatch.setenv(
            "TREVOR_DATABRICKS_WRITE_ALLOWED_VOLUMES",
            "/Volumes/workspace/trevor_agent/trevor_home",
        )

    def test_outside_allowlist_rejected(
        self, vol_writes_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = json.loads(
            ts._h_volume_write({"path": "/Volumes/other/cat/vol/x.txt", "content": "x"})
        )
        assert "error" in result
        assert "WRITE_ALLOWED_VOLUMES" in result["error"]

    def test_inside_allowlist_calls_sdk(
        self, vol_writes_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict = {}

        class _Files:
            @staticmethod
            def upload(path: str, contents, overwrite: bool = False) -> None:
                seen["path"] = path
                seen["overwrite"] = overwrite

            @staticmethod
            def get_metadata(path):
                raise Exception("not found")

        class _WC:
            files = _Files

        monkeypatch.setattr(ts, "_client", lambda: _WC)
        result = json.loads(
            ts._h_volume_write(
                {
                    "path": "/Volumes/workspace/trevor_agent/trevor_home/note.txt",
                    "content": "hi",
                }
            )
        )
        assert result["ok"] is True, result
        assert seen["path"].endswith("/note.txt")

    def test_overwrite_existing_requires_yolo(
        self, vol_writes_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Files:
            @staticmethod
            def upload(*a, **kw):
                raise AssertionError("upload should not be called")

            @staticmethod
            def get_metadata(path):
                return type("M", (), {"path": path})()

        class _WC:
            files = _Files

        monkeypatch.setattr(ts, "_client", lambda: _WC)
        result = json.loads(
            ts._h_volume_write(
                {
                    "path": "/Volumes/workspace/trevor_agent/trevor_home/exists.txt",
                    "content": "hi",
                    "overwrite": True,
                }
            )
        )
        assert "error" in result
        assert "yolo" in result["error"].lower()

    def test_volume_delete_requires_yolo(
        self, vol_writes_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = json.loads(
            ts._h_volume_delete({"path": "/Volumes/workspace/trevor_agent/trevor_home/x.txt"})
        )
        assert "error" in result
        assert "yolo" in result["error"].lower()


class TestSchemaMatching:
    def test_wildcard_suffix(self) -> None:
        assert ts._schema_match("main.s.t", ["main.s.*"])
        assert ts._schema_match("main.s.user_table", ["main.s.user_*"])
        assert not ts._schema_match("main.s.t", ["other.*"])

    def test_exact_match(self) -> None:
        assert ts._schema_match("workspace.trevor_agent.notes", ["workspace.trevor_agent.notes"])
        assert not ts._schema_match(
            "workspace.trevor_agent.other", ["workspace.trevor_agent.notes"]
        )

    def test_catalog_wildcard(self) -> None:
        assert ts._schema_match("workspace.any.thing", ["workspace.*"])

    def test_empty_allowlist_rejects(self) -> None:
        assert not ts._schema_match("anything", [])
