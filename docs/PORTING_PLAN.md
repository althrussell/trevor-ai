# Porting Plan — Hermes Agent → Databricks Free Edition

This document is the engineering plan that translates Hermes' real
code surface (as it exists in `hermes-agent==0.14.0`) into a set of
isolated adapters that let it run inside a Databricks Apps container.

## Guiding principles

1. **Never fork Hermes core.** All Databricks-specific code lives in
   the `trevor_databricks` package or in plugin directories that
   Hermes already supports.
2. **Use Hermes' own extension points first.**
   * `ProviderProfile` for the model provider.
   * `AIAgent(session_db=…)` for state.
   * `HERMES_HOME` env var for the home directory.
   * Plugin hooks for memory / context / image_gen / browser if needed.
3. **Patch only at one isolated boundary** if necessary (currently:
   one line that swaps `agent.client` with the WorkspaceClient OpenAI
   client). Document the patch and cover it with tests.
4. **Keep failure modes explicit.** If a Hermes feature cannot work
   inside the App container (browser, computer-use, native MCP stdio
   binaries), return a structured `BackendUnavailable` diagnostic
   from the tool — never silently delete the tool.

## What we are NOT replacing

* Hermes' `AIAgent` constructor and `run_conversation`.
* Hermes' `model_tools.py` tool registry, `tools/registry.py`,
  `toolsets.py`, `toolset_distributions.py`.
* Hermes' provider routing in `agent/auxiliary_client.py`,
  `agent/transports/*`, `agent/agent_runtime_helpers.create_openai_client`.
* Hermes' context compressor (`agent/context_compressor.py`,
  `trajectory_compressor.py`).
* Hermes' skills loader (`agent/skill_utils.py`,
  `hermes_cli/config.ensure_trevor_home`).
* Hermes' cron scheduler (`cron/scheduler.py`, `cron/jobs.py`).
* Hermes' logging (`hermes_logging.py`) — we only extend the formatter
  with our redactor.

## What we ARE replacing

| Hermes assumption | Databricks replacement | Adapter |
|-------------------|------------------------|---------|
| SQLite `state.db` at `HERMES_HOME/state.db` | Postgres on Lakebase | `state.lakebase_session_db.LakebaseSessionDB` |
| `~/.hermes` or POSIX `HERMES_HOME` writes durable across restarts | `/tmp/trevor_cache` (ephemeral) + sync to UC Volume | `fs.volume_fs.UCVolumeHome` + `fs.cache_sync.CacheSync` |
| `.env` file with provider API keys | Databricks Secrets | `config.load_secrets` |
| External paid model providers | Databricks Model Serving via OpenAI-compatible client | `databricks_provider.DatabricksOpenAIClientFactory` + `apply_provider_to_agent` |
| Telegram webhook (or local long-poll) | App-side outbound long polling | `telegram_polling.TelegramClient` |
| Local terminal subprocess as default | `in_app_subprocess` with cwd/timeout guard; `databricks_job` opt-in | `tools.terminal_backend` |
| Local Playwright Chromium | `disabled` by default; `external_browser` opt-in | `tools.browser_backend` |
| Local stdio MCP binaries | `disabled` by default; `remote_http` opt-in | `tools.mcp_backend` |

## Phase mapping (matches the user spec)

| Phase | Deliverable | Status |
|-------|-------------|--------|
| 0 | Source inspection + docs (this file, ARCHITECTURE, RUNTIME_CONSTRAINTS, TOOL_BACKEND_MATRIX) | This change |
| 1 | Bundle + `app/app.py` + `/health` | This change |
| 2 | `pip install hermes-agent==0.14.0` + `trevor_databricks.runtime.TrevorRuntime` | This change |
| 3 | `databricks_provider` + `/debug/model-turn` | This change |
| 4 | `LakebaseSessionDB` + `schema.sql` + `sql/setup_lakebase.py` | This change |
| 5 | `UCVolumeHome` + `CacheSync` | This change |
| 6 | `TelegramClient.poll_loop` wired to Hermes | This change |
| 7 | `DatabricksToolset` + backend selectors | This change |
| 8 | Cron tick + `/debug/cron` endpoints | This change |
| 9 | `/debug/*` + structured logs + redactor + `agent_events` | This change |
| 10 | `scripts/smoke_test.sh`, README, unit/integration tests | This change |

## SessionDB compatibility surface

`run_conversation` and `agent.agent_runtime_helpers` reach the
session DB through these methods (extracted from the actual Hermes
source — see `docs/RUNTIME_CONSTRAINTS.md` §SessionDB):

* `create_session(session_id, source, model=None, model_config=None,
   system_prompt=None, parent_session_id=None, **kwargs) -> str`
* `ensure_session(session_id, source="unknown", model=None, **kwargs) -> str`
* `get_session(session_id) -> dict | None`
* `update_system_prompt(session_id, system_prompt)`
* `update_token_counts(session_id, input_tokens=0, output_tokens=0,
   model=None, cache_read_tokens=0, cache_write_tokens=0,
   reasoning_tokens=0, estimated_cost_usd=None, actual_cost_usd=None,
   cost_status=None, cost_source=None, pricing_version=None,
   billing_provider=None, billing_base_url=None, billing_mode=None,
   api_call_count=0, absolute=False)`
* `append_message(session_id, role, content=None, tool_name=None,
   tool_calls=None, tool_call_id=None, token_count=None,
   finish_reason=None, reasoning=None, reasoning_content=None,
   reasoning_details=None, codex_reasoning_items=None,
   codex_message_items=None) -> int`
* `replace_messages(session_id, messages)`
* `get_messages(session_id) -> list[dict]`
* `clear_messages(session_id)`
* `end_session(session_id, end_reason)`
* `reopen_session(session_id)`
* `search_messages(query, source_filter=None, exclude_sources=None,
   role_filter=None, limit=20, offset=0, sort=None)` — *limited*
* `resolve_session_id(prefix) -> str | None`
* `set_session_title(session_id, title) -> bool`
* `get_session_title(session_id) -> str | None`
* `get_compression_tip(session_id) -> str | None`
* `set_meta(key, value)`, `get_meta(key)`
* `vacuum()` — no-op for Postgres
* `close()` — closes the connection pool

Methods that exist on `SessionDB` but are **not** exercised by the
core loop (some Telegram topic helpers, handoff helpers, the
`prune_*` lifecycle helpers, `_fts_*` internals) are either stubbed
with safe no-ops or implemented lazily as we light up features.

## Provider integration boundary

There are three plausible integration points; we pick the **last** one
because it requires zero monkey-patching of Hermes' core logic:

1. **`ProviderProfile` only** — register `databricks` so it appears in
   `hermes model` / introspection. ✅ done.
2. **`api_key` + `base_url` to constructor** — pass the workspace URL
   and a one-shot bearer token. ❌ token rotation has to be handled
   manually, brittle in long-running Apps.
3. **Post-init client swap** — call `AIAgent(...)` with placeholder
   `api_key="not-used"` and `base_url="https://placeholder"`, then
   immediately set `agent.client = workspace_client.serving_endpoints
   .get_open_ai_client()`. The SDK's `BearerAuth` httpx auth flow
   refreshes the token on every request. ✅ done. We also patch
   `agent._client_kwargs` and `agent.base_url` so internal helpers
   that read them get the right values.

The swap happens in exactly one place
(`trevor_databricks.databricks_provider.apply_to_agent`) and is
covered by a unit test that asserts the resulting client is the
WorkspaceClient one and that `chat.completions.create` is dispatched
to it.

## HERMES_HOME strategy

Databricks Apps:

* Use a writable but **ephemeral** filesystem.
* Restart frequently (config changes, upgrades, autoscale events).
* Cannot rely on `~/.hermes` surviving across restarts.

Strategy:

1. Set `HERMES_HOME=/tmp/trevor_cache/trevor_home`.
2. On boot, list the UC Volume root and download all files into the
   cache (`UCVolumeHome.sync_from_volume`).
3. Run `hermes_cli.config.ensure_trevor_home(...)` so any missing
   subdirs (cron/, sessions/, logs/, …) are created.
4. Monkeypatch nothing — Hermes happily writes to whatever
   `HERMES_HOME` points at.
5. Background task syncs **important** subpaths back to UC Volume on
   change (skills/, cron/, memories/, image_cache/, audio_cache/,
   logs/curator/). The `sessions/` tree is informational (session
   snapshots) — durable source of truth is Lakebase.
6. On shutdown lifespan exit, best-effort full upload.

## Optional vs. required dependencies

The base `requirements.txt` installs **`hermes-agent==0.14.0`** plus
extras we need for the App path:

* core (always): `fastapi`, `uvicorn[standard]`, `databricks-sdk`,
  `psycopg[binary]`, `httpx`, `openai`, `python-dotenv`.
* `hermes-agent` itself pins `openai`, `tenacity`, `pyyaml`, `croniter`,
  `pydantic`, `jinja2`, `httpx[socks]`, etc.

We deliberately **do not** install `hermes-agent[all]`. Optional
extras like `anthropic`, `bedrock`, `voice`, `tts-premium`, `pty`,
`computer-use`, `dingtalk`, `feishu`, etc. add dependencies (boto3,
pyaudio, sounddevice, faster-whisper, ptyprocess, native binaries)
that either won't build in the App container or aren't useful in this
deployment. Toolsets that need them are documented as `requires-extra`
in `docs/TOOL_BACKEND_MATRIX.md`.

## Test plan

* **Unit** (no Databricks workspace required, all SDK calls mocked):
  * Provider: `apply_to_agent` swaps `client`, passes through
    `chat.completions.create`, handles model override.
  * Lakebase SessionDB: all method signatures present; SQL is valid
    Postgres; insert/select round-trip via a `psycopg.fake_conn`.
  * UC Volume path guard: rejects `..`, absolute escapes, mixed
    separators.
  * SQL read-only validator: rejects every dangerous statement type.
  * Telegram update parsing: handles edited_message, missing username,
    non-text messages.
  * Secret redactor: scrubs bearer tokens, postgres URIs, telegram
    tokens.
* **Integration** (mocked Databricks SDK):
  * Full model turn: `runtime.run_turn(user_message="hi")` returns
    text from a stubbed serving endpoint.
  * Session persistence: write/read same `session_id` after a fresh
    `LakebaseSessionDB` instance.
  * Cache sync: write under `skills/` → upload called.
* **Smoke** (requires a deployed workspace):
  `scripts/smoke_test.sh` validates `/health`, `/ready`, `/debug/runtime`,
  `/debug/model-turn`, and `/debug/sessions`.

## Acceptance gates per phase

| Phase | Gate |
|-------|------|
| 1 | `databricks bundle validate -t free` is clean; `app/app.py` boots locally with `uvicorn` (Hermes optional). |
| 2 | `from run_agent import AIAgent` succeeds; `runtime.status()["hermes_version"]` returns 0.14.0. |
| 3 | `runtime.run_turn("hello")` returns a non-empty completion when a workspace + serving endpoint is reachable. (Mocked in unit tests.) |
| 4 | `LakebaseSessionDB.create_session` + `append_message` + `get_messages` round-trip in Lakebase. |
| 5 | After writing `skills/foo.md`, restart App, file is still present. |
| 6 | A Telegram DM from the configured user triggers `run_conversation`, the reply is delivered, and messages appear in Lakebase. |
| 7 | At least one `databricks_*` tool returns a real result; terminal tool runs `echo hi` in a guarded cwd. |
| 8 | A scheduled job in `HERMES_HOME/cron/jobs.json` survives restart and `/debug/cron/run/{id}` triggers it. |
| 9 | `/ready` summarises five subsystem checks; an injected failure shows in `agent_events`. |
| 10 | `scripts/smoke_test.sh` passes against a real Free Edition workspace. |
