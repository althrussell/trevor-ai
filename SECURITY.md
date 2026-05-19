# Security Policy

## Supported versions

This project is currently a **0.x public POC**. Security fixes will be
applied to the latest `main` and the most recent tagged `0.x` release
only.

| Version | Supported          |
|---------|--------------------|
| `main`  | yes                |
| `0.1.x` | yes                |
| `< 0.1` | no                 |

## Reporting a vulnerability

**Please do not file a public GitHub issue** for anything that could be
exploited against a deployed instance.

Send the report to **althrussell@gmail.com** with subject prefix
`[hermes-on-databricks security]`. If you want PGP, ask in the first
mail and I'll send a key.

Please include:

* Affected component (path, function, endpoint, bundle resource, etc.)
* Reproduction steps — ideally a minimal failing curl / Python snippet
* Impact assessment: what an attacker could read, write, or escalate
* Suggested fix if you have one
* Whether you want public credit and what handle to use

### What to expect

| Stage | SLO |
|---|---|
| Acknowledgement | within **72 hours** |
| Initial triage  | within **7 days** |
| Fix released or mitigation documented | as fast as practical, target **30 days** |
| Public advisory + CVE (if warranted) | after the fix lands |

Coordinated disclosure is appreciated — please give us a chance to ship
a fix before going public.

## Security model — what this project assumes

These are the invariants the codebase relies on. A break in any of them
is a security bug.

### Identity & access

* The Databricks App is gated by **workspace OAuth**. There are no
  anonymous endpoints. The platform rejects unauthenticated requests at
  the proxy layer before any code in `app/app.py` runs.
* All Databricks API calls inside the App use the **App's Service
  Principal**, never a user PAT. The SP only ever has least-privilege
  grants:
  * `CAN_USE` on the configured serving endpoint
  * `READ/WRITE` on the two named UC Volumes
  * `READ` on the named secrets scope
  * `USAGE` + DML on the `hermes_session` Lakebase schema

### Secrets

* Secrets are stored in **Databricks Secrets** and surfaced via the
  Asset Bundle's `resources` binding (e.g. `valueFrom: telegram-bot-token`).
* No secret value is ever written to local disk, the UC Volume mirror,
  or Lakebase tables.
* The redacting JSON formatter in
  `app/hermes_databricks/observability/logging.py` strips bearer
  tokens, Telegram bot tokens, Postgres URIs, and `api_key=…`
  query-string fragments from every log record before it reaches
  stderr. This is a defence in depth, not a substitute for not logging
  secrets.

### State & storage

* All UC Volume I/O goes through `UCVolumeHome._resolve_local` and
  `_resolve_volume`, which **reject absolute paths and `..` escapes**.
  The `HERMES_HOME` mirror is bounded to `/tmp/hermes_cache/hermes_home`
  on the App and the configured UC Volume path remotely.
* Lakebase access uses **per-connection short-lived credentials**
  minted from the Databricks SDK; we re-mint before the
  ~1h expiry and auto-reconnect on `OperationalError`.

### Tools

* The Hermes tool registry is loaded but **every Databricks-native
  tool is allowlist-gated** by env vars (`HERMES_DATABRICKS_*`):
  * `databricks_uc_query_readonly` — read-only SQL validator,
    `SELECT/WITH` only, no stacked queries, mandatory `warehouse_id`,
    `row_limit ≤ 5000`.
  * `databricks_volume_read` — must match
    `HERMES_DATABRICKS_VOLUME_READ_PREFIXES`, byte-capped.
  * `databricks_volume_write_agent_note` — restricted to
    `<artifacts_volume>/<agent-notes>/`.
  * `databricks_jobs_run_allowlist` — must be in
    `HERMES_DATABRICKS_JOB_ID_ALLOWLIST`.
  * `databricks_uc_describe_table` — must match
    `HERMES_DATABRICKS_QUERY_ALLOWED_TABLES`.
* The `terminal` backend (`in_app_subprocess`) restricts cwd to
  `<HERMES_HOME>/workspace`, enforces a command denylist (`rm -rf /`,
  `dd`, `mkfs`, `:(){:|:&};:` etc.), and a 60s default timeout.
* `browser`, `mcp`, `voice`, `computer-use`, `homeassistant` are
  **disabled by default**. Enabling them is a deliberate configuration
  step.

### Channels

* The Telegram channel is **outbound long-poll only**. Apps reject
  anonymous inbound webhooks, so the long-poll loop is the only
  ingress path.
* Every Telegram update passes through a **server-side allowlist**
  (`telegram_primary_user_handle` + `telegram_allowed_users`) before
  it reaches the Hermes runtime. The allowlist match is exact on
  `from.username`. Updates from users not in the allowlist are
  silently dropped and counted under `/debug/telegram` as
  `messages_dropped_not_allowed`.

### Supply chain

* Direct dependencies are pinned in `app/requirements.txt`. Indirect
  dependencies follow Hermes' own pins (we deliberately do not
  override its `openai`/`pydantic`/`httpx`/`pyyaml`/`requests`
  versions).
* Dependabot is configured for `app/requirements.txt`, `pyproject.toml`,
  and GitHub Actions in `.github/dependabot.yml`.
* CI runs `pip install` against the pinned set and exercises the
  offline test runner — any unexpected resolution change shows up
  there first.

## Out-of-scope

* Vulnerabilities in **upstream Databricks platform components**
  (Apps proxy, Foundation Model serving, Lakebase, Unity Catalog).
  Please report those to Databricks directly.
* Vulnerabilities in **`hermes-agent` core**. Please report those
  upstream at <https://github.com/nousresearch/hermes-agent>.
* Vulnerabilities only reachable by an actor who already has
  workspace admin in the target Databricks account.

## Hall of fame

Security researchers who responsibly disclose will be credited here
(with permission).
