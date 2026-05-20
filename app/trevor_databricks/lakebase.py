"""Lakebase connection helper (single connection + credential rotation).

Implements the same pattern Living-AI uses in production:

* Databricks SDK ``database.generate_database_credential`` mints a
  1-hour Postgres token; we cache it for 50 minutes.
* Databricks SDK ``database.get_database_instance`` resolves the
  read/write DNS name.
* ``psycopg.connect`` opens a single connection in ``autocommit`` mode
  with ``sslmode="require"`` and ``user=DATABRICKS_CLIENT_ID`` (the
  App's service principal).
* All cursors are acquired through ``with self.cursor() as cur`` which
  is guarded by a process-wide ``RLock`` (the FastAPI process is single
  uvicorn worker by default).
* On ``OperationalError``, the connection is dropped and re-opened on
  the next call.

This is intentionally not a pool. Free Edition workloads are small,
and a single autocommit connection is dramatically simpler than
managing a real pool. If concurrency becomes a concern we can swap to
``psycopg_pool.ConnectionPool`` without changing the public API.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

from trevor_databricks.config import Config

log = logging.getLogger("trevor_databricks.lakebase")


CREDENTIAL_TTL_SECONDS = 50 * 60  # rotate before the 1-hour Lakebase token expiry


class Lakebase:
    """Single-connection Lakebase helper with credential rotation."""

    def __init__(
        self,
        instance_name: str,
        *,
        database: str = "databricks_postgres",
        sp_client_id: str | None = None,
        workspace_factory=None,
    ) -> None:
        self.instance_name = instance_name
        self.database = database
        self.sp_client_id = sp_client_id or os.environ.get("DATABRICKS_CLIENT_ID", "")
        self._lock = threading.RLock()
        self._conn: Any = None
        self._cred_token: str | None = None
        self._cred_minted_at: float = 0.0
        self._instance_dns: str | None = None
        self._workspace_factory = workspace_factory
        self._workspace: Any = None

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: Config, *, workspace_factory=None) -> Lakebase:
        return cls(
            instance_name=cfg.lakebase_instance,
            database=cfg.lakebase_database,
            workspace_factory=workspace_factory,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def cursor(self) -> Iterator[Any]:
        """Acquire a psycopg cursor under the connection lock.

        Auto-reconnects on ``OperationalError`` and re-runs the caller
        once (the caller is expected to be a single statement or an
        idempotent block — Lakebase + autocommit makes this safe).
        """
        try:
            import psycopg  # type: ignore
        except Exception as exc:  # pragma: no cover - dependency error path
            raise RuntimeError("psycopg is required for Lakebase") from exc

        with self._lock:
            conn = self._ensure_connection()
            try:
                with conn.cursor() as cur:
                    yield cur
            except psycopg.OperationalError:
                log.warning("Lakebase OperationalError; reconnecting", exc_info=True)
                self._close_connection()
                conn = self._ensure_connection()
                with conn.cursor() as cur:
                    yield cur

    def close(self) -> None:
        with self._lock:
            self._close_connection()

    def execute(self, sql: str, params: tuple | None = None) -> None:
        with self.cursor() as cur:
            cur.execute(sql, params)

    def fetchone(self, sql: str, params: tuple | None = None) -> tuple | None:
        with self.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def fetchall(self, sql: str, params: tuple | None = None) -> list:
        with self.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    async def health_probe(self) -> dict:
        try:
            with self.cursor() as cur:
                cur.execute("SELECT 1")
                _ = cur.fetchone()
            return {"status": "ok", "instance": self.instance_name}
        except Exception as exc:
            return {
                "status": "error",
                "reason": f"{type(exc).__name__}: {exc}",
                "instance": self.instance_name,
            }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _workspace_client(self):
        if self._workspace is not None:
            return self._workspace
        if self._workspace_factory is not None:
            self._workspace = self._workspace_factory()
            return self._workspace
        from databricks.sdk import WorkspaceClient  # type: ignore

        self._workspace = WorkspaceClient()
        return self._workspace

    def _resolve_instance_dns(self) -> str:
        if self._instance_dns is not None:
            return self._instance_dns
        w = self._workspace_client()
        try:
            instance = w.database.get_database_instance(name=self.instance_name)
        except Exception as exc:
            # The SDK raises ``NotFound`` for both "instance doesn't exist"
            # and "caller lacks DATABASE_USAGE on the instance". The latter
            # is the common Databricks Apps failure mode: the App's
            # service principal needs the ``lakebase-instance`` resource
            # binding in ``resources/app.yml`` to receive DATABASE_USAGE.
            raise RuntimeError(
                f"Lakebase instance '{self.instance_name}' not visible to the App "
                f"service principal. Either the instance was renamed/deleted, or "
                f"the App is missing the `lakebase-instance` resource binding "
                f"(see resources/app.yml). Underlying error: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        dns = getattr(instance, "read_write_dns", None) or getattr(instance, "host", None)
        if not dns:
            raise RuntimeError(f"Lakebase instance '{self.instance_name}' has no DNS record")
        self._instance_dns = dns
        return dns

    def _mint_credential(self) -> str:
        if self._cred_token and (time.time() - self._cred_minted_at) < CREDENTIAL_TTL_SECONDS:
            return self._cred_token
        w = self._workspace_client()
        cred = w.database.generate_database_credential(
            request_id=str(uuid.uuid4()),
            instance_names=[self.instance_name],
        )
        token = getattr(cred, "token", None)
        if not token:
            raise RuntimeError("Lakebase credential mint returned no token")
        self._cred_token = token
        self._cred_minted_at = time.time()
        return token

    def _ensure_connection(self):
        if self._conn is not None:
            return self._conn

        import psycopg  # type: ignore

        dns = self._resolve_instance_dns()
        token = self._mint_credential()
        sp = self.sp_client_id or os.environ.get("DATABRICKS_CLIENT_ID", "")
        if not sp:
            raise RuntimeError(
                "DATABRICKS_CLIENT_ID is not set. Lakebase requires the App "
                "service principal client id as the Postgres user."
            )

        self._conn = psycopg.connect(
            host=dns,
            port=5432,
            dbname=self.database,
            user=sp,
            password=token,
            sslmode="require",
            autocommit=True,
            connect_timeout=10,
        )
        log.info(
            "Lakebase connection opened",
            extra={
                "extras": {
                    "instance": self.instance_name,
                    "host": dns,
                    "user": sp[:8] + "…",
                    "database": self.database,
                }
            },
        )
        return self._conn

    def _close_connection(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except Exception:
            log.exception("Lakebase connection close failed")
        finally:
            self._conn = None
