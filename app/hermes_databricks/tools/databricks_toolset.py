"""Databricks-native Hermes toolset.

Adds the following tools under the ``databricks`` toolset to the
Hermes tool registry. They are intentionally narrow and safety-gated:

* ``databricks_serving_endpoint_status``
* ``databricks_uc_describe_table``
* ``databricks_uc_query_readonly``       (SELECT/WITH only, allowlisted)
* ``databricks_volume_read``             (prefix-restricted)
* ``databricks_volume_write_agent_note`` (artifacts volume only)
* ``databricks_jobs_list``
* ``databricks_jobs_run_allowlist``      (job_id allowlist enforced)
* ``databricks_terminal``                (Phase 7 terminal backend)

Hermes contract (from ``tools/registry.py``):

* Handlers take ``(args: dict, **kw)`` and return a JSON ``str``.
* The schema is the flat OpenAI ``function`` object (``name``,
  ``description``, ``parameters``); Hermes wraps it in
  ``{"type": "function", "function": ...}`` at definition time.

Allowlists are driven by env vars (overridable through ``app.yaml`` or
bundle variables):

* ``HERMES_DATABRICKS_QUERY_ALLOWED_TABLES`` — CSV of
  ``catalog.schema.table`` patterns; ``*`` wildcards allowed.
* ``HERMES_DATABRICKS_JOB_ID_ALLOWLIST`` — CSV of integer job ids.
* ``HERMES_DATABRICKS_VOLUME_READ_PREFIXES`` — CSV of
  ``/Volumes/...`` prefixes (the configured hermes_home and artifacts
  volumes are always included).
* ``HERMES_DATABRICKS_AGENT_NOTES_SUBPATH`` — relative subpath under
  the artifacts volume (default ``agent-notes``).
* ``HERMES_DATABRICKS_WAREHOUSE_ID`` — SQL warehouse for
  ``databricks_uc_query_readonly``.

If a tool is invoked without the required Databricks SDK / endpoint
configuration, it returns ``{"error": ...}`` instead of crashing —
Hermes will surface that in conversation history.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from hermes_databricks.config import Config, load as load_cfg
from hermes_databricks.tools import terminal_backend as _term
from hermes_databricks.tools.sql_guard import validate_readonly

log = logging.getLogger("hermes_databricks.tools.databricks_toolset")


# ---------------------------------------------------------------------
# Workspace client (lazy / shared)
# ---------------------------------------------------------------------


_WORKSPACE_CLIENT = None


def _client():
    """Return a cached ``WorkspaceClient`` instance. Raises if the SDK is missing."""
    global _WORKSPACE_CLIENT
    if _WORKSPACE_CLIENT is not None:
        return _WORKSPACE_CLIENT
    from databricks.sdk import WorkspaceClient  # type: ignore

    _WORKSPACE_CLIENT = WorkspaceClient()
    return _WORKSPACE_CLIENT


def _err(message: str, **extra) -> str:
    payload = {"error": message}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, default=str)


def _ok(**payload) -> str:
    payload["ok"] = True
    return json.dumps(payload, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------
# Allowlist helpers
# ---------------------------------------------------------------------


def _allowed_tables() -> list[str]:
    raw = os.environ.get("HERMES_DATABRICKS_QUERY_ALLOWED_TABLES", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _allowed_job_ids() -> list[int]:
    raw = os.environ.get("HERMES_DATABRICKS_JOB_ID_ALLOWLIST", "")
    out: list[int] = []
    for s in raw.split(","):
        s = s.strip()
        if not s:
            continue
        try:
            out.append(int(s))
        except ValueError:
            continue
    return out


def _allowed_volume_prefixes(cfg: Config) -> list[str]:
    raw = os.environ.get("HERMES_DATABRICKS_VOLUME_READ_PREFIXES", "")
    out = [s.strip() for s in raw.split(",") if s.strip()]
    out.extend([cfg.hermes_home_volume_path, cfg.artifacts_volume_path])
    seen: list[str] = []
    for p in out:
        if p and p not in seen:
            seen.append(p)
    return seen


def _table_match(full_name: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    full_name = full_name.lower()
    for pat in patterns:
        pat = pat.lower()
        if pat == full_name:
            return True
        if "*" in pat:
            rgx = "^" + re.escape(pat).replace(r"\*", ".*") + "$"
            if re.match(rgx, full_name):
                return True
    return False


def _path_match(path: str, prefixes: list[str]) -> bool:
    path = path.strip()
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


# ---------------------------------------------------------------------
# Tool implementations — handler(args, **kw) -> str
# ---------------------------------------------------------------------


def _h_serving_endpoint_status(args: dict, **_kw) -> str:
    endpoint_name = (args or {}).get("endpoint_name", "")
    if not endpoint_name:
        return _err("endpoint_name is required")
    try:
        w = _client()
        ep = w.serving_endpoints.get(endpoint_name)
        state = getattr(ep, "state", None)
        config = getattr(ep, "config", None)
        return _ok(
            name=getattr(ep, "name", endpoint_name),
            state=state.as_dict() if state and hasattr(state, "as_dict") else None,
            config=config.as_dict() if config and hasattr(config, "as_dict") else None,
            last_updated=str(getattr(ep, "last_updated_timestamp", None) or ""),
        )
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", endpoint_name=endpoint_name)


def _h_uc_describe_table(args: dict, **_kw) -> str:
    full_table_name = (args or {}).get("full_table_name", "")
    if not full_table_name:
        return _err("full_table_name is required (catalog.schema.table)")
    if full_table_name.count(".") != 2:
        return _err("full_table_name must be 'catalog.schema.table'")
    patterns = _allowed_tables()
    if patterns and not _table_match(full_table_name, patterns):
        return _err(
            "table not in HERMES_DATABRICKS_QUERY_ALLOWED_TABLES allowlist",
            allowlist=patterns,
            full_table_name=full_table_name,
        )
    try:
        w = _client()
        t = w.tables.get(full_table_name)
        cols = []
        for c in getattr(t, "columns", None) or []:
            cols.append(
                {
                    "name": getattr(c, "name", None),
                    "type_text": getattr(c, "type_text", None),
                    "comment": getattr(c, "comment", None),
                    "nullable": getattr(c, "nullable", None),
                }
            )
        return _ok(
            name=full_table_name,
            comment=getattr(t, "comment", None),
            table_type=str(getattr(t, "table_type", None) or ""),
            columns=cols,
            owner=getattr(t, "owner", None),
        )
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", full_table_name=full_table_name)


def _h_uc_query_readonly(args: dict, **_kw) -> str:
    args = args or {}
    sql = args.get("sql", "")
    row_limit = args.get("row_limit", 200)
    guard = validate_readonly(sql)
    if not guard.ok:
        return _err(f"SQL rejected by read-only guard: {guard.reason}")
    warehouse_id = os.environ.get("HERMES_DATABRICKS_WAREHOUSE_ID", "").strip()
    if not warehouse_id:
        return _err(
            "no SQL warehouse configured. Set HERMES_DATABRICKS_WAREHOUSE_ID "
            "to a warehouse the App SP CAN_USE."
        )
    try:
        row_limit = max(1, min(int(row_limit), 5000))
    except (TypeError, ValueError):
        row_limit = 200
    log.info(
        "uc_query_readonly",
        extra={
            "extras": {"sql_head": guard.normalised.split("\n", 1)[0][:120], "row_limit": row_limit}
        },
    )
    try:
        from databricks.sdk.service import sql as sql_mod  # type: ignore

        w = _client()
        resp = w.statement_execution.execute_statement(
            statement=guard.normalised,
            warehouse_id=warehouse_id,
            wait_timeout="30s",
            on_wait_timeout=sql_mod.ExecuteStatementRequestOnWaitTimeout.CANCEL,
            disposition=sql_mod.Disposition.INLINE,
            format=sql_mod.Format.JSON_ARRAY,
            row_limit=row_limit,
        )
        state = getattr(getattr(resp, "status", None), "state", None)
        result = getattr(resp, "result", None)
        manifest = getattr(resp, "manifest", None)
        cols: list[dict[str, Any]] = []
        if manifest is not None and getattr(manifest, "schema", None) is not None:
            for c in manifest.schema.columns or []:
                cols.append(
                    {"name": getattr(c, "name", None), "type_text": getattr(c, "type_text", None)}
                )
        rows = getattr(result, "data_array", None) or []
        return _ok(
            sql=guard.normalised,
            warehouse_id=warehouse_id,
            row_limit=row_limit,
            state=str(state or ""),
            columns=cols,
            rows=rows,
            row_count=len(rows),
        )
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


def _h_volume_read(args: dict, **_kw) -> str:
    args = args or {}
    path = args.get("path", "")
    max_bytes = int(args.get("max_bytes", 65_536) or 65_536)
    if not path:
        return _err("path is required")
    cfg = load_cfg()
    prefixes = _allowed_volume_prefixes(cfg)
    if not _path_match(path, prefixes):
        return _err(
            "path not in allowed UC Volume prefixes",
            allowed_prefixes=prefixes,
            path=path,
        )
    try:
        w = _client()
        resp = w.files.download(path)
        contents_obj = getattr(resp, "contents", None)
        if contents_obj is None:
            return _err("download response had no contents", path=path)
        contents = contents_obj.read() if hasattr(contents_obj, "read") else contents_obj
        if not isinstance(contents, (bytes, bytearray)):
            contents = bytes(contents)
        truncated = False
        if max_bytes and len(contents) > max_bytes:
            contents = contents[:max_bytes]
            truncated = True
        try:
            text = contents.decode("utf-8")
            return _ok(
                path=path, encoding="utf-8", text=text, truncated=truncated, size=len(contents)
            )
        except UnicodeDecodeError:
            import base64

            return _ok(
                path=path,
                encoding="base64",
                text=base64.b64encode(contents).decode("ascii"),
                truncated=truncated,
                size=len(contents),
            )
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", path=path)


def _h_volume_write_agent_note(args: dict, **_kw) -> str:
    args = args or {}
    relative_path = args.get("relative_path", "")
    content = args.get("content", "")
    if not relative_path or relative_path.startswith("/") or ".." in relative_path.split("/"):
        return _err("relative_path must be a non-empty path without '..' or leading '/'")
    cfg = load_cfg()
    subpath = os.environ.get("HERMES_DATABRICKS_AGENT_NOTES_SUBPATH", "agent-notes").strip("/")
    target = f"{cfg.artifacts_volume_path.rstrip('/')}/{subpath}/{relative_path.lstrip('/')}"
    try:
        w = _client()
        if isinstance(content, str):
            data = content.encode("utf-8")
        elif isinstance(content, (bytes, bytearray)):
            data = bytes(content)
        else:
            data = json.dumps(content, default=str).encode("utf-8")
        w.files.upload(target, contents=io.BytesIO(data), overwrite=True)
        return _ok(path=target, bytes_written=len(data))
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", path=target)


def _h_jobs_list(_args: dict, **_kw) -> str:
    try:
        w = _client()
        out: list[dict[str, Any]] = []
        for j in w.jobs.list():
            settings = getattr(j, "settings", None)
            out.append(
                {
                    "job_id": getattr(j, "job_id", None),
                    "name": getattr(settings, "name", None),
                    "created_time": str(getattr(j, "created_time", None) or ""),
                    "creator_user_name": getattr(j, "creator_user_name", None),
                }
            )
        return _ok(count=len(out), jobs=out)
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


def _h_jobs_run_allowlist(args: dict, **_kw) -> str:
    args = args or {}
    try:
        job_id_int = int(args.get("job_id"))
    except (TypeError, ValueError):
        return _err("job_id must be an integer")
    parameters = args.get("parameters") or {}
    allowed = _allowed_job_ids()
    if not allowed or job_id_int not in allowed:
        return _err(
            "job_id not in HERMES_DATABRICKS_JOB_ID_ALLOWLIST",
            allowlist=allowed,
            job_id=job_id_int,
        )
    try:
        w = _client()
        run = w.jobs.run_now(job_id=job_id_int, notebook_params=parameters)
        return _ok(
            job_id=job_id_int,
            run_id=getattr(run, "run_id", None),
            number_in_job=getattr(run, "number_in_job", None),
        )
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", job_id=job_id_int)


def _h_terminal(args: dict, **_kw) -> str:
    args = args or {}
    cmd = args.get("cmd") or []
    cwd = args.get("cwd")
    timeout = args.get("timeout", 60.0)
    cfg = load_cfg()
    if not isinstance(cmd, list):
        return _err("cmd must be a list of strings; shell strings are not allowed")
    try:
        result = _term.run(
            [str(a) for a in cmd],
            backend=cfg.terminal_backend,
            hermes_home=cfg.hermes_home,
            cwd=cwd,
            timeout=timeout,
        )
        return json.dumps(result.to_dict(), ensure_ascii=False, default=str)
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------
# Tool registry registration
# ---------------------------------------------------------------------


@dataclass
class _ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Any
    emoji: str = ""


_TOOL_SPECS: list[_ToolSpec] = [
    _ToolSpec(
        name="databricks_serving_endpoint_status",
        description="Return the configuration and state of a Databricks Model Serving endpoint.",
        parameters={
            "type": "object",
            "properties": {
                "endpoint_name": {"type": "string", "description": "Name of the serving endpoint."}
            },
            "required": ["endpoint_name"],
        },
        handler=_h_serving_endpoint_status,
        emoji="🛰️",
    ),
    _ToolSpec(
        name="databricks_uc_describe_table",
        description="Describe a Unity Catalog table (columns, types, comments). Restricted to the configured allowlist when set.",
        parameters={
            "type": "object",
            "properties": {
                "full_table_name": {
                    "type": "string",
                    "description": "Fully-qualified name: catalog.schema.table",
                }
            },
            "required": ["full_table_name"],
        },
        handler=_h_uc_describe_table,
        emoji="📊",
    ),
    _ToolSpec(
        name="databricks_uc_query_readonly",
        description=(
            "Run a SELECT or WITH query against the configured SQL warehouse. "
            "INSERT/UPDATE/DELETE/MERGE/DDL are rejected by the SQL guard. "
            "Returns at most 'row_limit' rows (default 200, hard cap 5000)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "The SELECT/WITH SQL statement to execute.",
                },
                "row_limit": {
                    "type": "integer",
                    "description": "Maximum rows to return. Default 200, max 5000.",
                },
            },
            "required": ["sql"],
        },
        handler=_h_uc_query_readonly,
        emoji="🔍",
    ),
    _ToolSpec(
        name="databricks_volume_read",
        description="Read a file from a UC Volume (text or base64 if non-UTF8). Restricted to allowed volume prefixes.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute /Volumes/... path."},
                "max_bytes": {
                    "type": "integer",
                    "description": "Truncate the response to this many bytes.",
                },
            },
            "required": ["path"],
        },
        handler=_h_volume_read,
        emoji="📥",
    ),
    _ToolSpec(
        name="databricks_volume_write_agent_note",
        description="Write a text note under the agent-notes subpath of the artifacts volume.",
        parameters={
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": "Relative path under the artifacts volume's agent-notes/ subpath.",
                },
                "content": {
                    "type": "string",
                    "description": "Note content. JSON-serialisable values are also accepted.",
                },
            },
            "required": ["relative_path", "content"],
        },
        handler=_h_volume_write_agent_note,
        emoji="📝",
    ),
    _ToolSpec(
        name="databricks_jobs_list",
        description="List the Databricks Jobs visible to the App service principal.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=_h_jobs_list,
        emoji="🧰",
    ),
    _ToolSpec(
        name="databricks_jobs_run_allowlist",
        description="Trigger a Databricks Job. The job_id must be in HERMES_DATABRICKS_JOB_ID_ALLOWLIST.",
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "integer", "description": "Job id to run."},
                "parameters": {
                    "type": "object",
                    "description": "Optional notebook_params/job parameters.",
                },
            },
            "required": ["job_id"],
        },
        handler=_h_jobs_run_allowlist,
        emoji="▶️",
    ),
    _ToolSpec(
        name="databricks_terminal",
        description=(
            "Run a shell command via the configured terminal backend. "
            "Default backend 'in_app_subprocess' restricts cwd to "
            "<HERMES_HOME>/workspace and enforces a 60s default timeout."
        ),
        parameters={
            "type": "object",
            "properties": {
                "cmd": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command + arguments as a list (no shell string).",
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional sub-directory under the workspace.",
                },
                "timeout": {"type": "number", "description": "Seconds; default 60, max 600."},
            },
            "required": ["cmd"],
        },
        handler=_h_terminal,
        emoji="🖥️",
    ),
]


def _toolset_check_fn() -> bool:
    """Toolset is available iff Databricks SDK is importable.

    The Databricks App always has the SDK installed and a service-principal
    identity, so this is effectively always True in production — but we
    probe it explicitly so local dev surfaces a sensible diagnostic.
    """
    try:
        from databricks.sdk import WorkspaceClient  # noqa: F401

        return True
    except Exception:
        return False


def register_databricks_toolset(cfg: Config | None = None, *, home_fs: object | None = None) -> int:
    """Register every Databricks tool with Hermes' tool registry.

    Idempotent (uses ``override=True``). Returns the number of tools
    registered. Silently no-ops if Hermes is not importable (degraded
    App boot).
    """
    try:
        from tools.registry import registry as _registry  # type: ignore
    except Exception:
        log.debug(
            "Hermes tools.registry unavailable; databricks toolset not registered", exc_info=True
        )
        return 0

    count = 0
    for spec in _TOOL_SPECS:
        try:
            schema = {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            }
            _registry.register(
                name=spec.name,
                toolset="databricks",
                schema=schema,
                handler=spec.handler,
                check_fn=_toolset_check_fn,
                description=spec.description,
                emoji=spec.emoji,
                override=True,
            )
            count += 1
        except Exception:
            log.exception("Failed to register tool %s", spec.name)

    log.info("Registered %d Databricks-native tools in toolset 'databricks'", count)
    return count


def tool_names() -> list[str]:
    return [s.name for s in _TOOL_SPECS]
