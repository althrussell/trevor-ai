#!/usr/bin/env bash
# Hermes-on-Databricks — destroy script. Removes all bundle resources.
#
# Usage:
#   scripts/destroy.sh
#   TARGET=prod PROFILE=my-prod scripts/destroy.sh
#
# Notes:
#   - Bundle destroy removes the App, jobs, volumes, schema, and the
#     Lakebase instance. Lakebase has a 7-day purge window for managed
#     resources; pass `--purge` if you need an immediate hard delete.

set -euo pipefail

TARGET="${TARGET:-free}"
PROFILE="${PROFILE:-hermes-free}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> destroy bundle (target=$TARGET, profile=$PROFILE)"
databricks bundle destroy --profile "$PROFILE" -t "$TARGET" --auto-approve
