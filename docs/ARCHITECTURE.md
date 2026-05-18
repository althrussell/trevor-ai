# Hermes on Databricks — Architecture

This document describes the runtime architecture that lets the full
[Nous Research Hermes Agent](https://github.com/nousresearch/hermes-agent)
(`hermes-agent==0.14.0`) execute inside a Databricks Free Edition
workspace using only Databricks-managed services for state, files,
secrets, and inference.

## Goals

1. **Preserve the real Hermes runtime.** No simplified rewrite. The same
   `AIAgent`, `run_conversation`, tool registry, provider abstraction,
   context compressor, skills loader, and cron scheduler that ship in
   `hermes-agent` are loaded and executed.
2. **Replace local-machine assumptions with Databricks-managed services.**
   * Postgres on Lakebase replaces SQLite for durable session state.
   * UC Volumes (via the SDK Files API) replace `~/.hermes` on disk for
     home/config/skills/artifacts/logs.
   * Databricks Secrets replace `.env` files for tokens.
   * Databricks Model Serving (OSS endpoints) replaces external paid
     model providers.
3. **Stay deployable as a Databricks Asset Bundle.** Everything ships
   via `databricks bundle deploy -t free`.

## High-level diagram

```text
       ┌──────────────────────────┐
       │  Telegram bot (outbound  │
       │  long polling only)      │
       └────────────┬─────────────┘
                    │ getUpdates / sendMessage
                    ▼
   ┌────────────────────────────────────────┐
   │ Databricks App: hermes-agent           │
   │ ┌──────────────────────────────────┐   │
   │ │ FastAPI lifespan supervisor      │   │
   │ │ (app/app.py)                     │   │
   │ │  - boot Hermes runtime           │   │
   │ │  - boot Telegram poller          │   │
   │ │  - boot cron tick                │   │
   │ │  - expose /health /ready /debug  │   │
   │ └──────────────────────────────────┘   │
   │                │                       │
   │                ▼                       │
   │ ┌──────────────────────────────────┐   │
   │ │ Full Hermes AIAgent              │   │
   │ │ (pip install hermes-agent)       │   │
   │ │  - run_conversation              │   │
   │ │  - tool registry & toolsets      │   │
   │ │  - context compression           │   │
   │ │  - cron / skills / memory        │   │
   │ └──────────────────────────────────┘   │
   │   │             │              │       │
   │   │             │              │       │
   │   ▼             ▼              ▼       │
   │ provider     SessionDB      home_fs    │
   │ adapter     adapter        adapter     │
   │   │             │              │       │
   └───┼─────────────┼──────────────┼───────┘
       │             │              │
       ▼             ▼              ▼
 ┌──────────┐  ┌────────────┐  ┌────────────┐
 │ Databricks│  │  Lakebase  │  │ UC Volume  │
 │  Model    │  │  Postgres  │  │ (SDK Files │
 │  Serving  │  │ databricks_│  │   API)     │
 │  OAI-comp │  │  postgres  │  │            │
 └──────────┘  └────────────┘  └────────────┘
```

## Components

### 1. Databricks Asset Bundle (`databricks.yml`, `resources/`)

A single bundle (`hermes-on-databricks`) provisions everything:

| Resource | Purpose |
|----------|---------|
| `schemas.hermes_schema` | UC schema for all owned objects |
| `volumes.hermes_home_volume` | Durable Hermes `HERMES_HOME` files |
| `volumes.hermes_artifacts_volume` | Tool/agent artifacts |
| `database_instances.hermes_db` | Lakebase (Postgres) for session state |
| `apps.hermes_app` | The FastAPI app that loads Hermes |
| `jobs.setup_lakebase` | One-shot DDL + SP grants notebook |

App resource bindings (in `resources/app.yml`) grant the app's
service principal `WRITE_VOLUME` on both volumes, `CAN_QUERY` on the
default serving endpoint, and `READ` on the configured secrets. No
broader workspace permissions are granted.

### 2. Databricks App (`app/app.py`, `app/app.yaml`)

A FastAPI application launched by `uvicorn`. Its lifespan handler:

1. Resolves config from env (`hermes_databricks.config`).
2. Initialises a `UCVolumeHome` and downloads any existing
   `HERMES_HOME` contents from UC Volume into `/tmp/hermes_cache`.
3. Sets `HERMES_HOME` to the cache directory and bootstraps Hermes
   (`hermes_cli.config.ensure_hermes_home`).
4. Constructs a `LakebaseSessionDB` (duck-typed `SessionDB` replacement).
5. Builds an `AIAgent` with `session_db=…`, then swaps its OpenAI
   `client` for the WorkspaceClient OpenAI-compatible client.
6. Starts background coroutines: Telegram long-polling, cron tick,
   periodic UC Volume sync, periodic heartbeat.
7. Exposes `/health`, `/ready`, and a debug API surface.
8. On shutdown, cancels tasks and best-effort syncs cached state back
   to UC Volume.

### 3. Databricks model provider (`hermes_databricks/databricks_provider.py`)

Returns a refresh-aware OpenAI-compatible client built from
`WorkspaceClient().serving_endpoints.get_open_ai_client()`. Auth headers
are minted per-request by the SDK's `httpx` Bearer auth, so token
rotation is automatic. The provider's `model` attribute is set to the
serving endpoint name (default `databricks-qwen3-next-80b-a3b-instruct`).
A `ProviderProfile` is also registered with Hermes' provider registry
so `hermes model` and CLI introspection report `databricks` as a first-
class provider.

### 4. Lakebase-backed `SessionDB` (`hermes_databricks/state/*`)

`LakebaseSessionDB` implements the subset of methods Hermes' core
loop calls against `SessionDB`:

* `create_session`, `get_session`, `ensure_session`, `end_session`
* `append_message`, `get_messages`, `replace_messages`, `clear_messages`
* `update_system_prompt`, `update_token_counts`, `set_session_title`,
  `get_session_title`, `get_compression_tip`
* `resolve_session_id`, `resolve_session_by_title`
* `search_messages` (ILIKE-based fallback; FTS5 is SQLite-only)
* meta helpers (`get_meta`, `set_meta`, `vacuum` no-op)

Schema lives in `hermes_databricks/state/schema.sql` and matches the
columns Hermes actually writes (input/output tokens, cache tokens,
reasoning tokens, cost fields, handoff fields, etc.). FTS5 specifics
are replaced by Postgres GIN indexes plus `to_tsvector` / `pg_trgm`
where appropriate, with `search_messages` documented as having
limitations vs. SQLite FTS5.

### 5. UC Volume home (`hermes_databricks/fs/*`)

`UCVolumeHome` wraps the Databricks SDK Files API and gives Hermes a
durable home directory:

* On boot: pull all files under the configured volume prefix into
  `/tmp/hermes_cache/hermes_home/`.
* During run: writes are made to the local cache; "durable" subtrees
  (`skills/`, `cron/`, `memories/`, `logs/curator/`, `sessions/`,
  `image_cache/`, `audio_cache/`) are scheduled for upload via the
  `CacheSync` writer.
* On shutdown: best-effort full upload.
* All paths are validated through a guard that prevents escaping the
  configured volume root.

Hermes' own `state.db` is never used as the system of record: we set
`HERMES_HOME` to the cache, but the `SessionDB` instance Hermes
operates against is the Lakebase one, so SQLite is only ever touched
by code paths that bypass `_session_db` (e.g. `kanban.db`, which lives
in-cache only).

### 6. Telegram long polling (`hermes_databricks/telegram_polling.py`)

Direct port of the Living-AI pattern:

1. `deleteWebhook` at boot.
2. `getUpdates` with `timeout=25`, `allowed_updates=["message","edited_message"]`.
3. Allowlist enforcement against `telegram_primary_user_handle` /
   `telegram_allowed_users`.
4. Each incoming chat maps to a Hermes session (`telegram:{chat_id}`).
5. Hermes' `run_conversation` runs inside `asyncio.to_thread` so the
   event loop stays responsive.
6. Responses are sent via `sendMessage`; Markdown failures fall back
   to plain text.

### 7. Tool backends (`hermes_databricks/tools/*`)

Hermes' tool registry is preserved unchanged. We provide:

* `DatabricksToolset` — first-class Hermes tools that query
  serving endpoints, list jobs, describe UC tables, run SELECT-only
  SQL, read/write to allowed UC Volume prefixes, and run allowlisted
  jobs.
* `TerminalBackend` — backend selector (`in_app_subprocess`,
  `databricks_job`, `external_sandbox`). Default in Free Edition is
  `in_app_subprocess` with timeouts and cwd guard.
* `BrowserBackend` — selector (`in_app_playwright`,
  `databricks_job_browser`, `external_browser`). Default `disabled`;
  returns explicit diagnostic if invoked.
* `MCPBackend` — selector (`disabled`, `remote_http`, `local_stdio`).
  Default `disabled`.

The matrix is documented in `docs/TOOL_BACKEND_MATRIX.md`.

### 8. Cron / scheduler

Hermes ships a cron scheduler that reads jobs from
`HERMES_HOME/cron/jobs.json`. Since `HERMES_HOME` is now UC-backed,
the jobs file is durable. The supervisor schedules `cron.scheduler.tick`
every 60s. Heavyweight scheduled jobs can be delegated to Databricks
Jobs via a future `databricks_job` cron backend.

### 9. Observability (`hermes_databricks/observability/*`)

* Structured JSON logging with a `RedactingFormatter` that strips
  bearer tokens, Postgres URIs with credentials, and Telegram tokens.
* `agent_events` table in Lakebase records start/stop/model/tool/error
  events with a `payload` JSONB column.
* `/debug/*` endpoints expose runtime, sessions, events, tool registry,
  recent usage, and Telegram status.

## Sequence: a Telegram turn

```text
Telegram user → getUpdates loop receives a Message
              → supervisor.on_telegram_message(update)
              → session_id = f"telegram:{chat_id}"
              → ensure session row exists in Lakebase
              → asyncio.to_thread(agent.run_conversation,
                                   user_message=text,
                                   session_id=session_id)
              → Hermes builds messages from LakebaseSessionDB
              → Hermes calls agent.client.chat.completions.create
              → Workspace OpenAI client signs request with fresh token
              → Databricks Model Serving returns completion
              → Hermes dispatches any tool calls
              → Hermes persists messages via LakebaseSessionDB
              → Final assistant text returned
              → telegram.send_message(chat_id, text)
```

## Non-goals (explicit)

* We do **not** ship Hermes' web dashboard inside the Databricks App.
  Operators use `/debug/*` JSON endpoints; running the React TUI is
  out of scope for Free Edition.
* We do **not** run Hermes' MCP stdio server inside the App by
  default. MCP can be enabled by configuring a remote HTTP MCP server.
* We do **not** ship the browser/computer-use toolsets enabled by
  default — they require a sandbox the App container does not provide.
* We do **not** support inbound Telegram webhooks: the Databricks App
  is OAuth-fronted and rejects anonymous requests.

## Component-to-file mapping

| Concern | File |
|---------|------|
| Bundle | `databricks.yml`, `resources/*.yml` |
| App entrypoint | `app/app.py`, `app/app.yaml` |
| Config | `app/hermes_databricks/config.py` |
| Hermes wiring | `app/hermes_databricks/runtime.py` |
| Model provider | `app/hermes_databricks/databricks_provider.py` |
| Lakebase pool | `app/hermes_databricks/lakebase.py` |
| Session adapter | `app/hermes_databricks/state/lakebase_session_db.py` |
| Volume FS | `app/hermes_databricks/fs/volume_fs.py`, `cache_sync.py` |
| Telegram | `app/hermes_databricks/telegram_polling.py` |
| Supervisor | `app/hermes_databricks/supervisor.py` |
| Tools | `app/hermes_databricks/tools/*` |
| Observability | `app/hermes_databricks/observability/*` |
| Lakebase DDL | `sql/setup_lakebase.py`, `app/hermes_databricks/state/schema.sql` |
| Scripts | `scripts/{deploy,destroy,smoke_test,local_dev}.sh` |
| Tests | `tests/{unit,integration,smoke}/` |
