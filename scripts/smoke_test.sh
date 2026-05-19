#!/usr/bin/env bash
# Hermes-on-Databricks — end-to-end smoke test.
#
# Run after `scripts/deploy.sh`. Exits non-zero on first failure.
#
# Usage:
#   APP_URL=https://...databricksapps.com scripts/smoke_test.sh
#   PROFILE=hermes-free scripts/smoke_test.sh   # resolves URL via 'apps get'

set -euo pipefail

PROFILE="${PROFILE:-hermes-free}"
APP_NAME="${APP_NAME:-hermes-agent}"

if [[ -z "${APP_URL:-}" ]]; then
  echo "==> resolving app URL via 'databricks --profile $PROFILE apps get $APP_NAME'"
  APP_URL=$(databricks --profile "$PROFILE" apps get "$APP_NAME" --output json | python3 -c "import sys, json; print(json.load(sys.stdin)['url'])")
fi

echo "Using APP_URL=$APP_URL"

# We don't currently auto-acquire an OAuth token for the App's
# OAuth-gated endpoints. If you have one, export it as DATABRICKS_TOKEN
# and we'll add the Authorization header.
auth_header=()
if [[ -n "${DATABRICKS_TOKEN:-}" ]]; then
  auth_header=(-H "Authorization: Bearer $DATABRICKS_TOKEN")
fi

req() {
  local method="$1"
  local path="$2"
  local data="${3:-}"
  echo ""
  echo "==> $method $path"
  if [[ -n "$data" ]]; then
    curl -fsS -X "$method" "${auth_header[@]}" -H 'Content-Type: application/json' \
      -d "$data" "$APP_URL$path" | python3 -m json.tool
  else
    curl -fsS -X "$method" "${auth_header[@]}" "$APP_URL$path" | python3 -m json.tool
  fi
}

req GET /health
req GET /
req GET /config
req GET /ready
req GET /debug/runtime
req GET /debug/tools
req GET /debug/fs

# Optional: model turn (requires Hermes runtime up and a working endpoint binding)
if [[ "${SKIP_MODEL_TURN:-0}" != "1" ]]; then
  req POST /debug/model-turn '{"message": "ping from smoke test"}'
fi

# Optional: list sessions (requires Lakebase)
if [[ "${SKIP_SESSIONS:-0}" != "1" ]]; then
  req GET /debug/sessions || true
fi

echo ""
echo "Smoke test complete."
