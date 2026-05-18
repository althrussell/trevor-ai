# Runtime Constraints — Hermes Inside Databricks Apps (Free Edition)

This is a brutally-honest list of the constraints we have to design
around. If a feature in Hermes assumes any of these things are
available, we either provide a Databricks-compatible substitute or
gate the feature behind an explicit backend selector.

## 1. Filesystem

* **Writable, but ephemeral.** The container has a writable root
  filesystem and `/tmp`, but **nothing in it survives across App
  restarts.** Anything Hermes wants to persist must end up in UC
  Volumes or Lakebase.
* **No POSIX `/Volumes` mount in Databricks Apps.** Notebooks can
  read `/Volumes/<catalog>/<schema>/<volume>` directly because the
  cluster mounts UC volumes. Apps do **not**. All UC Volume I/O must
  go through the Databricks SDK Files API
  (`WorkspaceClient().files.download/upload/list/...`).
* **No NFS or shared disk between replicas.** If the App is scaled,
  multiple instances do not share local writes. The Lakebase
  SessionDB makes this tolerable for session state, and UC Volume
  uploads make it tolerable for skills/cron config; SQLite WAL across
  replicas is **not**.

## 2. Network

* **Outbound HTTPS** to public hosts works (Telegram, Hugging Face,
  PyPI, model endpoints).
* **Inbound HTTPS** is gated by Databricks workspace OAuth. Anonymous
  requests are rejected, so:
  * Telegram webhooks **cannot** be received.
  * Any external scheduler that expects to POST to the App must be
    configured to use a Databricks PAT or OAuth token.
* **No fixed inbound IP.** Outbound polling pattern is mandatory for
  chat platforms.

## 3. Processes and concurrency

* **Single-process FastAPI/uvicorn** by default in Free Edition.
  `asyncio` event loop is the main coordinator.
* **No long-lived child processes** survive across restarts. Any
  Hermes feature that spawns a daemonized helper (e.g. `hermes
  gateway` as a background process) must run inside the supervisor's
  task group so it shuts down cleanly.
* **`fork`-based parallelism** that Hermes uses in delegate/code-exec
  tools works but is bounded by container CPU/memory limits. Be
  conservative with `max_iterations` and `tool_delay`.

## 4. Authentication

* The App is granted a **workspace service principal** identity when
  it starts. The runtime exposes this via:
  * `DATABRICKS_CLIENT_ID` — env var. Used as the Postgres user.
  * `DATABRICKS_HOST` / OAuth env — picked up by `WorkspaceClient()`
    automatically.
* Hermes' provider profiles expect API keys via env vars
  (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.). For Databricks Model
  Serving we **do not** populate these. Instead, the Hermes `client`
  is swapped with `WorkspaceClient().serving_endpoints.get_open_ai_client()`,
  which uses the SP token via `httpx` Bearer auth that refreshes on
  every request.

## 5. Lakebase (managed Postgres)

* Created via bundle `database_instances` resource.
* Connection details:
  * Host: `WorkspaceClient().database.get_database_instance(name).read_write_dns`.
  * Port: `5432`.
  * Database: `databricks_postgres`.
  * User: `DATABRICKS_CLIENT_ID` (the SP's OAuth client id).
  * Password: short-lived token from
    `WorkspaceClient().database.generate_database_credential(request_id=uuid,
    instance_names=[name])`. **TTL is 1 hour.** We rotate at 50
    minutes (matches Living-AI pattern).
  * `sslmode="require"`, `autocommit=True`.
* The SP must be GRANTed permissions on the schema before it can
  use the database. The bundle's `setup_lakebase` job runs as the
  deploying user, creates the role via
  `SELECT databricks_create_role(<sp_client_id>, 'SERVICE_PRINCIPAL')`,
  and issues schema/table grants.
* **No `CREATE EXTENSION` for FTS5 / pg_trgm** without superuser.
  `pg_trgm` is generally available; FTS5 is SQLite-specific. Our
  `search_messages` implementation uses ILIKE first, with a path to
  GIN+`to_tsvector` later. Documented as a known limitation.

## 6. UC Volumes (managed)

* MANAGED volumes with `WRITE_VOLUME` granted to the App SP via the
  `apps.<name>.resources.uc_securable` block.
* Path style: `/Volumes/<catalog>/<schema>/<volume>/<...>`.
* All reads/writes through Files API:
  * `files.download(path)` → `DownloadResponse` with `.contents`
    (binary stream).
  * `files.upload(path, contents=BytesIO|bytes, overwrite=True)`.
  * `files.list_directory_contents(path)` (paginates `DirectoryEntry`).
  * `files.delete(path)`.
* Files API does not enforce atomic rename; concurrent writers can
  produce torn states. We avoid this by treating UC Volume as the
  durable mirror of `/tmp/hermes_cache`, written from a single async
  writer task.

## 7. Secrets

* Use Databricks Secrets, **not** local `.env`.
* App binds individual secret keys via
  `resources.apps.<name>.resources.secret` with `permission: READ`.
  Bindings inject the secret as an environment variable into the App
  container (`DATABRICKS_SECRET_<NAME>`-style). We also support
  reading them lazily via `WorkspaceClient().secrets.get_secret`.
* Values are returned **base64-encoded** by the API. Both the binding
  and the lazy path produce decoded values, but the lazy path must
  call `base64.b64decode` (same as Living-AI).

## 8. Databricks Model Serving

* Default endpoint: `databricks-qwen3-next-80b-a3b-instruct` (Free
  Edition has a number of OSS endpoints pre-provisioned).
* Endpoints are OpenAI-compatible; the URL is
  `https://<workspace>/serving-endpoints/<endpoint>/invocations` for
  REST, and the OpenAI client returned by
  `get_open_ai_client()` already targets `…/serving-endpoints` with
  `model=<endpoint>` passed per-request.
* Tool/`tool_choice` support varies by endpoint. The Qwen3 endpoint
  supports OpenAI-style `tools` parameter. We pass Hermes' tool
  schemas through unchanged; we set `tool_choice="auto"`.
* Token usage is returned in the standard OpenAI shape; we route it
  to `LakebaseSessionDB.update_token_counts` and `usage_ledger`.
* Failures: `429 RESOURCE_EXHAUSTED` (concurrency cap), `503
  UNAVAILABLE` (endpoint scaling), `PERMISSION_DENIED` (SP lacks
  `CAN_QUERY`). Each is mapped to a structured log + agent_events row.

## 9. Hermes assumptions to override

| Hermes assumption | What we do |
|-------------------|------------|
| `HERMES_HOME` writable across restarts | Mirror to UC Volume via `UCVolumeHome` |
| `state.db` is the source of truth | Pass a `session_db=` adapter; `state.db` is ignored |
| `~/.hermes/.env` provides API keys | We do not load it; provider gets a swapped OpenAI client |
| Terminal tool can spawn shells | Allowed in-app with strict cwd/timeout; opt-in Databricks Job backend |
| Browser tool can launch Chromium | Disabled by default; explicit `external_browser` opt-in |
| `mcp_serve.py` runs alongside CLI | Not started; remote-HTTP MCP only |
| Cron tick runs in `hermes gateway` | Supervisor schedules `cron.scheduler.tick` |
| Verbose stderr logging | Routed through `hermes_logging` with our redactor and structured JSON formatter |

## 10. What we do **not** support out of the box

* Voice mode (mic capture / TTS playback) — App container has no audio devices.
* `hermes dashboard` (FastAPI dashboard inside `web` extra) — we own the FastAPI app, and the dashboard wants to run its own server.
* `acp_adapter` — that's a stdio IDE adapter, no purpose in a server context.
* Native MCP stdio binaries — could be re-enabled via a Databricks Job runner.
* Computer-use (`cua-driver`) — macOS-only.

If we later want any of these, the path is: implement a `Backend` that
forwards the workload to a Databricks Job (which can install binaries
and have larger resource limits), then expose a status row in
`/debug/tools` so the operator can see whether the backend is wired.

## 11. SessionDB compatibility — full method inventory

Methods Hermes calls on `SessionDB` (from `hermes_state.py`, schema
version 11). Our `LakebaseSessionDB` implements the ones marked ✅,
stubs (no-op or `NotImplementedError`-with-diagnostic) the ones
marked ⚠️, and skips (never called by core loop) the ones marked ❌.

| Method | Status |
|--------|--------|
| `__init__(db_path=None)` | ✅ replaced signature: `(lakebase, schema="hermes_session")` |
| `close()` | ✅ closes pool |
| `create_session(session_id, source, **kwargs) -> str` | ✅ |
| `ensure_session(session_id, source, **kwargs) -> str` | ✅ |
| `end_session(session_id, end_reason)` | ✅ |
| `reopen_session(session_id)` | ✅ |
| `update_system_prompt(session_id, system_prompt)` | ✅ |
| `update_token_counts(session_id, ..., absolute=False)` | ✅ |
| `append_message(...) -> int` | ✅ |
| `replace_messages(session_id, messages)` | ✅ |
| `get_messages(session_id) -> list[dict]` | ✅ |
| `get_messages_around(session_id, anchor_msg_id, before, after)` | ✅ |
| `get_anchored_view(...)` | ✅ |
| `get_messages_as_conversation(session_id) -> list[dict]` | ✅ |
| `resolve_resume_session_id(session_id) -> str` | ✅ |
| `clear_messages(session_id)` | ✅ |
| `get_session(session_id) -> dict | None` | ✅ |
| `resolve_session_id(prefix) -> str | None` | ✅ |
| `set_session_title(session_id, title) -> bool` | ✅ |
| `get_session_title(session_id) -> str | None` | ✅ |
| `get_session_by_title(title) -> dict | None` | ✅ |
| `resolve_session_by_title(title) -> str | None` | ✅ |
| `get_next_title_in_lineage(base_title) -> str` | ✅ |
| `get_compression_tip(session_id) -> str | None` | ✅ |
| `list_sessions_rich(...) -> list` | ✅ |
| `search_messages(query, ...)` | ⚠️ ILIKE-only; FTS5 unavailable |
| `search_sessions(query, ...)` | ⚠️ ILIKE-only |
| `session_count(source=None) -> int` | ✅ |
| `message_count(session_id=None) -> int` | ✅ |
| `export_session(session_id) -> dict | None` | ✅ |
| `export_all(source=None) -> list` | ✅ |
| `delete_session(session_id, sessions_dir=None)` | ✅ |
| `prune_sessions(...)` | ✅ |
| `prune_empty_ghost_sessions(...)` | ✅ |
| `finalize_orphaned_compression_sessions()` | ✅ |
| `get_meta(key)` / `set_meta(key, value)` | ✅ |
| `vacuum()` | ✅ (no-op) |
| `maybe_auto_prune_and_vacuum(...)` | ✅ (delegates) |
| Telegram topic helpers | ⚠️ stub returns "topic mode unsupported" |
| Handoff helpers (`request_handoff`, ...) | ⚠️ stub returns no-op |
| `apply_telegram_topic_migration()` | ⚠️ stub |
| `sanitize_title(title)` (static) | ✅ |

The two `⚠️ stub` categories return safe no-op responses (empty lists,
`False`, `None`) so they don't break callers. They're tracked as
follow-ups; the core conversation loop does not depend on them.
