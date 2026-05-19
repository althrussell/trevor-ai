# Databricks notebook source
# MAGIC %md
# MAGIC # Hermes-on-Databricks — Lakebase setup
# MAGIC
# MAGIC Run by the bundle job `setup_lakebase`. Creates the
# MAGIC `hermes_session` schema in the bundle's Lakebase instance,
# MAGIC issues the required DDL, and grants the Databricks App service
# MAGIC principal CONNECT + schema/table/sequence privileges.
# MAGIC
# MAGIC Idempotent: safe to re-run after every bundle deploy.

# COMMAND ----------

# MAGIC %pip install psycopg[binary]==3.2.3

# COMMAND ----------

dbutils.widgets.text("instance_name", "")
dbutils.widgets.text("app_sp_client_id", "")
dbutils.widgets.text("schema_name", "hermes_session")

instance_name = dbutils.widgets.get("instance_name") or "hermes-db"
app_sp_client_id = dbutils.widgets.get("app_sp_client_id") or ""
schema_name = dbutils.widgets.get("schema_name") or "hermes_session"

print(f"instance_name={instance_name}")
print(f"app_sp_client_id={app_sp_client_id or '(none)'}")
print(f"schema_name={schema_name}")

# COMMAND ----------

import uuid
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
instance = w.database.get_database_instance(name=instance_name)
print(f"Resolved Lakebase host: {instance.read_write_dns}")

current_user = spark.sql("SELECT current_user()").collect()[0][0]
print(f"Connecting as Databricks user: {current_user}")

cred = w.database.generate_database_credential(
    request_id=str(uuid.uuid4()),
    instance_names=[instance_name],
)

# COMMAND ----------

import psycopg

conn = psycopg.connect(
    host=instance.read_write_dns,
    port=5432,
    dbname="databricks_postgres",
    user=current_user,
    password=cred.token,
    sslmode="require",
    autocommit=True,
)
print("Lakebase connection established")

# COMMAND ----------

DDL_PRELUDE = [
    "CREATE EXTENSION IF NOT EXISTS databricks_auth",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"',
    f'SET search_path TO "{schema_name}"',
]

OPTIONAL_DDL_MARKERS = (
    # Statements that depend on optional extensions. They will be
    # silently skipped on Lakebase Free Edition (no pg_trgm
    # superuser), but kept for parity on full Lakebase deployments.
    "CREATE EXTENSION",
    "gin_trgm_ops",
)


def _is_optional(stmt: str) -> bool:
    return any(marker in stmt for marker in OPTIONAL_DDL_MARKERS)

DDL_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id                    TEXT PRIMARY KEY,
        source                TEXT NOT NULL,
        user_id               TEXT,
        model                 TEXT,
        model_config          JSONB,
        system_prompt         TEXT,
        parent_session_id     TEXT,
        started_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
        ended_at              TIMESTAMPTZ,
        end_reason            TEXT,
        message_count         INTEGER DEFAULT 0,
        tool_call_count       INTEGER DEFAULT 0,
        input_tokens          INTEGER DEFAULT 0,
        output_tokens         INTEGER DEFAULT 0,
        cache_read_tokens     INTEGER DEFAULT 0,
        cache_write_tokens    INTEGER DEFAULT 0,
        reasoning_tokens      INTEGER DEFAULT 0,
        billing_provider      TEXT,
        billing_base_url      TEXT,
        billing_mode          TEXT,
        estimated_cost_usd    DOUBLE PRECISION,
        actual_cost_usd       DOUBLE PRECISION,
        cost_status           TEXT,
        cost_source           TEXT,
        pricing_version       TEXT,
        title                 TEXT,
        api_call_count        INTEGER DEFAULT 0,
        handoff_state         JSONB,
        handoff_platform      TEXT,
        handoff_error         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id                     BIGSERIAL PRIMARY KEY,
        session_id             TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        role                   TEXT NOT NULL,
        content                TEXT,
        tool_call_id           TEXT,
        tool_calls             JSONB,
        tool_name              TEXT,
        timestamp              TIMESTAMPTZ NOT NULL DEFAULT now(),
        token_count            INTEGER,
        finish_reason          TEXT,
        reasoning              TEXT,
        reasoning_content      TEXT,
        reasoning_details      JSONB,
        codex_reasoning_items  JSONB,
        codex_message_items    JSONB
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tool_calls (
        id              TEXT PRIMARY KEY,
        session_id      TEXT NOT NULL,
        message_id      BIGINT,
        tool_name       TEXT NOT NULL,
        arguments       JSONB,
        status          TEXT NOT NULL DEFAULT 'started',
        started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        ended_at        TIMESTAMPTZ,
        result_preview  TEXT,
        error           TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tool_results (
        id             BIGSERIAL PRIMARY KEY,
        session_id     TEXT NOT NULL,
        tool_call_id   TEXT,
        tool_name      TEXT NOT NULL,
        result         JSONB,
        result_text    TEXT,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_events (
        id          BIGSERIAL PRIMARY KEY,
        session_id  TEXT,
        ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
        kind        TEXT NOT NULL,
        payload     JSONB
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_ledger (
        id                  BIGSERIAL PRIMARY KEY,
        session_id          TEXT,
        ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
        provider            TEXT,
        model               TEXT,
        prompt_tokens       INTEGER,
        completion_tokens   INTEGER,
        total_tokens        INTEGER,
        cache_read_tokens   INTEGER,
        cache_write_tokens  INTEGER,
        reasoning_tokens    INTEGER,
        estimated_cost_usd  DOUBLE PRECISION,
        raw_usage           JSONB
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_events (
        id           TEXT PRIMARY KEY,
        ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
        kind         TEXT NOT NULL,
        channel      TEXT,
        session_id   TEXT,
        thread_id    TEXT,
        payload      JSONB
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cron_jobs (
        id              TEXT PRIMARY KEY,
        name            TEXT,
        schedule        TEXT,
        prompt          TEXT,
        enabled         BOOLEAN DEFAULT TRUE,
        last_run_at     TIMESTAMPTZ,
        next_run_at     TIMESTAMPTZ,
        last_status     TEXT,
        last_error      TEXT,
        config          JSONB,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kv_state (
        key         TEXT PRIMARY KEY,
        value       JSONB,
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_messages_session_ts
        ON messages(session_id, timestamp)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_messages_content_trgm
        ON messages USING gin (content gin_trgm_ops)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_events_ts
        ON agent_events(ts DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_usage_ledger_session
        ON usage_ledger(session_id, ts DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tool_calls_session
        ON tool_calls(session_id, started_at DESC)
    """,
]

applied = 0
skipped = 0
for stmt in DDL_PRELUDE + DDL_TABLES:
    label = stmt.strip().splitlines()[0][:80]
    try:
        with conn.cursor() as cur:
            cur.execute(stmt)
        applied += 1
    except Exception as exc:
        if _is_optional(stmt):
            skipped += 1
            print(f"SKIP optional DDL ({label}): {exc}")
        else:
            print(f"FAIL DDL ({label}): {exc}")
            raise
print(f"DDL applied: {applied}, skipped: {skipped}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Grant the Databricks App service principal access

# COMMAND ----------

if app_sp_client_id:
    # Ensure the Postgres role for the App SP exists. On Lakebase
    # Free Edition the SQL extension `databricks_create_role()` is
    # not installed, so we use the Databricks REST API to create the
    # role first (idempotent — 409 means it already exists). Run
    # this from the bundle deploy or once manually; we just no-op if
    # the role is already present.
    try:
        w.database.create_database_instance_role(
            instance_name=instance_name,
            database_instance_role={
                "name": app_sp_client_id,
                "identity_type": "SERVICE_PRINCIPAL",
            },
        )
        print(f"Created Postgres role for SP {app_sp_client_id} (via REST API)")
    except Exception as exc:
        msg = str(exc).lower()
        if "already" in msg or "exists" in msg or "409" in msg:
            print(f"Postgres role for SP {app_sp_client_id} already exists (ok)")
        else:
            print(f"create_database_instance_role: {exc} (continuing)")

    with conn.cursor() as cur:
        grant_stmts = [
            f'GRANT CONNECT ON DATABASE databricks_postgres TO "{app_sp_client_id}"',
            f'GRANT USAGE, CREATE ON SCHEMA "{schema_name}" TO "{app_sp_client_id}"',
            f'GRANT ALL ON ALL TABLES IN SCHEMA "{schema_name}" TO "{app_sp_client_id}"',
            f'GRANT ALL ON ALL SEQUENCES IN SCHEMA "{schema_name}" TO "{app_sp_client_id}"',
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema_name}" GRANT ALL ON TABLES TO "{app_sp_client_id}"',
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema_name}" GRANT ALL ON SEQUENCES TO "{app_sp_client_id}"',
        ]
        for stmt in grant_stmts:
            try:
                cur.execute(stmt)
                print(f"OK: {stmt}")
            except Exception as exc:
                print(f"GRANT failed ({stmt}): {exc}")
                raise
else:
    print("No app_sp_client_id provided — skipping GRANT step.")

# COMMAND ----------

conn.close()
print("setup_lakebase complete")
