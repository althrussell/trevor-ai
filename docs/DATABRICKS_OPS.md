# Databricks Operations

How Trevor interacts with Databricks: the global gates, the primitive
tools the agent uses, the bundled skills that orchestrate them, and the
UC + workspace privileges the App service principal needs.

## TL;DR

1. The agent uses Hermes' **skill** system to pick a Databricks playbook
   for the user's request. Skills are markdown files vendored from
   [databricks-solutions/ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit)'s
   `databricks-skills/` subtree (27 SKILL.md, seeded into
   `HERMES_HOME/skills/databricks/` on first boot).
2. Skills call a small set of **primitives**: `databricks_sql_execute`,
   `databricks_volume_*`, `databricks_python_exec`,
   `databricks_terminal`, `databricks_uc_*`, `databricks_jobs_*`.
3. All mutating primitives respect two **global switches**:
   - `TREVOR_DATABRICKS_WRITES_ENABLED` — must be `true` for any
     DML / DDL.
   - `TREVOR_DATABRICKS_YOLO` — must also be `true` for destructive
     verbs (`DROP`, `TRUNCATE`, `DELETE` without `WHERE`, volume
     delete, force-overwrite, etc.).

No per-call approval loop. No `/approve` Telegram command. If you don't
trust the agent to mutate state on a given target, leave `WRITES_ENABLED`
off — every mutating tool will reject with a clear error message and
reads still work.

## Environment-variable matrix

| Variable | Default | Effect |
|---|---|---|
| `TREVOR_DATABRICKS_WRITES_ENABLED` | `false` | Master switch. `false` → every mutating primitive returns `{"error":"...disabled..."}`. `true` → DML + non-destructive DDL allowed on allowlisted targets. |
| `TREVOR_DATABRICKS_YOLO` | `false` | Destructive-op switch. Only consulted when `WRITES_ENABLED=true`. `false` → `DROP`/`TRUNCATE`/`DELETE`-without-`WHERE`/`ALTER DROP`/`CREATE OR REPLACE`/`REVOKE`/`VACUUM`/`PURGE`/volume delete/overwrite-existing rejected. `true` → allowed, logged as `dbx_yolo_call`. |
| `TREVOR_DATABRICKS_WRITE_ALLOWED_SCHEMAS` | `${catalog}.${schema}.*` | CSV of `catalog.schema.table` patterns the SQL-execute path may target. Wildcards: `cat.*`, `cat.sch.*`, `cat.sch.user_*`. |
| `TREVOR_DATABRICKS_WRITE_ALLOWED_VOLUMES` | `/Volumes/${catalog}/${schema}/${trevor_home_volume}`, `/Volumes/${catalog}/${schema}/${artifacts_volume}` | CSV of UC Volume path prefixes the write tools may touch. |
| `TREVOR_DATABRICKS_VOLUME_READ_PREFIXES` | (none) | CSV of read prefixes for `databricks_volume_read` / `databricks_volume_list`. The write allowlist is auto-included. |
| `TREVOR_DATABRICKS_WAREHOUSE_ID` | (deploy-time pin) | SQL warehouse used by both the readonly and execute SQL tools. Required for any SQL. |

## Per-target defaults (from `databricks.yml`)

| Target | `writes_enabled` | `yolo` | Rationale |
|---|---|---|---|
| `free` | `false` | `false` | Free Edition is shared / restricted — never escalate privileges by default. Operator opts in per deploy via `--var writes_enabled=true`. |
| `dev` | `true` | `false` | Mutating workflows allowed for iteration. Destructive ops still need a deliberate `--var yolo=true` per deploy. |
| `prod` | `true` | `false` | Same posture as dev. Destructive ops are operator-issued, never agent-spontaneous. |

These are bundle variables — they flow into the app at deploy time via
`config.env` in [`resources/app.yml`](../resources/app.yml). To flip
them per deploy:

```bash
databricks bundle deploy -t dev --var writes_enabled=true --var yolo=true
```

## YOLO semantics (concrete)

`find_destructive_verbs()` in
[`app/trevor_databricks/tools/sql_guard.py`](../app/trevor_databricks/tools/sql_guard.py)
flags a statement as destructive (and therefore YOLO-gated) if any of:

| Trigger | Example |
|---|---|
| Lead verb in the destructive set | `DROP TABLE x`, `TRUNCATE TABLE x`, `VACUUM x`, `PURGE x`, `REVOKE ... FROM y` |
| `DELETE` with no `WHERE` clause | `DELETE FROM x` |
| `UPDATE` with no `WHERE` clause | `UPDATE x SET a=1` |
| `ALTER ... DROP ...` anywhere | `ALTER TABLE x DROP COLUMN c` |
| `CREATE OR REPLACE ...` | `CREATE OR REPLACE TABLE x AS SELECT 1` |

For volume primitives:

| Trigger | Tool |
|---|---|
| `databricks_volume_delete` | Always destructive — needs `YOLO=true` regardless of file existence |
| `databricks_volume_write` with `overwrite=true` and the target already exists | Needs `YOLO=true` (clean writes to non-existing paths only need `WRITES_ENABLED=true`) |

Every successful YOLO call is logged at `WARNING` level with `extras={"tool": ..., "payload": {...}}`. Tail it from the App logs.

## App SP grant matrix

The Databricks App's service principal needs the following privileges
to use the skill-driven workflows. Most of them are issued by the
[`setup_grants`](../resources/setup_job.yml) bundle job (run once after
every deploy) or by **resource bindings** in
[`resources/app.yml`](../resources/app.yml).

| Surface | Privilege | How it's granted | Why |
|---|---|---|---|
| UC catalog | `USE CATALOG` on `${var.catalog}` | `setup_grants` notebook | every SQL call hits the catalog first |
| UC schema | `USE SCHEMA`, `SELECT`, `MODIFY`, `EXECUTE` on `${var.catalog}.${var.schema}` | `setup_grants` | reads + DML for the agent's working schema |
| UC schema | `CREATE TABLE`, `CREATE VOLUME`, `CREATE FUNCTION`, `CREATE MATERIALIZED VIEW` | `setup_grants` | DDL for skill-driven object creation |
| UC schema | `READ VOLUME`, `WRITE VOLUME` | `setup_grants` | volume primitives target this schema's volumes |
| UC catalog | `CREATE SCHEMA` (optional) | `setup_grants` with `enable_create_schema=true` | required for the `databricks-unity-catalog` skill's `CREATE SCHEMA` recipe |
| Volume (`trevor_home`, `trevor_artifacts`) | `WRITE_VOLUME` | bundle binding in `app.yml` | cache mirror + agent notes |
| SQL warehouse | `CAN_USE` on `${var.warehouse_id}` | bundle binding in `app.yml` | both SQL tools route through this warehouse |
| Serving endpoint | `CAN_QUERY` on `${var.llm_endpoint}` | bundle binding in `app.yml` | the foundation model providing Trevor's brain |
| Lakebase instance | `CAN_CONNECT_AND_CREATE` on the configured instance | bundle binding in `app.yml` | session DB + `agent_app` skill scratchpad |
| Lakebase schema (`trevor_session`, `agent_app`) | `USAGE`, `CREATE`, `ALL` on tables/sequences, default-privilege grants | `setup_lakebase` notebook | session writes + skill-driven OLTP demos |
| Secrets (Telegram) | `READ` on three keys in the `${var.secrets_scope}` scope | bundle bindings in `app.yml` | outbound Telegram messages |

Privileges **not** issued by the bundle (operator responsibility if a
skill needs them):

- `CAN_MANAGE` on additional serving endpoints (for the
  `databricks-model-serving` deploy recipes). Add explicitly to
  `app.yml` as a new `serving_endpoint` binding.
- Workspace-level `Jobs CAN_USE` (for the `databricks-jobs` create-job
  recipes). Set on the SP via the workspace admin or `permissions.yml`.
- Vector Search endpoint grants (the
  `databricks-vector-search` skill). Free-Edition workspaces may
  return "tier-unavailable".

## Bundled skill catalogue

Trevor ships with the entire `databricks-skills/` subtree from
[ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit) at a
pinned tag. The skill body lives in
[`app/seeds/skills/databricks/`](../app/seeds/skills/databricks/);
[`INDEX.md`](../app/seeds/skills/databricks/INDEX.md) is the routing
manifest. Every skill is a `SKILL.md` markdown file with optional
`references/`, `examples/`, and `scripts/` siblings. Skills are not
code — they're playbooks the LLM reads via `skill_view` and then
executes through the primitives below.

The catalogue at the time of vendoring (see
[`SKILLS_VERSION`](../app/seeds/skills/databricks/SKILLS_VERSION) for
the exact tag/commit):

| Skill | What it teaches |
|---|---|
| `databricks-agent-bricks` | Build Knowledge Assistants / Genie Spaces / Supervisor Agents |
| `databricks-ai-functions` | `ai_classify`, `ai_extract`, `ai_summarize`, ..., and custom RAG pipelines |
| `databricks-aibi-dashboards` | Create / update / deploy Lakeview dashboards |
| `databricks-app-python` | Build Python Databricks Apps (FastAPI, Dash, Streamlit, ...) |
| `databricks-bundles` | Author and deploy Declarative Automation Bundles |
| `databricks-config` | Switch workspaces / profiles / `.databrickscfg` |
| `databricks-dbsql` | Advanced SQL features (DBSQL, pipe syntax, AI functions, ...) |
| `databricks-docs` | `llms.txt`-indexed Databricks doc lookups |
| `databricks-execution-compute` | Serverless / classic compute lifecycle |
| `databricks-genie` | Build and query Genie Spaces |
| `databricks-iceberg` | Managed Iceberg tables + Uniform reads |
| `databricks-jobs` | Create / list / run / update / delete Databricks Jobs |
| `databricks-lakebase-autoscale` | Lakebase Autoscaling projects + branching |
| `databricks-lakebase-provisioned` | Lakebase Provisioned OLTP patterns |
| `databricks-metric-views` | Author and publish UC Metric Views |
| `databricks-mlflow-evaluation` | `mlflow.genai.evaluate()`, scorers, MemAlign, GEPA |
| `databricks-model-serving` | Deploy classical ML, custom pyfunc, GenAI agents |
| `databricks-python-sdk` | `databricks-sdk`, `databricks-connect`, CLI, REST API |
| `databricks-spark-declarative-pipelines` | SDP/LDP/DLT, streaming tables, CDC, Auto Loader |
| `databricks-spark-structured-streaming` | Structured Streaming production patterns |
| `databricks-synthetic-data-gen` | Spark + Faker synthetic data |
| `databricks-unity-catalog` | System tables + UC volume file ops |
| `databricks-unstructured-pdf-generation` | Generate PDFs and upload to UC volumes |
| `databricks-vector-search` | Endpoints, indexes, RAG patterns |
| `databricks-zerobus-ingest` | Build Zerobus Ingest clients (gRPC) |
| `spark-python-data-source` | Custom PySpark DataSource readers/writers |

## Primitives (the only things skills actually call)

Detailed schemas live in
[`docs/TOOL_BACKEND_MATRIX.md`](TOOL_BACKEND_MATRIX.md). Cheat sheet:

| Primitive | Reads? | Writes (gated)? | Destructive (YOLO)? |
|---|:-:|:-:|:-:|
| `databricks_uc_query_readonly` | ✅ | — | — |
| `databricks_sql_execute` | ✅ (SELECT bypasses gates) | ✅ | ✅ (DROP/TRUNCATE/...) |
| `databricks_volume_read` | ✅ | — | — |
| `databricks_volume_list` | ✅ | — | — |
| `databricks_volume_write` | — | ✅ | ✅ (overwrite-existing) |
| `databricks_volume_mkdir` | — | ✅ | — |
| `databricks_volume_delete` | — | — | ✅ (always) |
| `databricks_python_exec` | ✅ | — (the script can do anything via the SDK) | — |
| `databricks_terminal` | ✅ | — (same caveat) | — |
| `databricks_jobs_list` / `databricks_uc_*` | ✅ | — | — |
| `databricks_jobs_run_allowlist` | — | ✅ (allowlist enforced separately) | — |

`databricks_python_exec` and `databricks_terminal` are intentionally
**not** gated — they run inside the same App container with the same
SP identity, so anything they do is auditable via the App logs and the
existing UC privilege boundary. Skills should still prefer the gated
primitives when they apply, so a misbehaving model is caught at the
narrowest possible chokepoint.

## Operator workflow

```bash
# One-time bundle deploy + privilege setup
scripts/deploy.sh                           # validate -> deploy -> setup_lakebase -> setup_grants -> trevor_app

# Enable writes for dev or prod
databricks bundle deploy -t dev --var writes_enabled=true

# Enable destructive ops only when you mean it
databricks bundle deploy -t dev --var writes_enabled=true --var yolo=true

# Refresh the vendored ai-dev-kit skills
scripts/sync_databricks_skills.sh --tag v0.1.12
git diff app/seeds/skills/databricks/
git commit -m "Refresh ai-dev-kit skills to v0.1.12"
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `{"error":"...disabled..."}` from `databricks_sql_execute` or `databricks_volume_write` | `TREVOR_DATABRICKS_WRITES_ENABLED=false` on the deployed App | Re-deploy with `--var writes_enabled=true` |
| `{"error":"...yolo..."}` with `destructive: [...]` | Statement is a DROP/TRUNCATE/etc. but YOLO is off | Re-deploy with `--var yolo=true` if you really want to run it |
| `{"error":"sql target(s) not in TREVOR_DATABRICKS_WRITE_ALLOWED_SCHEMAS"}` | Target table is outside the bundle's catalog/schema | Widen the allowlist via env var, or scope the statement |
| `PERMISSION_DENIED` from the SDK | The App SP doesn't actually hold the privilege the gate let through | Run `databricks bundle run setup_grants -t <target>` |
| Skills missing from `skill_list` | First-boot seed didn't run (e.g. `HERMES_HOME` was prepopulated) | Delete `<HERMES_HOME>/skills/databricks/` and restart the App; the seed runs when the directory is empty |

## Reference reading

- [`README.md`](../README.md) §9 (tool matrix), §13 (extending), §16 (license)
- [`NOTICE.md`](../NOTICE.md) §3 (ai-dev-kit attribution)
- [`docs/TOOL_BACKEND_MATRIX.md`](TOOL_BACKEND_MATRIX.md) (every tool, full surface)
- [`app/seeds/skills/databricks/VENDOR_README.md`](../app/seeds/skills/databricks/VENDOR_README.md) (modification rules + license summary)
- [`app/seeds/skills/databricks/SKILLS_VERSION`](../app/seeds/skills/databricks/SKILLS_VERSION) (pinned upstream tag/commit)
