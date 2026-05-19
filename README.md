# Hermes on Databricks

<p align="left">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue.svg">
  <img alt="Databricks Free Edition" src="https://img.shields.io/badge/Databricks-Free%20Edition-orange.svg">
  <img alt="Hermes Agent 0.14.0" src="https://img.shields.io/badge/hermes--agent-0.14.0-purple.svg">
  <a href="https://github.com/althrussell/trevor-ai/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/althrussell/trevor-ai/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Status: working POC" src="https://img.shields.io/badge/status-working%20POC-brightgreen.svg">
</p>

> **Run the real [NousResearch Hermes](https://github.com/nousresearch/hermes-agent)
> agent — its full agent loop, tool registry, memory model, skills, and
> cron scheduler — inside a Databricks App on Databricks Free Edition,
> talking to a Databricks-hosted open-source model.**

This is *not* a stripped-down "Hermes-on-Databricks demo". The Hermes
runtime is imported unmodified from PyPI (`hermes-agent==0.14.0`) and
wired to Databricks-native compatibility layers underneath:

* **LLM** — [Databricks Foundation Model serving](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis) (default `databricks-gpt-oss-120b`) via the OpenAI-compatible client.
* **State** — [Lakebase Postgres](https://docs.databricks.com/aws/en/oltp/) replaces Hermes' local SQLite `SessionDB`.
* **Filesystem** — [Unity Catalog Volumes](https://docs.databricks.com/aws/en/volumes/) replace `~/.hermes` with a durable mirror.
* **Secrets** — [Databricks Secrets](https://docs.databricks.com/aws/en/security/secrets/) hold Telegram tokens and optional provider keys.
* **Deployment** — [Databricks Asset Bundles](https://docs.databricks.com/aws/en/dev-tools/bundles/).
* **Channel** — Telegram via outbound long-polling (Apps reject anonymous inbound).

> **Want to deploy in 10 minutes?** Jump to **[QUICKSTART.md](QUICKSTART.md)**.

---

## Who is Trevor?

<p align="center">
  <img src="docs/images/trevor-hero.png" alt="Trevor — the Hermes-powered AI agent running on Databricks" width="820">
</p>

**Trevor is the face of this project — the agent you actually talk to.**

Under the hood, Trevor is a thin persona wrapped around the real
[NousResearch Hermes](https://github.com/nousresearch/hermes-agent) runtime.
He carries the **Hermes brain** — the messenger-god's winged helmet and
caduceus made literal — because everything that makes him useful comes
from Hermes itself: the tool-calling agent loop, the memory model, the
skills registry, the cron scheduler, the provider abstraction.

What this repo gives Trevor is a *body* he can live in on Databricks
Free Edition:

* **Heart** — a Databricks-hosted Foundation Model
  (`databricks-gpt-oss-120b` by default) drives every turn.
* **Memory** — Lakebase Postgres replaces Hermes' local SQLite
  `SessionDB`, so Trevor remembers conversations across container
  restarts.
* **Home** — a Unity Catalog Volume mirrors `~/.hermes`, so skills,
  configs, and cron jobs survive redeploys.
* **Voice** — Telegram (outbound long-poll) lets you reach Trevor from
  your phone; a FastAPI surface handles diagnostics in the workspace.
* **Senses** — a Databricks-native tool backend lets him query Unity
  Catalog, inspect serving endpoints, read volumes, and run
  allowlisted jobs.

The name is deliberate. Calling the agent "Hermes" all the way through
would conflate the runtime (a library) with the deployed agent (a
character with state, memory, and a Telegram handle). Trevor *is* the
deployed agent; Hermes is the engine in his head.

---

## Table of contents

1. [Why this project](#1-why-this-project)
2. [Why Databricks Free Edition](#2-why-databricks-free-edition)
3. [Why Hermes Agent](#3-why-hermes-agent)
4. [Architecture](#4-architecture)
5. [Repository layout](#5-repository-layout)
6. [Quickstart](#6-quickstart)
7. [Configuration reference](#7-configuration-reference)
8. [Endpoint reference](#8-endpoint-reference)
9. [Tool backend matrix](#9-tool-backend-matrix)
10. [Validation, tests, and CI](#10-validation-tests-and-ci)
11. [Known limitations](#11-known-limitations)
12. [Troubleshooting](#12-troubleshooting)
13. [Extending](#13-extending)
14. [Contributing](#14-contributing)
15. [Security](#15-security)
16. [License & credits](#16-license--credits)

---

## 1. Why this project

Most "agent on Databricks" examples fall into one of two camps:

* **Notebook demos** — a `for`-loop calling a chat model from a
  notebook. No tool registry, no persistent state, no real agent loop,
  no production surface.
* **Custom mini-runtimes** — somebody's home-grown agent harness, written
  from scratch and missing all the hard things (context compression,
  retries, provider fallback, structured tool calling, memory).

This project goes the other way. It takes the **complete production
Hermes runtime** — the same one used in the Hermes desktop CLI — and
embeds it inside a long-running Databricks App with durable, governed
state.

The result is a single repository that proves you can host a serious
open-source agent runtime on Databricks Free Edition, end-to-end, with:

* a workspace-OAuth-gated FastAPI surface for diagnostics,
* Hermes' real `AIAgent.run_conversation` loop driving every turn,
* every message and tool call written to Lakebase Postgres,
* every skill, config edit, and cron job mirrored to a UC Volume,
* a Telegram channel reachable from your phone,
* token, cost, and event ledgers ready for downstream BI,
* and a Databricks-native toolset that lets the agent query UC,
  inspect serving endpoints, read volumes, and run allowlisted jobs.

If that sounds like a substrate you could swap a different agent
runtime onto — yes, that's intentional. The compatibility layers
(`LakebaseSessionDB`, `UCVolumeHome`, `DatabricksOpenAIClientFactory`,
the tool-backend strategy) are reusable and have no Hermes-specific
imports outside their adapter modules.

---

## 2. Why Databricks Free Edition

Databricks Free Edition gives anyone a real workspace with:

* Foundation Model serving (no credit card, no API keys)
* Unity Catalog + Volumes (governed storage)
* Lakebase Postgres (managed OLTP)
* Databricks Apps (containerised Python web apps with workspace OAuth)
* Asset Bundles (declarative deploys + secrets)
* SQL Warehouses (serverless, scale-to-zero)

For an agent POC that's a complete platform: compute, model, storage,
authn, deploy, and a UI front door — all under one identity, no
external accounts to wire together. The constraint that makes it
interesting is also what makes it real: a single Lakebase instance,
ephemeral local disk, mandatory OAuth on every inbound request, and a
container that gets restarted. If your agent runs here, it'll run
anywhere on Databricks.

The whole thing fits on the Free tier:

| Resource | Free-tier footprint |
|---|---|
| 1 Databricks App (`hermes-agent`) | included |
| 1 Lakebase instance (`CU_1`, ~$0/month idle) | included |
| 2 UC Volumes (`hermes_home`, `hermes_artifacts`) | included |
| 1 Serverless SQL Warehouse (used by the read-only query tool) | scale-to-zero |
| Foundation Model API quota | included |

There is no Free-tier-only code — promote to a paid workspace by
flipping `-t free` to `-t prod` in any `databricks bundle` command.

---

## 3. Why Hermes Agent

Hermes is one of the more interesting OSS agent runtimes available
in 2026:

* **Real agent loop.** `run_conversation` is a proper tool-calling
  iteration loop with retries, fallback providers, context
  compression, parallel-safe tool dispatch, and structured
  termination.
* **Rich tool registry.** Out of the box: file I/O, web search,
  browser, terminal, memory, skills, sessions, cron, planner,
  delegation, MCP, Telegram, Slack, Discord, Matrix, voice,
  computer-use, plus a `skill_manage` mechanism for runtime tool
  generation.
* **Memory + skills.** Persistent memory injected into every turn;
  skills are versioned, patchable, and live under `HERMES_HOME`.
* **Provider abstraction.** Talks OpenAI / Anthropic / Bedrock /
  Gemini / OpenRouter / Copilot ACP / local-vLLM through a
  pluggable provider registry — we just slot in a `databricks`
  provider profile.
* **Cron scheduler.** Built-in cron tick that runs prompts on a
  schedule, with job state owned by `HERMES_HOME/cron`.
* **Trajectory + telemetry.** Saves trajectories for replay,
  evaluation, and trace analysis.

Re-implementing any one of these is a multi-week project. Hermes gives
us all of them for the cost of a `pip install`. The win of this repo
is that we get to keep them.

---

## 4. Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Telegram (outbound long-poll, allowlist-gated)                     │
└──────────────┬──────────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Databricks App: hermes-agent     ← workspace OAuth gate            │
│  FastAPI / uvicorn / asyncio                                        │
│                                                                     │
│  HermesSupervisor                                                   │
│   ├── HermesRuntime                                                 │
│   │    ├── AIAgent (real Hermes 0.14.0)                             │
│   │    │    ├── tool_registry  →  databricks, terminal, web, file,  │
│   │    │    │                     memory, skills, cron, …           │
│   │    │    ├── run_conversation                                    │
│   │    │    └── _disable_streaming = True                           │
│   │    ├── DatabricksOpenAIClientFactory                            │
│   │    │    └── WorkspaceClient().serving_endpoints                 │
│   │    │            .get_open_ai_client()                           │
│   │    │            + class-level Completions.create patch          │
│   │    │              (strips stream_options + integer-schema       │
│   │    │               constraints Databricks proxy rejects)        │
│   │    ├── LakebaseSessionDB                                        │
│   │    │    → Postgres: sessions, messages, tool_calls,             │
│   │    │      tool_results, memory_events, usage_ledger,            │
│   │    │      agent_events, cron_jobs, kv_state                     │
│   │    └── UCVolumeHome                                             │
│   │         → /Volumes/<catalog>/<schema>/hermes_home               │
│   │           (mirrored against /tmp/hermes_cache/hermes_home)      │
│   ├── TelegramClient (httpx async long-poll, allowlist)             │
│   ├── Heartbeat task   ──▶ UC Volume sync + bearer-token refresh    │
│   └── Cron tick task   ──▶ Hermes scheduler.tick()                  │
└─────────────────────────────────────────────────────────────────────┘
```

In-depth: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
[`docs/RUNTIME_CONSTRAINTS.md`](docs/RUNTIME_CONSTRAINTS.md),
[`docs/PORTING_PLAN.md`](docs/PORTING_PLAN.md),
[`docs/TOOL_BACKEND_MATRIX.md`](docs/TOOL_BACKEND_MATRIX.md).

---

## 5. Repository layout

```
.
├── README.md                          ← you are here
├── QUICKSTART.md                      ← 10-minute deploy guide
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── SECURITY.md
├── CHANGELOG.md
├── LICENSE
├── databricks.yml                     ← Asset Bundle entrypoint
├── resources/
│   ├── schema.yml                       UC schema
│   ├── volumes.yml                      hermes_home + artifacts volumes
│   ├── lakebase.yml                     Postgres instance (optional / shared)
│   ├── setup_job.yml                    one-shot DDL + GRANTs
│   ├── app.yml                          App + resource bindings (volumes, endpoint, secrets)
│   ├── permissions.yml                  least-privilege grants
│   └── jobs.yml                         optional heavy-tool jobs
├── app/
│   ├── app.py                           FastAPI lifespan + endpoints
│   ├── app.yaml                         App runtime: uvicorn + env vars
│   ├── requirements.txt                 hermes-agent + databricks-sdk + …
│   └── hermes_databricks/
│       ├── config.py                    env → typed Config
│       ├── runtime.py                   embeds Hermes AIAgent
│       ├── supervisor.py                async lifecycle
│       ├── databricks_provider.py       OpenAI-compat client + sanitiser
│       ├── telegram_polling.py          outbound long-poll + allowlist
│       ├── health.py                    HealthRegistry / probes
│       ├── lakebase.py                  single-conn Postgres wrapper
│       ├── state/
│       │   ├── lakebase_session_db.py     Hermes SessionDB duck-type
│       │   └── schema.sql                 Lakebase DDL
│       ├── fs/
│       │   ├── volume_fs.py               UC Volume <-> local cache mirror
│       │   └── cache_sync.py
│       ├── tools/
│       │   ├── databricks_toolset.py      databricks_* tools
│       │   ├── sql_guard.py               SELECT/WITH validator
│       │   ├── terminal_backend.py        in_app_subprocess + alts
│       │   ├── browser_backend.py         diagnostic-only by default
│       │   ├── mcp_backend.py             diagnostic-only by default
│       │   └── backend_registry.py        toolset selection
│       └── observability/
│           ├── logging.py                 JSON + secret redaction
│           └── tracing.py                 optional MLflow spans
├── sql/
│   └── setup_lakebase.py                  notebook executed by setup_job
├── scripts/
│   ├── deploy.sh                          end-to-end deploy helper
│   ├── destroy.sh                         tear down everything
│   ├── smoke_test.sh                      hit every endpoint
│   └── local_dev.sh                       local venv bootstrap
├── tests/
│   ├── conftest.py
│   ├── unit/                              pytest unit suite
│   └── run_offline_checks.py              stdlib-only fallback runner
├── docs/
│   ├── ARCHITECTURE.md
│   ├── PORTING_PLAN.md
│   ├── RUNTIME_CONSTRAINTS.md
│   └── TOOL_BACKEND_MATRIX.md
├── pyproject.toml                       ruff + pytest config
├── .editorconfig
├── .gitignore
└── .github/
    ├── workflows/ci.yml                 lint + offline checks + bundle validate
    ├── ISSUE_TEMPLATE/{bug_report,feature_request,config}.yml
    ├── PULL_REQUEST_TEMPLATE.md
    ├── CODEOWNERS
    └── dependabot.yml
```

---

## 6. Quickstart

**[→ QUICKSTART.md](QUICKSTART.md)** has the full 10-minute path: install
the CLI, deploy the bundle, seed Lakebase, create the Telegram bot, send
your first message.

TL;DR:

```bash
# 1. auth
databricks auth login --host <workspace-url> --profile hermes-free

# 2. deploy
databricks bundle deploy -t free --profile hermes-free

# 3. seed Lakebase schema + grants
databricks bundle run setup_lakebase -t free --profile hermes-free

# 4. put Telegram secrets
databricks --profile hermes-free secrets create-scope hermes_agent
databricks --profile hermes-free secrets put-secret hermes_agent telegram_bot_token            --string-value '<BotFather token>'
databricks --profile hermes-free secrets put-secret hermes_agent telegram_primary_user_handle  --string-value '<your_handle>'
databricks --profile hermes-free secrets put-secret hermes_agent telegram_allowed_users        --string-value '<your_handle>'

# 5. start the App
databricks bundle run hermes_app -t free --profile hermes-free
```

Then DM your bot from Telegram.

---

## 7. Configuration reference

### Bundle variables

Override at deploy time with `--var key=value`:

| Variable | Default | Purpose |
|---|---|---|
| `catalog` | `workspace` | UC catalog that owns the schema/volumes |
| `schema` | `hermes_agent` | UC schema |
| `app_name` | `hermes-agent` | App slug |
| `agent_name` | `Hermes` | Display name |
| `llm_endpoint` | `databricks-gpt-oss-120b` | Foundation Model endpoint |
| `secrets_scope` | `hermes_agent` | Secrets scope |
| `lakebase_instance` | `hermes-db` | Lakebase Postgres instance |
| `hermes_home_volume` | `hermes_home` | UC volume for `HERMES_HOME` |
| `artifacts_volume` | `hermes_artifacts` | UC volume for agent outputs |
| `warehouse_id` | `""` | SQL warehouse for `databricks_uc_query_readonly` |
| `daily_token_cap` | `100000` / `1000000` | Soft cap |
| `heartbeat_seconds` | `180` | Supervisor heartbeat cadence |
| `terminal_backend` | `in_app_subprocess` | `in_app_subprocess`/`databricks_job`/`external_sandbox`/`disabled` |
| `browser_backend` | `disabled` | `disabled`/`in_app_playwright`/`databricks_job_browser`/`external_browser` |
| `mcp_enabled` | `false` | Hermes MCP toolset |
| `telegram_enabled` | `true` | Start the Telegram poller |

### App env vars (`app/app.yaml`)

All begin with `HERMES_DATABRICKS_` and derive from the bundle variables. Edit
`app/app.yaml` directly to flip a flag without re-running the bundle.

### Secrets (`databricks secrets put-secret`)

| Key | Required? | Used by |
|---|---|---|
| `telegram_bot_token` | yes (if telegram_enabled) | `TelegramClient` |
| `telegram_primary_user_handle` | yes | allowlist |
| `telegram_allowed_users` | optional | CSV of additional handles |
| `openai_api_key` / `anthropic_api_key` | optional | Hermes provider fallbacks |
| `exa_api_key` / `firecrawl_api_key` / `tavily_api_key` | optional | Hermes web search |
| `slack_bot_token` | optional | future Slack channel |

`hermes_databricks.observability.logging.RedactingFormatter` strips bearer
tokens, Telegram bot tokens, Postgres URIs, and `api_key=…` query-string
fragments from every log line.

---

## 8. Endpoint reference

All endpoints sit behind the App's workspace-OAuth gate.

| Endpoint | Purpose |
|---|---|
| `GET /` | App identity + key paths |
| `GET /health` | Liveness |
| `GET /ready` | Aggregated readiness from every subsystem probe |
| `GET /config` | Effective configuration (sans secrets) |
| `GET /hermes/status` | Hermes runtime status |
| `GET /debug/runtime` | Wider runtime state |
| `GET /debug/supervisor` | Background task names + done/cancelled/exception |
| `GET /debug/health-checks` | Last status of every registered probe |
| `GET /debug/tools` | Tool registry + backend matrix + Databricks-native tool list |
| `GET /debug/fs` | UC Volume mirror status |
| `GET /debug/fs/list?path=…` | List a relative path under `HERMES_HOME` |
| `GET /debug/sessions` | Recent Lakebase sessions |
| `GET /debug/session/{id}` | Full session row + messages |
| `GET /debug/events?kind=&limit=` | Filterable `agent_events` tail |
| `GET /debug/usage` | Recent `usage_ledger` rows |
| `GET /debug/cron` | Cron jobs + tick stats |
| `POST /debug/cron/run/{job_id}` | Manually run one cron job |
| `GET /debug/telegram` | Telegram poller status |
| `POST /debug/model-turn` | Run one Hermes conversation turn |

---

## 9. Tool backend matrix

| Toolset | Default in App | Backend |
|---|---|---|
| `core` / `file` / `web` / `memory` / `skills` / `session` / `cron` / `planner` / `delegation` | enabled | Pure Python; file ops via `UCVolumeHome` |
| `databricks` (this repo) | enabled | `WorkspaceClient`, allowlist-gated |
| `terminal` | enabled (`in_app_subprocess`) | cwd under `<HERMES_HOME>/workspace`, 60s default timeout |
| `browser` | disabled | Configure `HERMES_DATABRICKS_BROWSER_BACKEND` to enable |
| `mcp` | disabled | `HERMES_DATABRICKS_MCP_ENABLED=true` + `mcp_servers:` in `HERMES_HOME/config.yaml` |
| `voice` / `computer-use` / `homeassistant` | disabled | No audio / macOS / external network |
| `messaging` (Telegram) | enabled | Outbound poll |

The full per-tool table is in
[`docs/TOOL_BACKEND_MATRIX.md`](docs/TOOL_BACKEND_MATRIX.md).
**Tools are never silently removed** — if a backend is unavailable the
tool stays registered and returns a `BackendUnavailable` diagnostic.

### Databricks-native toolset

| Tool | Guardrails |
|---|---|
| `databricks_serving_endpoint_status` | Read-only |
| `databricks_uc_describe_table` | `HERMES_DATABRICKS_QUERY_ALLOWED_TABLES` allowlist |
| `databricks_uc_query_readonly` | `SELECT`/`WITH` only; no stacked queries; `row_limit ≤ 5000`; `warehouse_id` required |
| `databricks_volume_read` | Prefix gate + byte cap |
| `databricks_volume_write_agent_note` | Restricted to `<artifacts_volume>/<agent-notes>/` |
| `databricks_jobs_list` | Filtered by App SP visibility |
| `databricks_jobs_run_allowlist` | `HERMES_DATABRICKS_JOB_ID_ALLOWLIST` allowlist |
| `databricks_terminal` | Shares the `terminal_backend` |

---

## 10. Validation, tests, and CI

| Command | What it does |
|---|---|
| `scripts/smoke_test.sh` | Hits every endpoint above on a deployed App |
| `PYTHONPATH=app python3 tests/run_offline_checks.py` | Stdlib-only logic checks (SQL guard, terminal sandbox, UC Volume path guard, redaction, toolset arg validation, Telegram allowlist) |
| `PYTHONPATH=app pytest tests/unit -v` | Full pytest unit suite |
| `databricks bundle validate -t free` | Asset Bundle static validation |

GitHub Actions [`ci.yml`](.github/workflows/ci.yml) runs lint, the
offline checks, the pytest suite, and `bundle validate` on every push
and PR.

---

## 11. Known limitations

| Limitation | Reason |
|---|---|
| Telegram webhooks unsupported | Apps reject anonymous inbound; we long-poll |
| Browser tools off by default | No Chromium in the App base image — configure an external backend |
| `computer-use` blocked | macOS-only `cua-driver` |
| `pg_trgm` index optional | Free Edition Lakebase has no `pg_trgm` extension; the trgm GIN index is best-effort |
| `/Volumes` not POSIX-mounted | All UC Volume I/O goes through the SDK Files API; Hermes paths are mirrored to `/tmp/hermes_cache/hermes_home` |
| Local SQLite WAL ignored | Lakebase is the source of truth for sessions/messages |
| Lakebase credentials expire | Refreshed every ≤50 minutes with auto-reconnect on `OperationalError` |
| Databricks proxy rejects OpenAI extras | `databricks_provider._sanitise_oai_kwargs` strips `stream_options` and integer-schema constraints |

---

## 12. Troubleshooting

| Symptom | First place to look |
|---|---|
| `/health` 200 but `/ready` 503 | `GET /debug/health-checks` — identifies the failing probe |
| Model turn fails | `GET /debug/runtime` → `errors`; ensure the App SP `CAN_USE` the serving endpoint |
| Lakebase errors at boot | Verify `setup_lakebase` ran; check `agent_events` for `lakebase_*` rows |
| Skills missing after restart | `GET /debug/fs` — `last_sync_to_volume` should be recent |
| Telegram bot silent | `GET /debug/telegram` — `poll_iterations` increases, `bot_username` populated, allowlist contains your `@handle` |
| Tool registry empty | `GET /debug/tools` — `registry.available` tells you if Hermes loaded |
| SQL query rejected | The `error` payload explains which token tripped the read-only guard |
| Cron jobs not running | `GET /debug/cron` — `tick_count` should grow every 60s |
| `Connection error` mid-conversation | Check the bearer-token refresh task in `/debug/supervisor`; should run every ~30 min |

---

## 13. Extending

| You want to… | Touch |
|---|---|
| Use a different Databricks model | `--var llm_endpoint=…` on `databricks bundle deploy` |
| Add a custom Hermes tool | Drop a file under `vendor/hermes-agent/tools/`; call `registry.register(...)` at module top-level |
| Add a Databricks-native tool | Edit `app/hermes_databricks/tools/databricks_toolset.py`; add a `_ToolSpec` |
| Allowlist a new UC table | Add to `HERMES_DATABRICKS_QUERY_ALLOWED_TABLES` in `app/app.yaml` |
| Enable an external browser | Add the relevant API key to secrets and set `HERMES_DATABRICKS_BROWSER_BACKEND=external_browser` |
| Add a new chat channel | Mirror `telegram_polling.py` (outbound poll + allowlist + dispatch via `runtime.run_turn`); register `health_probe` with the supervisor |
| Persist a new event kind | Call `supervisor._record_event(kind, payload)` — `/debug/events?kind=` will pick it up |

---

## 14. Contributing

PRs are welcome. Start with [`CONTRIBUTING.md`](CONTRIBUTING.md) for the
dev workflow, code style, and commit conventions. Be kind — see
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

---

## 15. Security

Found something exploitable? Please follow
[`SECURITY.md`](SECURITY.md) instead of filing a public issue.

Key invariants worth knowing:

* The App is gated by workspace OAuth; there are no anonymous
  endpoints.
* Secrets live in Databricks Secrets; they are read by the App SP
  via resource bindings and never persisted to local disk.
* The Lakebase App SP role is created via the
  `/api/2.0/database/instances/{name}/roles` API and granted
  least-privilege (`USAGE` + DML on `hermes_session`).
* All UC Volume I/O is bounded under `HERMES_HOME` with explicit
  guards against absolute paths and `..` escapes.
* The terminal sandbox restricts cwd to `<HERMES_HOME>/workspace`
  and enforces a command denylist + 60s default timeout.
* The read-only SQL tool rejects everything except `SELECT` / `WITH`
  and strips trailing semicolons / stacked queries.
* Logs are filtered through a redacting JSON formatter that strips
  bearer tokens, Telegram bot tokens, Postgres URIs, and
  `api_key=…` query strings.

---

## 16. License & credits

[MIT](LICENSE) © 2026 Al Thrussell. See [NOTICE.md](NOTICE.md) for the
full third-party attribution list and architectural-reference
disclosures.

### Runtime dependencies (summary)

* **Hermes Agent** — MIT (Nous Research),
  <https://github.com/NousResearch/hermes-agent>. Installed unmodified
  from PyPI (`hermes-agent==0.14.0`); no Hermes core code is vendored
  or modified here.
* **Databricks SDK** — Apache-2.0.
* **FastAPI** — MIT. **Uvicorn** — BSD-3-Clause.
* **psycopg v3** — LGPL-3.0 (dynamic-linked via pip; not modified).
  See [NOTICE.md §1](NOTICE.md#note-on-psycopgbinary-and-lgpl-30) for
  details.

### Architectural references

* **Living-AI** — <https://github.com/vbalasu/living-ai>. Consulted as
  an *architectural pattern reference* for the agent-on-Databricks-
  Free-Edition substrate shape. **No source code is vendored,
  copied, or adapted** — a line-level overlap analysis confirmed
  that all shared "substantive" lines are Databricks SDK / Telegram
  Bot API / psycopg call signatures (non-copyrightable under merger
  doctrine) or mandatory Databricks notebook idioms (scènes à
  faire). See [NOTICE.md §3](NOTICE.md#living-ai) for the full
  disclosure.
* **Databricks AI Dev Kit** — <https://github.com/databricks-solutions/ai-dev-kit>.
  Reviewed but **not used**. Distributed under a restrictive custom
  "Databricks License" that limits use to Databricks Services
  contexts; we deliberately avoid any code or content dependency on
  it. The repo's *governance file layout* (LICENSE / NOTICE /
  SECURITY / CONTRIBUTING / CODEOWNERS / dependency attribution
  table) was reviewed as a non-copyrightable governance pattern;
  our equivalents are independently authored.

If this is useful to you, ⭐ the repo and tell me what broke.
