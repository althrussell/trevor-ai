# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- CI: make the lint, offline-checks, and unit-tests jobs actually
  pass on `main` and on dependabot PRs. The initial workflow shipped
  with strict `ruff format --check .` enabled but the pre-existing
  code had never been run through the formatter, so every push and
  every dependabot PR failed before the dep change was evaluated.
  Reformatted 22 files via `ruff format`, applied 392 auto-fixes via
  `ruff check --fix`, and 13 `--unsafe-fixes` modernizations
  (mostly `List`→`list`, `Tuple`→`tuple`, `startswith` tuple
  collapsing, `contextlib.suppress`, `zip(..., strict=True)`). One
  manual fix: `raise HTTPException(...) from None` in the cron
  `KeyError` handler so the trace doesn't dump a redundant inner
  exception. No runtime behaviour change — `tests/run_offline_checks.py`
  still passes 37/0.
- CI: add per-file ruff ignore for `sql/setup_lakebase.py` covering
  `F821` (`dbutils` / `spark` are notebook globals) and `E402`
  (Databricks notebooks legitimately interleave widgets and
  imports).
- CI: make the `Asset Bundle validate` job non-blocking
  (`continue-on-error: true`). The Databricks CLI's `bundle
  validate` always probes the workspace for `/scim/v2/Me`, so it
  cannot run without auth — passing a stub `DATABRICKS_HOST` fails
  DNS resolution. The job now (a) runs `databricks bundle schema`
  which is offline-capable and catches CLI/DSL regressions, then
  (b) attempts `bundle validate -t free` against the stub host and
  emits a `::warning::` if (as expected) it can't reach a
  workspace. Real validation belongs in `scripts/deploy.sh` before
  each deploy.
- CI: bump `actions/checkout@v4 → v5` and `actions/setup-python@v5 → v6`
  to drop the Node.js 20 deprecation warnings (matches the
  upgrades dependabot PRs #1 and #2 were proposing).

### Added

- `NOTICE.md` — full third-party attribution: runtime dependencies
  with licenses (hermes-agent MIT, databricks-sdk Apache-2.0, FastAPI
  MIT, Uvicorn BSD-3-Clause, psycopg LGPL-3.0 with an explicit
  dynamic-linking note), dev-time dependencies, and architectural-
  reference disclosures for Hermes Agent, Living-AI, Databricks AI
  Dev Kit, and the Anthropic Claude tooling used during development.
- License-compliance review of [databricks-solutions/ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit)
  (custom restrictive "Databricks License"): confirmed zero
  references / dependencies / vendored code from ai-dev-kit; no
  compliance obligations triggered.
- License-compliance review of [vbalasu/living-ai](https://github.com/vbalasu/living-ai)
  (no LICENSE file declared — default copyright = all rights
  reserved): performed line-level overlap analysis across 8 file
  pairs. All shared "substantive" lines are Databricks SDK /
  Telegram Bot API / psycopg call signatures (merger doctrine) or
  mandatory Databricks notebook idioms (scènes à faire); no
  copyrightable expression was copied. Findings documented in
  `NOTICE.md §3`.
- README §16 expanded to include a licensing summary and explicit
  architectural-reference disclosures pointing at `NOTICE.md`.
- LICENSE now references `NOTICE.md` and requires NOTICE.md to be
  redistributed alongside it.
- `CONTRIBUTING.md` gains a "Licensing & attribution" section
  documenting which upstream license categories are OK to pull in
  (MIT/Apache/BSD/ISC: yes; LGPL: runtime only; GPL: no; custom
  restrictive licenses like ai-dev-kit's DB License: no; no-license
  repos: patterns only, never code) plus an SPDX header convention
  for new files.

- Comprehensive `README.md` rewrite with badges, story-driven intro,
  "Why Databricks Free Edition", "Why Hermes Agent", architecture
  diagram, repo layout, configuration reference, endpoint reference,
  tool backend matrix, troubleshooting, and extension guide.
- `QUICKSTART.md` with a 10-minute zero-to-live walkthrough covering
  auth, deploy, Telegram bot creation, secrets, Lakebase seeding,
  smoke verification, and best practices.
- `CONTRIBUTING.md` with dev setup, code style, test guidance,
  PR flow, conventional-commit conventions, release process, and
  architectural ground rules.
- `CODE_OF_CONDUCT.md` adopting Contributor Covenant 2.1.
- `SECURITY.md` documenting supported versions, reporting flow,
  security model, and supply chain assumptions.
- `LICENSE` (MIT).
- `pyproject.toml` with ruff + pytest configuration and a `[dev]`
  extras group.
- `.editorconfig` for cross-editor formatting consistency.
- GitHub Actions CI workflow (`.github/workflows/ci.yml`) running
  ruff format/lint, the offline check runner, the pytest suite, and
  `databricks bundle validate -t free`.
- Issue templates (`bug_report.yml`, `feature_request.yml`,
  `config.yml`) and a pull request template under `.github/`.
- `.github/CODEOWNERS` for review routing.
- `.github/dependabot.yml` for weekly pip and GitHub Actions updates.

## [0.1.0] — 2026-05-19

First working proof-of-concept. Real `hermes-agent==0.14.0` `AIAgent`
running inside a Databricks App on Free Edition, talking to a
Databricks-hosted open-source serving endpoint, with Lakebase Postgres
session state and a UC Volume `HERMES_HOME` mirror, reachable from
Telegram.

### Added

- **Phase 0** — Porting plan, architecture document, runtime
  constraints, and tool backend matrix under `docs/`.
- **Phase 1** — Databricks Asset Bundle scaffolding (`databricks.yml`
  + `resources/*.yml`), FastAPI app skeleton (`app/app.py`,
  `app/app.yaml`), deploy/destroy/smoke/local-dev scripts under
  `scripts/`.
- **Phase 2** — Embedded Hermes `AIAgent` lifecycle
  (`hermes_databricks.runtime.HermesRuntime`), supervisor
  (`hermes_databricks.supervisor.HermesSupervisor`), typed `Config`
  loader from environment, and `HealthRegistry`.
- **Phase 3** — `DatabricksOpenAIClientFactory` providing a Databricks
  Foundation Model serving endpoint as an OpenAI-compatible client.
  Forces non-streaming (`_disable_streaming = True`) to avoid the
  Databricks proxy's rejection of `stream_options`, and patches
  `openai.resources.chat.completions.Completions.create` to strip
  integer-schema constraints (`minimum`, `maximum`,
  `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`) from tool
  definitions before they hit the proxy. Periodic bearer-token
  refresh via the supervisor heartbeat (every ≤30 minutes).
- **Phase 4** — `LakebaseSessionDB` duck-type for Hermes' `SessionDB`
  backed by Lakebase Postgres. `state/schema.sql` with `sessions`,
  `messages`, `tool_calls`, `tool_results`, `memory_events`,
  `usage_ledger`, `agent_events`, `cron_jobs`, `kv_state`. `ensure_schema`
  tolerates `permission denied` / `already exists` on `CREATE`
  statements so the App SP can come up cleanly even without DDL
  privileges. `sql/setup_lakebase.py` setup-job notebook creates the
  App SP's Postgres role via the Databricks REST API
  (`/api/2.0/database/instances/{name}/roles`) — required on Free
  Edition where `databricks_create_role()` SQL is unavailable —
  then grants least-privilege on `hermes_session`. Optional
  `pg_trgm` extension + GIN index are best-effort.
- **Phase 5** — `UCVolumeHome` mirroring `HERMES_HOME` between the
  ephemeral `/tmp/hermes_cache/hermes_home` and a UC Volume.
  Bi-directional sync via the SDK Files API. Path guards reject
  absolute paths and `..` escapes.
- **Phase 6** — Telegram outbound long-poll channel
  (`hermes_databricks.telegram_polling`) with strict server-side
  allowlist. Health probe registered with the supervisor.
- **Phase 7** — Databricks-native toolset
  (`databricks_serving_endpoint_status`, `databricks_uc_describe_table`,
  `databricks_uc_query_readonly`, `databricks_volume_read`,
  `databricks_volume_write_agent_note`, `databricks_jobs_list`,
  `databricks_jobs_run_allowlist`, `databricks_terminal`) with SQL
  guard (`sql_guard.py`), terminal sandbox (`terminal_backend.py`,
  `in_app_subprocess` strategy), browser/MCP diagnostic stubs, and
  `backend_registry.py` toolset selection. Tools are never silently
  removed — unavailable backends return a `BackendUnavailable`
  diagnostic.
- **Phase 8** — Hermes cron scheduler integrated with the supervisor's
  tick task. Cron job state mirrored under `HERMES_HOME/cron`.
  `/debug/cron` + `POST /debug/cron/run/{id}`.
- **Phase 9** — Observability: structured JSON logging with secret
  redaction (`observability/logging.py`), event persistence to
  Lakebase (`agent_events`, `usage_ledger`), lightweight `span`
  tracing context manager (`observability/tracing.py`), and the full
  `/debug/*` endpoint surface.
- **Phase 10** — `README.md` baseline, `tests/run_offline_checks.py`
  stdlib-only logic runner, and a fix to `terminal_backend._run_in_app`
  to wrap `_validate_cwd` in `try/except ValueError`.

### Compatibility notes

- Targets `hermes-agent==0.14.0` from PyPI. Does not vendor or
  modify Hermes core.
- Targets Databricks Free Edition; `dev` and `prod` bundle targets
  are provided for paid workspaces.
- Single Lakebase instance per Free Edition workspace — bundle
  binds to an existing instance via `--var lakebase_instance=` and
  does **not** attempt to create one when target is `free`.

[Unreleased]: https://github.com/althrussell/trevor-ai/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/althrussell/trevor-ai/releases/tag/v0.1.0
