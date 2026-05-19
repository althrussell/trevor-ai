#!/usr/bin/env bash
# Local development helper — runs the FastAPI app against your laptop.
#
# Most Databricks-specific subsystems (Lakebase, UC Volumes, Telegram)
# will report "unavailable" unless you point HERMES_DATABRICKS_* at a
# real workspace + secrets. The basic /health, /, /config endpoints
# work without any Databricks credentials.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR/app"

if [[ ! -d "../.venv" ]]; then
  echo "==> creating venv at ../.venv"
  python3 -m venv ../.venv
fi
# shellcheck disable=SC1091
source ../.venv/bin/activate

echo "==> installing requirements"
pip install -q --upgrade pip
pip install -q -r requirements.txt

export HERMES_DATABRICKS_AGENT_NAME="${HERMES_DATABRICKS_AGENT_NAME:-Hermes-Dev}"
export HERMES_HOME="${HERMES_HOME:-$ROOT_DIR/.hermes_cache/hermes_home}"
mkdir -p "$HERMES_HOME"

echo "==> launching uvicorn on http://127.0.0.1:8000"
exec uvicorn app:app --reload --host 127.0.0.1 --port 8000
