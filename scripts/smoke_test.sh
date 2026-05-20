#!/usr/bin/env bash
# Trevor-on-Databricks — end-to-end smoke test.
#
# Run after `scripts/deploy.sh`. Exits non-zero on first failure.
#
# Usage:
#   APP_URL=https://...databricksapps.com scripts/smoke_test.sh
#   PROFILE=trevor-free scripts/smoke_test.sh   # resolves URL via 'apps get'
#
# Environment toggles:
#   SKIP_MODEL_TURN=1   skip the /debug/model-turn call
#   SKIP_SESSIONS=1     skip the /debug/sessions call
#   SKIP_CRON=1         skip the cron tick check
#   SKIP_TOOL_PROBE=1   skip the databricks tool /debug/tools verification
#   DATABRICKS_TOKEN=…  used as Authorization: Bearer if set
#
# Exit codes:
#   0  all checked endpoints returned 2xx
#   1  app or core endpoint failed
#   2  optional check (sessions, cron) failed

set -euo pipefail

PROFILE="${PROFILE:-trevor-free}"
APP_NAME="${APP_NAME:-trevor-agent}"
EXIT_CODE=0

if [[ -z "${APP_URL:-}" ]]; then
  echo "==> resolving app URL via 'databricks --profile $PROFILE apps get $APP_NAME'"
  APP_URL=$(databricks --profile "$PROFILE" apps get "$APP_NAME" --output json | python3 -c "import sys, json; print(json.load(sys.stdin)['url'])")
fi

echo "Using APP_URL=$APP_URL"

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

req_softfail() {
  if ! req "$@"; then
    echo "  (optional check failed — continuing)"
    EXIT_CODE=2
  fi
}

echo "== Phase 1: core endpoints =="
req GET /health
req GET /
req GET /config
req GET /ready
req GET /debug/runtime
req GET /debug/supervisor
req GET /debug/health-checks

echo ""
echo "== Phase 5: UC Volume mirror =="
req GET /debug/fs
req GET '/debug/fs/list?path='

echo ""
echo "== Phase 7: tool registry & backends =="
if [[ "${SKIP_TOOL_PROBE:-0}" != "1" ]]; then
  req GET /debug/tools
fi

echo ""
echo "== Phase 3: model turn =="
if [[ "${SKIP_MODEL_TURN:-0}" != "1" ]]; then
  req POST /debug/model-turn '{"message": "ping from smoke test"}'
fi

echo ""
echo "== Phase 4 + 9: lakebase sessions & events =="
if [[ "${SKIP_SESSIONS:-0}" != "1" ]]; then
  req_softfail GET /debug/sessions
  req_softfail GET '/debug/events?limit=10'
  req_softfail GET '/debug/events?kind=app_startup&limit=5'
  req_softfail GET /debug/usage
fi

echo ""
echo "== Phase 8: cron =="
if [[ "${SKIP_CRON:-0}" != "1" ]]; then
  req_softfail GET /debug/cron
fi

echo ""
echo "== Phase 6: telegram poller status =="
req_softfail GET /debug/telegram

echo ""
echo "Smoke test complete (exit_code=$EXIT_CODE)."
exit "$EXIT_CODE"
