"""Databricks-native compatibility layers for the Hermes Agent runtime.

This package does NOT replace any part of Hermes core. It only
provides isolated adapters so that ``hermes-agent`` can run inside a
Databricks Apps container against Databricks-managed services for
state (Lakebase), files (UC Volumes), secrets (Databricks Secrets),
and model inference (Databricks Model Serving).

Public surface (stable):

* ``hermes_databricks.config``           — environment-driven config
* ``hermes_databricks.runtime``          — Hermes lifecycle inside the App
* ``hermes_databricks.databricks_provider`` — Databricks model provider adapter
* ``hermes_databricks.state``            — Lakebase-backed SessionDB
* ``hermes_databricks.fs``               — UC Volume filesystem
* ``hermes_databricks.telegram_polling`` — Telegram channel
* ``hermes_databricks.supervisor``       — async supervisor for the App
* ``hermes_databricks.tools``            — Databricks toolset + tool backends
* ``hermes_databricks.observability``    — structured logging + redaction
"""

__version__ = "0.1.0"
