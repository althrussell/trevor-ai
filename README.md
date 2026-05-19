# Hermes on Databricks

Run the **full [NousResearch Hermes](https://github.com/nousresearch/hermes-agent)
agent runtime** inside a **Databricks App** on **Databricks Free
Edition**, using:

- **Databricks Foundation Model / Serving Endpoints** (default
  `databricks-qwen3-next-80b-a3b-instruct`) as the LLM provider, via
  the OpenAI-compatible client the Databricks SDK exposes.
- **Lakebase Postgres** as Hermes' durable session/state store
  (replaces local SQLite).
- **Unity Catalog Volumes** as Hermes' durable home directory
  (`HERMES_HOME`, skills, cron jobs, memories, artifacts).
- **Databricks Secrets** for the Telegram bot token and optional
  third-party provider keys.
- **Databricks Asset Bundles** as the deployment and resource-
  provisioning mechanism.

The Hermes agent itself is **not** rewritten or stripped down. We
preserve its agent loop, tool registry, tool calling, memory/session
model, skills, cron scheduler, and provider abstraction, and slot
Databricks-native compatibility layers in underneath.

---

## 1. What this is (and isn't)

| | |
|---|---|
| ✅ The real `AIAgent` from `hermes-agent` | ❌ A simplified or "demo" Hermes |
| ✅ Hermes' real `run_conversation` loop | ❌ A one-shot chat handler |
| ✅ Hermes' real tool registry + dispatch | ❌ Hand-picked subset of tools |
| ✅ Databricks-hosted OSS model endpoint by default | ❌ Requires OpenAI/Anthropic keys |
| ✅ Lakebase-backed `SessionDB`-compatible store | ❌ Local SQLite as source of truth |
| ✅ UC Volume mirror for `HERMES_HOME` | ❌ Ephemeral local-only home |
| ✅ Outbound Telegram long-polling | ❌ Telegram webhooks (Apps reject anon inbound) |
| ✅ Explicit "backend required" diagnostics for browser/MCP/etc. | ❌ Silently disabled features |

---

## 2. Architecture at a glance

```
Telegram (outbound long-poll)
       │
       ▼
Databricks App: hermes-agent  ←── workspace OAuth gate
  └── FastAPI + uvicorn
        └── HermesSupervisor (asyncio)
              ├── HermesRuntime
              │    ├── Hermes AIAgent (real)
              │    │    ├── tool_registry  ──▶ Databricks toolset, browser, mcp, ...
              │    │    └── run_conversation
              │    ├── DatabricksOpenAIClientFactory
              │    │    └── WorkspaceClient().serving_endpoints.get_open_ai_client()
              │    ├── LakebaseSessionDB   ──▶ Postgres tables: sessions, messages,
              │    │                            tool_calls, tool_results, usage_ledger,
              │    │                            agent_events, kv_state
              │    └── UCVolumeHome        ──▶ /Volumes/<catalog>/<schema>/hermes_home
              ├── TelegramClient (long-poll, allowlist-gated)
              ├── Heartbeat task (UC Volume push)
              └── Cron tick task (Hermes scheduler)
```

See `docs/ARCHITECTURE.md`, `docs/RUNTIME_CONSTRAINTS.md`,
`docs/PORTING_PLAN.md`, and `docs/TOOL_BACKEND_MATRIX.md` for
implementation depth.

---

## 3. Repository layout

```
.
├── README.md                              ← you are here
├── databricks.yml                         ← bundle entrypoint
├── resources/
│   ├── schema.yml         (UC schema)
│   ├── volumes.yml        (hermes_home + artifacts volumes)
│   ├── lakebase.yml       (Postgres instance)
│   ├── setup_job.yml      (one-shot DDL + GRANTs)
│   ├── app.yml            (Databricks App + resource bindings)
│   ├── permissions.yml    (least-privilege grants)
│   └── jobs.yml           (optional: heavy cron/terminal back-ends)
├── app/
│   ├── app.py             (FastAPI lifespan supervisor)
│   ├── app.yaml           (Databricks App runtime + env vars)
│   ├── requirements.txt   (hermes-agent + databricks-sdk + ...)
│   └── hermes_databricks/
│       ├── config.py
│       ├── runtime.py
│       ├── supervisor.py
│       ├── databricks_provider.py
│       ├── telegram_polling.py
│       ├── health.py
│       ├── lakebase.py
│       ├── state/
│       │   ├── lakebase_session_db.py    (SessionDB duck-type)
│       │   └── schema.sql                (Lakebase DDL)
│       ├── fs/
│       │   ├── volume_fs.py              (UC Volume mirror)
│       │   └── cache_sync.py
│       ├── tools/
│       │   ├── databricks_toolset.py     (databricks_* tools)
│       │   ├── sql_guard.py              (SELECT/WITH validator)
│       │   ├── terminal_backend.py       (in_app_subprocess + alts)
│       │   ├── browser_backend.py        (diagnostic-only by default)
│       │   ├── mcp_backend.py            (diagnostic-only by default)
│       │   └── backend_registry.py       (toolset selection)
│       └── observability/
│           ├── logging.py                (JSON + secret redaction)
│           └── tracing.py                (optional MLflow spans)
├── sql/
│   └── setup_lakebase.py                 (notebook for setup_job)
├── scripts/
│   ├── deploy.sh
│   ├── destroy.sh
│   ├── smoke_test.sh
│   └── local_dev.sh
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_sql_guard.py
│   │   ├── test_terminal_backend.py
│   │   ├── test_databricks_toolset.py
│   │   ├── test_volume_fs_guards.py
│   │   ├── test_logging_redaction.py
│   │   └── test_telegram_polling.py
│   └── run_offline_checks.py             (stdlib-only fallback runner)
├── docs/
│   ├── ARCHITECTURE.md
│   ├── PORTING_PLAN.md
│   ├── RUNTIME_CONSTRAINTS.md
│   └── TOOL_BACKEND_MATRIX.md
└── .gitignore
```

---

## 4. Prerequisites

1. **Databricks Free Edition workspace** (or any workspace with Apps,
   Lakebase, UC, and Model Serving).
2. **Databricks CLI v0.245+** (`databricks --version`).
3. An **OAuth profile** or PAT for that workspace:
   ```bash
   databricks auth login --host <workspace-url> --profile hermes-free
   ```
4. A **Telegram bot token** (from `@BotFather`) and your own Telegram
   **username** (used as the primary allowlisted user).
5. (Optional) A **SQL warehouse ID** if you want the
   `databricks_uc_query_readonly` tool to actually execute SQL.

---

## 5. Deployment

### One-shot

```bash
# 1. validate
databricks bundle validate -t free

# 2. deploy (UC schema, volumes, Lakebase instance, setup job, App)
databricks bundle deploy -t free

# 3. seed Lakebase: DDL from app/hermes_databricks/state/schema.sql
#    + GRANTs to the App SP. Idempotent.
databricks bundle run setup_lakebase -t free

# 4. populate secrets (Telegram + optional provider keys)
databricks secrets create-scope hermes_agent
databricks secrets put-secret hermes_agent telegram_bot_token            # <BotFather token>
databricks secrets put-secret hermes_agent telegram_primary_user_handle  # <your @handle without @>
# optional:
databricks secrets put-secret hermes_agent telegram_allowed_users        # CSV of handles
databricks secrets put-secret hermes_agent openai_api_key                # only if you want OpenAI fallback
databricks secrets put-secret hermes_agent exa_api_key                   # for web search
databricks secrets put-secret hermes_agent firecrawl_api_key             # alt search backend

# 5. start the App
databricks bundle run hermes_app -t free
```

Or use the helper script which wraps the same steps with sanity checks:

```bash
PROFILE=hermes-free scripts/deploy.sh
```

### What gets created

| Resource | Default name | Source |
|---|---|---|
| UC schema | `workspace.hermes_agent` | `resources/schema.yml` |
| UC volume (Hermes home) | `workspace.hermes_agent.hermes_home` | `resources/volumes.yml` |
| UC volume (artifacts) | `workspace.hermes_agent.hermes_artifacts` | `resources/volumes.yml` |
| Lakebase instance | `hermes-db` | `resources/lakebase.yml` |
| Setup job | `hermes_setup_lakebase` | `resources/setup_job.yml` |
| Databricks App | `hermes-agent` | `resources/app.yml` |
| Permissions | App SP → CAN_USE on endpoint, R/W on volumes, READ on secrets | `resources/permissions.yml` |

### Tearing it down

```bash
PROFILE=hermes-free scripts/destroy.sh
```

---

## 6. Configuration

### Bundle variables

Override at deploy time with `databricks bundle deploy -t free --var key=value`:

| Variable | Default | Purpose |
|---|---|---|
| `catalog` | `workspace` | UC catalog that owns the schema/volumes |
| `schema` | `hermes_agent` | UC schema |
| `app_name` | `hermes-agent` | App slug |
| `agent_name` | `Hermes` | Display name |
| `llm_endpoint` | `databricks-qwen3-next-80b-a3b-instruct` | Default Model Serving endpoint |
| `secrets_scope` | `hermes_agent` | Secrets scope for Telegram + provider keys |
| `lakebase_instance` | `hermes-db` | Lakebase Postgres instance name |
| `hermes_home_volume` | `hermes_home` | UC volume for `HERMES_HOME` |
| `artifacts_volume` | `hermes_artifacts` | UC volume for agent outputs |
| `warehouse_id` | `""` | SQL warehouse for the read-only query tool |
| `daily_token_cap` | `100000` (free) / `1000000` (prod) | Soft cap |
| `heartbeat_seconds` | `180` | Supervisor heartbeat cadence |
| `terminal_backend` | `in_app_subprocess` | `in_app_subprocess` / `databricks_job` / `external_sandbox` / `disabled` |
| `browser_backend` | `disabled` | `disabled` / `in_app_playwright` / `databricks_job_browser` / `external_browser` |
| `mcp_enabled` | `false` | Turn on Hermes' MCP toolset |
| `telegram_enabled` | `true` | Start the long-poll loop |

### Environment variables (set by `app/app.yaml`)

All begin with `HERMES_DATABRICKS_` and come from the bundle
variables. Add ad-hoc overrides under the `env:` list in `app/app.yaml`
if you need to flip a flag without redeploying the bundle.

### Secrets (`databricks secrets put-secret`)

| Key | Required? | Used by |
|---|---|---|
| `telegram_bot_token` | yes (if telegram_enabled) | `TelegramClient` |
| `telegram_primary_user_handle` | yes | allowlist |
| `telegram_allowed_users` | optional | CSV of additional allowlisted usernames |
| `openai_api_key` / `anthropic_api_key` | optional | Hermes fallback providers |
| `exa_api_key` / `firecrawl_api_key` / `tavily_api_key` | optional | Hermes web search |
| `slack_bot_token` | optional | future Slack channel |

The redacting JSON formatter in
`hermes_databricks.observability.logging` strips bearer tokens,
Telegram bot tokens, Postgres URIs, and `api_key=...` query-string
fragments from every log line before they reach stderr.

---

## 7. Endpoint reference

The App exposes the following endpoints. All are workspace-OAuth
gated (Databricks injects identity headers; anonymous calls are
rejected at the platform layer).

| Endpoint | Purpose |
|---|---|
| `GET /` | App identity + key paths |
| `GET /health` | Liveness ("alive" if process up) |
| `GET /ready` | Aggregated readiness from every registered subsystem probe; 503 if degraded |
| `GET /config` | Effective configuration (sans secrets) |
| `GET /hermes/status` | Hermes runtime status (`AIAgent` loaded, model endpoint, errors) |
| `GET /debug/runtime` | Wider runtime state including process info |
| `GET /debug/supervisor` | Background task names + done/cancelled/exception |
| `GET /debug/health-checks` | Last status of every registered probe |
| `GET /debug/tools` | Tool registry + backend matrix + Databricks-native tool list |
| `GET /debug/fs` | UC Volume mirror status (last sync, counts, errors) |
| `GET /debug/fs/list?path=…` | List a relative path under `HERMES_HOME` |
| `GET /debug/sessions` | List recent Lakebase sessions |
| `GET /debug/session/{id}` | Full session row + messages |
| `GET /debug/events?kind=…` | Filterable `agent_events` tail |
| `GET /debug/usage` | Recent `usage_ledger` rows |
| `GET /debug/cron` | Cron jobs + tick stats |
| `POST /debug/cron/run/{job_id}` | Manually run one cron job |
| `GET /debug/telegram` | Telegram poller status |
| `POST /debug/model-turn` | Run one Hermes conversation turn (`{"message": "…"}`) |

---

## 8. Validation

### Smoke test against a deployed App

```bash
PROFILE=hermes-free scripts/smoke_test.sh
```

Hits every endpoint above and prints the JSON responses. Returns
`0` on success, `1` on a core failure, `2` if an optional check
(sessions, cron, telegram) failed.

### Offline unit checks (no Databricks needed)

```bash
PYTHONPATH=app python3 tests/run_offline_checks.py
```

Exercises the pure-Python guards (SQL allowlist, terminal sandbox,
UC Volume path guard, secret redaction, Databricks toolset arg
validation, Telegram allowlist) using stdlib only. Useful as a CI
gate when pytest isn't available.

### Pytest suite

```bash
pip install pytest pytest-asyncio
PYTHONPATH=app pytest tests/unit -v
```

---

## 9. Tool backend matrix (summary)

| Toolset | Default in App | Backend |
|---|---|---|
| `core` / `file` / `web` / `memory` / `skills` / `session` / `cron` / `planner` / `delegation` | enabled | Pure Python; file ops routed through `UCVolumeHome` |
| `databricks` (this repo) | enabled | `WorkspaceClient` calls, gated by allowlists |
| `terminal` | enabled (in_app_subprocess) | cwd under `<HERMES_HOME>/workspace`, 60s default timeout |
| `browser` | disabled | Configure `HERMES_DATABRICKS_BROWSER_BACKEND` + secrets to enable |
| `mcp` | disabled | `HERMES_DATABRICKS_MCP_ENABLED=true` + `mcp_servers:` in `HERMES_HOME/config.yaml` |
| `voice` / `computer-use` / `homeassistant` | disabled | No audio / macOS / external network in App |
| `messaging` (Telegram) | enabled | Outbound poll; webhooks not viable |

The full per-tool table is in `docs/TOOL_BACKEND_MATRIX.md`.
**Tools are never silently removed** — if a backend is unavailable,
the tool stays registered and returns a `BackendUnavailable`
diagnostic when invoked.

### Databricks-native toolset

Registered under the `databricks` toolset and visible to Hermes
just like any other tool:

| Tool | Guardrails |
|---|---|
| `databricks_serving_endpoint_status` | Read-only |
| `databricks_uc_describe_table` | `HERMES_DATABRICKS_QUERY_ALLOWED_TABLES` allowlist |
| `databricks_uc_query_readonly` | SQL guard: SELECT/WITH only, no stacked queries, row_limit ≤ 5000, warehouse_id required |
| `databricks_volume_read` | `HERMES_DATABRICKS_VOLUME_READ_PREFIXES` prefix gate, byte-cap |
| `databricks_volume_write_agent_note` | Restricted to `<artifacts_volume>/<agent-notes>/` |
| `databricks_jobs_list` | Filtered by App SP visibility |
| `databricks_jobs_run_allowlist` | `HERMES_DATABRICKS_JOB_ID_ALLOWLIST` allowlist |
| `databricks_terminal` | Same backend as Hermes' own `terminal` tool |

---

## 10. Known limitations

| Limitation | Reason |
|---|---|
| Telegram webhooks unsupported | Apps reject anonymous inbound; we long-poll instead |
| Browser tools off by default | No Chromium in the App base image; configure an external backend (Browserbase, Databricks Job, etc.) to enable |
| `computer-use` blocked | macOS-only `cua-driver` |
| Local SQLite WAL ignored | Lakebase is the source of truth for sessions/messages |
| `/Volumes` not POSIX-mounted | All UC Volume I/O goes through the SDK Files API; Hermes paths are mirrored under `/tmp/hermes_cache/hermes_home` |
| Lakebase credentials expire | We mint a fresh credential every ≤50 minutes and reconnect on OperationalError |
| Heavy / long-running tools | Use `terminal_backend=databricks_job` (wire up a job in `resources/jobs.yml`) or `external_sandbox` |

---

## 11. Troubleshooting

| Symptom | First place to look |
|---|---|
| `/health` 200 but `/ready` 503 | `GET /debug/health-checks` to identify the failing probe |
| Model turn fails | `GET /debug/runtime` → check `errors`; ensure the App SP `CAN_USE` the serving endpoint |
| Lakebase errors at boot | Verify the setup job ran; check `agent_events` for `lakebase_ensure_schema_warning` |
| Skills missing after restart | `GET /debug/fs` to confirm `last_sync_to_volume` is recent; check `files_uploaded_running` |
| Telegram bot silent | `GET /debug/telegram` → check `poll_iterations` increases and `bot_username` populated |
| Tool registry empty | `GET /debug/tools` → the `registry.available` flag tells you if Hermes itself loaded |
| SQL query rejected | Check the `error` payload — the read-only guard explains which token tripped it |
| Cron jobs not running | `GET /debug/cron` → `tick_count` should grow every 60s; manually run via `POST /debug/cron/run/{id}` |

---

## 12. Extending

| You want to… | Touch |
|---|---|
| Use a different Databricks model | `databricks bundle deploy -t free --var llm_endpoint=…` |
| Add a custom Hermes tool | Drop a file under `vendor/hermes-agent/tools/`, call `registry.register(...)` at module top-level — Hermes auto-discovers it on next boot |
| Add a Databricks-native tool | Edit `app/hermes_databricks/tools/databricks_toolset.py`; add a `_ToolSpec`. The bundle re-deploy picks it up |
| Allowlist a new UC table for the agent | `--var` doesn't reach env yet; add to `app/app.yaml`'s `HERMES_DATABRICKS_QUERY_ALLOWED_TABLES` |
| Enable Browserbase | Add `browserbase_api_key` to the secrets scope and set `HERMES_DATABRICKS_BROWSER_BACKEND=external_browser` |
| Add a new chat channel | Mirror `telegram_polling.py` (outbound poll, allowlist, dispatch via `runtime.run_turn`); register its `health_probe` with the supervisor |
| Persist a new event kind | Call `supervisor._record_event(kind, payload)` from wherever the event happens — `/debug/events?kind=` will pick it up |

---

## 13. Credits

- Hermes Agent runtime: <https://github.com/nousresearch/hermes-agent>
- Living-AI substrate (Databricks App + Lakebase + UC Volume + Telegram patterns): <https://github.com/vbalasu/living-ai>
- This integration is independent — it does not vendor or modify
  Hermes core unless explicitly noted in the file header. The
  Databricks adapters live entirely under
  `app/hermes_databricks/`.
