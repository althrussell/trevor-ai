-- Hermes-on-Databricks Lakebase schema.
-- Applied by sql/setup_lakebase.py at deploy time and by
-- LakebaseSessionDB.ensure_schema() as a self-bootstrap fallback.
--
-- The DDL is idempotent (CREATE ... IF NOT EXISTS), safe to re-run.

CREATE SCHEMA IF NOT EXISTS hermes_session;
SET search_path TO hermes_session;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

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
);

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
);

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
);

CREATE TABLE IF NOT EXISTS tool_results (
    id             BIGSERIAL PRIMARY KEY,
    session_id     TEXT NOT NULL,
    tool_call_id   TEXT,
    tool_name      TEXT NOT NULL,
    result         JSONB,
    result_text    TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memory_events (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT NOT NULL,
    payload     JSONB
);

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
);

CREATE TABLE IF NOT EXISTS agent_events (
    id           TEXT PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind         TEXT NOT NULL,
    channel      TEXT,
    session_id   TEXT,
    thread_id    TEXT,
    payload      JSONB
);

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
);

CREATE TABLE IF NOT EXISTS kv_state (
    key         TEXT PRIMARY KEY,
    value       JSONB,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_session_ts
    ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_content_trgm
    ON messages USING gin (content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_agent_events_ts
    ON agent_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_ledger_session
    ON usage_ledger(session_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session
    ON tool_calls(session_id, started_at DESC);
