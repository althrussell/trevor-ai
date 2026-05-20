# Databricks notebook source
# MAGIC %md
# MAGIC # Trevor-on-Databricks — UC grants for the App SP
# MAGIC
# MAGIC Run by the bundle job `setup_grants`. Issues idempotent
# MAGIC Unity Catalog grants on the bundle's catalog + schema so the
# MAGIC App service principal can use `databricks_sql_execute`,
# MAGIC `databricks_volume_write`, and the skill-driven workflows that
# MAGIC depend on them.
# MAGIC
# MAGIC Workspace-level grants (SQL warehouse `CAN_USE`, serving
# MAGIC endpoint `CAN_MANAGE`, Lakebase instance `CAN_MANAGE`) are
# MAGIC declared as app **resource bindings** in
# MAGIC `resources/app.yml` and applied by `databricks bundle deploy`;
# MAGIC this notebook only touches Unity Catalog.
# MAGIC
# MAGIC Idempotent — safe to re-run after every bundle deploy.

# COMMAND ----------

dbutils.widgets.text("catalog", "workspace")
dbutils.widgets.text("schema", "trevor_agent")
dbutils.widgets.text("app_sp_client_id", "")
dbutils.widgets.dropdown("enable_create_schema", "false", ["true", "false"])

catalog = dbutils.widgets.get("catalog") or "workspace"
schema = dbutils.widgets.get("schema") or "trevor_agent"
app_sp_client_id = dbutils.widgets.get("app_sp_client_id") or ""
enable_create_schema = dbutils.widgets.get("enable_create_schema") == "true"

print(f"catalog={catalog}")
print(f"schema={schema}")
print(f"app_sp_client_id={app_sp_client_id or '(none — exiting early)'}")
print(f"enable_create_schema={enable_create_schema}")

if not app_sp_client_id:
    print("No app_sp_client_id provided; nothing to grant. Exiting.")
    dbutils.notebook.exit("skipped: no app_sp_client_id")

# COMMAND ----------

# We quote the principal as `<client-id>` so UC accepts hyphens etc.
SP = f"`{app_sp_client_id}`"
SCHEMA_FQN = f"`{catalog}`.`{schema}`"
CATALOG_FQN = f"`{catalog}`"

GRANT_STATEMENTS = [
    # Catalog-level: needed for the App SP to even see anything inside.
    f"GRANT USE CATALOG ON CATALOG {CATALOG_FQN} TO {SP}",
    # Schema-level: cover the read + write surface the skills need.
    f"GRANT USE SCHEMA ON SCHEMA {SCHEMA_FQN} TO {SP}",
    f"GRANT CREATE TABLE ON SCHEMA {SCHEMA_FQN} TO {SP}",
    f"GRANT CREATE VOLUME ON SCHEMA {SCHEMA_FQN} TO {SP}",
    f"GRANT CREATE FUNCTION ON SCHEMA {SCHEMA_FQN} TO {SP}",
    f"GRANT CREATE MATERIALIZED VIEW ON SCHEMA {SCHEMA_FQN} TO {SP}",
    f"GRANT MODIFY ON SCHEMA {SCHEMA_FQN} TO {SP}",
    f"GRANT SELECT ON SCHEMA {SCHEMA_FQN} TO {SP}",
    f"GRANT READ VOLUME ON SCHEMA {SCHEMA_FQN} TO {SP}",
    f"GRANT WRITE VOLUME ON SCHEMA {SCHEMA_FQN} TO {SP}",
    f"GRANT EXECUTE ON SCHEMA {SCHEMA_FQN} TO {SP}",
]

if enable_create_schema:
    GRANT_STATEMENTS.insert(
        1,
        f"GRANT CREATE SCHEMA ON CATALOG {CATALOG_FQN} TO {SP}",
    )

# COMMAND ----------

applied = 0
failed: list[tuple[str, str]] = []
for stmt in GRANT_STATEMENTS:
    try:
        spark.sql(stmt)
        print(f"OK : {stmt}")
        applied += 1
    except Exception as exc:
        # Most failures here are PERMISSION_DENIED (you're not the
        # catalog/schema owner) or the privilege already being held.
        # Both are non-fatal — we surface them and continue so a single
        # pre-existing grant doesn't block the rest.
        msg = f"{type(exc).__name__}: {exc}"
        if "already" in msg.lower() or "is already a" in msg.lower():
            print(f"SKIP: {stmt} (already granted)")
            applied += 1
        else:
            print(f"FAIL: {stmt} -> {msg}")
            failed.append((stmt, msg))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary

# COMMAND ----------

print(f"applied={applied}/{len(GRANT_STATEMENTS)}")
if failed:
    print(f"failed_count={len(failed)}")
    for stmt, msg in failed:
        print(f"  - {stmt}")
        print(f"    {msg}")
    # We do NOT raise — the operator may have intentionally restricted
    # one of the privileges. The bundle-driven workspace bindings
    # (sql_warehouse CAN_USE, serving CAN_MANAGE, ...) are independent
    # of this notebook and will still apply.
print("setup_grants complete")
