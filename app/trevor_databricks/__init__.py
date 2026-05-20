"""Databricks-native compatibility layers for the Hermes Agent runtime.

This package does NOT replace any part of Hermes core. It only
provides isolated adapters so that ``hermes-agent`` can run inside a
Databricks Apps container against Databricks-managed services for
state (Lakebase), files (UC Volumes), secrets (Databricks Secrets),
and model inference (Databricks Model Serving).

Public surface (stable):

* ``trevor_databricks.config``           — environment-driven config
* ``trevor_databricks.runtime``          — Hermes lifecycle inside the App
* ``trevor_databricks.databricks_provider`` — Databricks model provider adapter
* ``trevor_databricks.state``            — Lakebase-backed SessionDB
* ``trevor_databricks.fs``               — UC Volume filesystem
* ``trevor_databricks.telegram_polling`` — Telegram channel
* ``trevor_databricks.supervisor``       — async supervisor for the App
* ``trevor_databricks.tools``            — Databricks toolset + tool backends
* ``trevor_databricks.observability``    — structured logging + redaction
"""

__version__ = "0.1.0"
