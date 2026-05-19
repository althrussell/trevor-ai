# Quickstart — Hermes on Databricks Free Edition

Get a real Hermes agent running on Databricks Free Edition, reachable
from your phone via Telegram, in about ten minutes.

> If you only want to read about the project, start with
> [README.md](README.md). If you want to *run* it, you're in the
> right place.

---

## What you'll end up with

* A Databricks App (`hermes-agent`) running 24/7 on Free Edition.
* A real `hermes-agent==0.14.0` `AIAgent` inside it, backed by:
  * a Databricks-hosted open-weights model (default `databricks-gpt-oss-120b`),
  * a Lakebase Postgres instance for session + message + tool-call history,
  * a Unity Catalog Volume mirroring `HERMES_HOME`.
* A Telegram bot you can DM to talk to the agent.
* A workspace-OAuth-gated FastAPI surface for diagnostics
  (`/health`, `/ready`, `/debug/*`, `/hermes/status`, `/debug/model-turn`).

Total deploy time: ~10 minutes once prerequisites are installed.

---

## 0. Prerequisites

| Tool | Why | How |
|---|---|---|
| **Databricks workspace** | Hosts the App + Lakebase + UC | Sign up at <https://signup.databricks.com/?dbr=true> (Free Edition is fine) |
| **Databricks CLI ≥ 0.245** | Asset Bundle + secrets + auth | `brew install databricks/tap/databricks` or `pipx install databricks-cli` |
| **Python 3.11+** | Local dev + offline tests | `python3 --version` |
| **Telegram account** | Channel | <https://telegram.org> |
| **`@BotFather`** | Mint the bot token | <https://t.me/BotFather> |
| **(optional) SQL warehouse** | Lets the `databricks_uc_query_readonly` tool actually execute SQL | Workspace → SQL Warehouses → Start a serverless one |

Verify:

```bash
databricks --version
python3 --version
```

---

## 1. Authenticate the CLI

Use OAuth (recommended) — it doesn't expire and it's safer than a PAT.

```bash
databricks auth login \
  --host https://<your-workspace>.cloud.databricks.com \
  --profile hermes-free
```

A browser tab will pop open. Approve, return to the terminal, and
verify:

```bash
databricks --profile hermes-free current-user me
```

You should see your email. If you prefer a PAT for ephemeral testing:

```bash
cat >> ~/.databrickscfg <<'EOF'

[hermes-free]
host  = https://<your-workspace>.cloud.databricks.com
token = <PAT-from-User-Settings>
EOF
```

> **PAT hygiene.** PATs are bearer tokens. Rotate them frequently and
> never paste them into chat or commits. The bundle and App don't
> need a PAT in normal operation — they use the workspace App SP.

---

## 2. Clone and inspect

```bash
git clone https://github.com/althrussell/trevor-ai.git
cd trevor-ai
```

Skim:

* `databricks.yml` — bundle entrypoint, target `free`
* `resources/` — what the bundle will create
* `app/app.yaml` — runtime env vars (catalog, schema, endpoint, secrets scope)

The defaults are tuned for Free Edition. The only thing you *might*
want to change up front is the `llm_endpoint` if your workspace
doesn't expose `databricks-gpt-oss-120b` — list what's
available with:

```bash
databricks --profile hermes-free serving-endpoints list \
  | jq -r '.[] | .name' | grep -i databricks
```

Pick another endpoint (e.g. `databricks-claude-sonnet-4`,
`databricks-meta-llama-3-3-70b-instruct`) and override at deploy time
with `--var llm_endpoint=<name>` in step 5.

---

## 3. Validate the bundle

```bash
databricks bundle validate -t free --profile hermes-free
```

Expected: `Validation OK!` and a summary of resources. If you see a
secret-resource warning that's fine — we create the scope in step 6.

---

## 4. (Free Edition only) Reuse an existing Lakebase instance

Free Edition allows **one** Lakebase Postgres instance per workspace.
If you already have one (e.g. created during onboarding), bind to it
instead of letting the bundle create a new one.

List your instances:

```bash
databricks --profile hermes-free database list-database-instances \
  | jq -r '.database_instances[] | .name'
```

Set the variable when you deploy in the next step:

```bash
--var lakebase_instance=<existing-instance-name>
```

If you have no instance yet and want the bundle to create one, leave
the default; on a paid workspace this just works. On Free Edition
the bundle is preconfigured to **not** create one for the `free`
target (see `resources/lakebase.yml`).

---

## 5. Deploy

```bash
databricks bundle deploy -t free --profile hermes-free \
  --var lakebase_instance=<existing-instance-name>
```

(Drop `--var` if you don't have an existing instance and your workspace
allows new ones.)

This provisions: the UC schema, two UC volumes, the Asset Bundle
files in `~/.bundle/`, the setup job, the Databricks App
(`hermes-agent`), and all least-privilege grants.

Wait for `Deployment complete!`.

---

## 6. Create the secrets scope

The bundle wires three Telegram secrets into the App via resource
bindings. The scope must exist before the App starts.

```bash
databricks --profile hermes-free secrets create-scope hermes_agent
```

Now mint a Telegram bot. In Telegram:

1. DM `@BotFather`.
2. Send `/newbot`.
3. Pick a display name (e.g. `Hermes on Databricks`).
4. Pick a username ending in `bot` (e.g. `my_hermes_dbx_bot`).
5. Copy the API token it gives you. The format is a numeric bot ID, a
   colon, and a long random string — e.g.
   `1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA`.
   **Treat it as a password.** Anyone who has it can post as your bot.

Find your own Telegram `@handle` (Settings → Edit Profile → Username),
without the `@`. We'll call it `<your_handle>` below.

Put the secrets:

```bash
databricks --profile hermes-free secrets put-secret \
  hermes_agent telegram_bot_token \
  --string-value '<paste-bot-token-here>'

databricks --profile hermes-free secrets put-secret \
  hermes_agent telegram_primary_user_handle \
  --string-value '<your_handle>'

databricks --profile hermes-free secrets put-secret \
  hermes_agent telegram_allowed_users \
  --string-value '<your_handle>'
```

> The allowlist is enforced **server-side** by the long-poller before
> any message reaches the Hermes runtime. Add comma-separated
> additional handles as needed.

---

## 7. Seed Lakebase

The setup job applies the DDL (`hermes_session` schema, `sessions`,
`messages`, `tool_calls`, …), creates the App SP's Postgres role
(via the Databricks REST API, not `databricks_create_role()` —
which Free Edition doesn't have), and grants least-privilege on the
schema.

```bash
databricks bundle run setup_lakebase -t free --profile hermes-free
```

Expected output ends with something like:

```
DDL applied: 16, skipped: 1
GRANTed least-privilege on schema "hermes_session" to "<sp-client-id>"
```

`skipped: 1` is fine — that's the optional `pg_trgm` GIN index that
Free Edition Lakebase doesn't support.

---

## 8. Start the App

```bash
databricks bundle run hermes_app -t free --profile hermes-free
```

The CLI prints the App URL when it's ready. Open it in a browser —
you'll get a workspace-OAuth prompt, then a JSON banner:

```json
{
  "service": "hermes-agent",
  "version": "0.1.0",
  "hermes_home": "/tmp/hermes_cache/hermes_home",
  "endpoints": ["/health", "/ready", "/config", "/hermes/status", "/debug/*"]
}
```

---

## 9. Verify

From your terminal:

```bash
APP_URL=$(databricks --profile hermes-free apps get hermes-agent | jq -r '.url')

# liveness
curl -sf $APP_URL/health

# aggregated readiness (all subsystems)
curl -sf $APP_URL/ready | jq

# Hermes runtime status
curl -sf $APP_URL/hermes/status | jq

# tool registry + backend matrix
curl -sf $APP_URL/debug/tools | jq '.registry, .databricks_native, .backend_matrix'

# telegram poller status
curl -sf $APP_URL/debug/telegram | jq

# one synthetic turn (no Telegram round-trip)
curl -sf $APP_URL/debug/model-turn \
  -H 'content-type: application/json' \
  -d '{"message":"hello hermes, say a one-line haiku about Databricks Free Edition"}' \
  | jq .text
```

`scripts/smoke_test.sh` is a one-shot wrapper that hits every endpoint
and prints the JSON responses:

```bash
APP_URL=$APP_URL PROFILE=hermes-free scripts/smoke_test.sh
```

---

## 10. Test from Telegram

Open Telegram, search your bot by the username you gave `@BotFather`
(e.g. `@my_hermes_dbx_bot`), tap **Start**, and send a message:

```
hello hermes
```

You should get a reply within a few seconds. If not, see [Troubleshooting](#troubleshooting) below.

---

## Best practices

### Identity & access

* Use **OAuth profiles** (`databricks auth login`), not PATs, for
  anything beyond ephemeral testing.
* Treat the **App's Service Principal** as the only identity that
  needs `CAN_USE` on the serving endpoint and the UC volumes. Don't
  grant your user-account credentials to the App.
* Keep the Telegram allowlist **tight**. Only put handles you
  recognise into `telegram_allowed_users`. The server-side allowlist
  is the only thing between a bot in `@BotFather`'s public directory
  and your agent.

### Secrets

* Always go through `databricks secrets put-secret`. Never paste
  tokens into `app.yaml` or `databricks.yml`.
* When you rotate a token, just re-`put-secret` the same key — the
  App reads through `dbutils.secrets.get` on every boot. Restart
  the App (`databricks apps restart hermes-agent`) to pick it up
  immediately.
* The `RedactingFormatter` in `observability/logging.py` strips
  bearer tokens, Telegram bot tokens, and Postgres URIs out of
  log lines — but don't *rely* on that; don't log secrets in the
  first place.

### State & data

* All durable state belongs in **Lakebase** or **UC Volumes**.
  Anything you write to local disk is wiped when the App restarts.
* Watch `agent_events` and `usage_ledger` in Lakebase — they're a
  ready-made trace and cost ledger.
* The `hermes_home` volume holds skills, config, cron jobs, and the
  trajectory store. Treat it as the system-of-record for the
  agent's "personality"; `hermes_artifacts` is for outputs.

### Cost & quota

* Foundation Model serving on Free Edition has a generous but finite
  quota. The `daily_token_cap` bundle variable (`100000` for the
  `free` target) is a *soft* cap enforced by Hermes itself; raise
  it on paid targets.
* Long-running browser / terminal tools can rack up CPU on the App
  container. The default `terminal` backend is in-process with a
  60-second timeout; switch to `terminal_backend=databricks_job` for
  anything heavy.
* Lakebase scales to zero when idle. Keep `heartbeat_seconds` at
  ≥120s so you don't keep waking it.

### Releases

* Iterate on `dev`/`prod` targets, not `free`. The `free` target is
  the bare-minimum Free-Edition shape; `dev` and `prod` add
  warehouse, job, and observability resources.
* Treat `databricks bundle validate` as a pre-commit hook.
* Tag releases with semver and update [`CHANGELOG.md`](CHANGELOG.md).
* If you fork, change `app_name` to avoid colliding with the
  upstream `hermes-agent` App.

### Observability

* `/debug/supervisor` should show **3+ tasks**: `telegram`,
  `heartbeat`, `cron`. If any are `done: true` with a non-empty
  `exception`, that subsystem crashed silently.
* `/debug/health-checks` is the canonical "what's broken right
  now". Pin it as a browser tab when iterating.
* `/debug/events?kind=…` gives you a per-event-type tail. Useful
  kinds: `tool_call`, `tool_result`, `lakebase_session_db_*`,
  `telegram_*`, `cron_*`.
* `/debug/usage` is the rolling token / cost ledger.

---

## Troubleshooting

| Symptom | What to check |
|---|---|
| `databricks bundle deploy` says `LakebaseLimitReached` | You hit Free Edition's single-instance limit. Pass `--var lakebase_instance=<existing-instance>` (step 4). |
| `setup_lakebase` fails with `permission denied for schema hermes_session` | The App SP role didn't get created. Re-run the job; it now creates the SP role via the Databricks REST API before granting. |
| `/ready` returns 503 with `hermes_runtime: false` | `GET /debug/runtime` and look at `errors`. Usually a model endpoint mismatch — list endpoints with `databricks serving-endpoints list` and set `--var llm_endpoint=`. |
| Telegram bot silent | `GET /debug/telegram` — `telegram_loaded` should be `true`, `bot_username` populated, your handle in `allowed_usernames`. If not, re-check the secret values. |
| `Bad request: json: unknown field "stream_options"` in logs | Should not happen — the Databricks provider sets `_disable_streaming = True`. If you see it, your `app/requirements.txt` is shipping a different Hermes version that re-enables streaming. |
| `Invalid JSON schema - integer types do not support minimum` | Should not happen — `_install_request_sanitiser` strips those. Confirm `app/hermes_databricks/databricks_provider.py` is unchanged. |
| `Connection error` after the first turn | The bearer-token refresh task isn't running. `GET /debug/supervisor` and confirm the `heartbeat` task is alive. |
| `databricks bundle run hermes_app` exits immediately | The App is now compute-attached; the CLI returns once compute is reserved. Open the App URL printed in the output. |

---

## Tear it down

```bash
PROFILE=hermes-free scripts/destroy.sh
```

This deletes the App, the bundle resources, and (if the bundle owns
it) the Lakebase instance. Your Telegram bot keeps existing in
`@BotFather` — delete it there if you don't want it.

---

## Next steps

* Add a custom tool — see [`docs/PORTING_PLAN.md`](docs/PORTING_PLAN.md)
  for where to drop it.
* Enable the SQL warehouse-backed read-only query tool — set
  `--var warehouse_id=<id>`.
* Promote to a paid workspace — same commands, swap `-t free` for
  `-t prod`.
* Wire a second channel — clone `app/hermes_databricks/telegram_polling.py`
  and register it with the supervisor.

If you ship something cool, open a PR or an issue. See
[`CONTRIBUTING.md`](CONTRIBUTING.md).
