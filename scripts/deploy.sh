#!/usr/bin/env bash
# Hermes-on-Databricks — deploy script.
#
# Usage:
#   scripts/deploy.sh              # uses target 'free' and profile 'hermes-free'
#   TARGET=prod PROFILE=my-prod scripts/deploy.sh
#
# Steps:
#   1. databricks bundle validate
#   2. databricks bundle deploy
#   3. databricks bundle run setup_lakebase  (idempotent DDL + GRANTs on Lakebase)
#   4. databricks bundle run setup_grants    (idempotent UC GRANTs for the App SP)
#   5. databricks bundle run hermes_app      (start the App)
#
# Prerequisites:
#   - databricks CLI installed and on PATH
#   - 'databricks auth login --host <workspace-url> --profile $PROFILE' done once

set -euo pipefail

TARGET="${TARGET:-free}"
PROFILE="${PROFILE:-hermes-free}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> validate (target=$TARGET, profile=$PROFILE)"
databricks bundle validate --profile "$PROFILE" -t "$TARGET"

echo "==> deploy (target=$TARGET, profile=$PROFILE)"
databricks bundle deploy --profile "$PROFILE" -t "$TARGET"

echo "==> run setup_lakebase (Lakebase schema + grants)"
databricks bundle run setup_lakebase --profile "$PROFILE" -t "$TARGET"

echo "==> run setup_grants (UC privileges for the App SP)"
databricks bundle run setup_grants --profile "$PROFILE" -t "$TARGET"

echo "==> start hermes_app"
databricks bundle run hermes_app --profile "$PROFILE" -t "$TARGET"

echo ""
echo "Deployment complete. Hint:"
echo "  databricks --profile $PROFILE apps get \$(databricks --profile $PROFILE apps list -o json | jq -r '.[] | select(.name|test(\"hermes-agent\")).name')"
