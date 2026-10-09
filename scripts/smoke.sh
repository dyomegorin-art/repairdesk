#!/usr/bin/env bash
# End-to-end smoke test of the Docker Compose stack on PostgreSQL (synthetic data only).
# Refuses to overwrite an existing .env unless CI=true.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ] && [ "${CI:-}" != "true" ]; then
  echo "Refusing to overwrite existing .env (set CI=true to force)." >&2
  exit 2
fi

# Isolated Compose project: never touches the developer's own containers or volumes.
export COMPOSE_PROJECT_NAME=repairhub-smoke
export API_PORT=18000   # does not clash with a dev stack on 8000
docker compose down -v >/dev/null 2>&1 || true

gen() { python3 -c "import secrets;print(secrets.token_urlsafe(18))"; }
KEY=$(gen)
cat > .env <<ENV
APP_ENV=local
ALLOW_LOCAL_SYNTHETIC_AUTH=true
INTAKE_WEBHOOK_KEY=$KEY
POSTGRES_PASSWORD=$(gen)
ENV

cleanup() { docker compose logs --tail 40 api worker 2>/dev/null || true; docker compose down -v >/dev/null 2>&1; [ "${CI:-}" = "true" ] || rm -f .env; }
trap cleanup EXIT

docker compose up -d --build --wait --wait-timeout 180

U=localhost:$API_PORT
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
code() { curl -s -o /dev/null -w "%{http_code}" "$@"; }
post() { curl -s -X POST "$U/webhooks/request_in" -H "X-Intake-Webhook-Key: $1" --data @"$2"; }

[ "$(code "$U/health")" = 200 ] || fail "health"
[ "$(code -X POST "$U/webhooks/request_in" -H "X-Intake-Webhook-Key: wrong" --data @fixtures/request_valid.json)" = 401 ] || fail "bad key must be 401"

R=$(post "$KEY" fixtures/request_valid.json)
echo "$R" | grep -q '"accepted"' || fail "new intake: $R"
CASE=$(echo "$R" | python3 -c "import sys,json;print(json.load(sys.stdin)['case_id'])")

D=$(post "$KEY" fixtures/request_valid.json)
echo "$D" | grep -q '"duplicate_ignored"' || fail "replay: $D"
echo "$D" | grep -q "$CASE" || fail "replay must reference the same case"

[ "$(code -X POST "$U/webhooks/request_in" -H "X-Intake-Webhook-Key: $KEY" --data @fixtures/request_missing_identifier.json)" = 400 ] || fail "missing identifier must be 400"

H="X-Synthetic-Identity: disp-1"
curl -s "$U/api/queues/new-requests" -H "$H" | grep -q "$CASE" || fail "case not in New Requests"
[ "$(code "$U/api/queues/new-requests" -H "X-Synthetic-Identity: tech-1")" = 403 ] || fail "technician must get 403"
# technicians see only their own assigned engagements
SEED1=00000000-0000-4000-8000-000000000001
SEED2=00000000-0000-4000-8000-000000000002
curl -s "$U/api/queues/engagements" -H "X-Synthetic-Identity: tech-1" | grep -q "$SEED1" || fail "tech-1 must see own engagement"
curl -s "$U/api/queues/engagements" -H "X-Synthetic-Identity: tech-1" | grep -q "$SEED2" && fail "tech-1 must not see tech-2 engagement"
[ "$(code "$U/api/cases/$SEED2" -H "X-Synthetic-Identity: tech-1")" = 403 ] || fail "tech-1 must get 403 for another technician's case"
[ "$(code -X PATCH "$U/api/cases/$CASE/intake-status" -H "$H" -H "Content-Type: application/json" -d '{"intake_status":"NEEDS_INFORMATION"}')" = 200 ] || fail "status update"

# worker must have processed the outbox command
for _ in $(seq 1 15); do
  curl -s "$U/api/cases/$CASE/audit" -H "$H" | grep -q WORKER_COMMAND_COMPLETED && break
  sleep 1
done
curl -s "$U/api/cases/$CASE/audit" -H "$H" | grep -q WORKER_COMMAND_COMPLETED || fail "worker did not complete"

# eligibility is evaluated by the worker: washer, residential, primary area -> ELIGIBLE
for _ in $(seq 1 15); do
  curl -s "$U/api/cases/$CASE" -H "$H" | grep -q '"eligibility_result":"ELIGIBLE"' && break
  sleep 1
done
curl -s "$U/api/cases/$CASE" -H "$H" | grep -q '"eligibility_result":"ELIGIBLE"' || fail "eligibility not evaluated"

# emergency signal is stopped by the rules, and the case is not closed or declined
R2=$(post "$KEY" fixtures/request_emergency.json)
CASE2=$(echo "$R2" | python3 -c "import sys,json;print(json.load(sys.stdin)['case_id'])")
for _ in $(seq 1 15); do
  curl -s "$U/api/cases/$CASE2" -H "$H" | grep -q '"eligibility_result":"EMERGENCY_STOP"' && break
  sleep 1
done
D2=$(curl -s "$U/api/cases/$CASE2" -H "$H")
echo "$D2" | grep -q '"eligibility_result":"EMERGENCY_STOP"' || fail "emergency not stopped: $D2"
echo "$D2" | grep -q '"case_phase":"INTAKE"' || fail "emergency case must stay in INTAKE"

# committed state survives an API restart
docker compose restart api >/dev/null
for _ in $(seq 1 30); do [ "$(code "$U/health")" = 200 ] && break; sleep 1; done
[ "$(code "$U/api/cases/$CASE" -H "$H")" = 200 ] || fail "case lost after restart"

# compiled PWA is served by the same service
curl -s "$U/" | grep -q "<title>RepairDesk" || fail "PWA not served"

echo "SMOKE OK"
