#!/usr/bin/env bash
# Run the backend tests against a throwaway PostgreSQL container.
# Usage: scripts/test.sh [pytest args]    (needs Docker and backend deps installed)
set -euo pipefail
cd "$(dirname "$0")/.."

NAME=repairdesk-test-pg
PORT=${TEST_PG_PORT:-55432}
PYTHON=${PYTHON:-.venv/bin/python}

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --rm --name "$NAME" -p "127.0.0.1:$PORT:5432" \
  -e POSTGRES_PASSWORD=test postgres:16 >/dev/null
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT

for _ in $(seq 1 40); do
  docker exec "$NAME" pg_isready -U postgres -h 127.0.0.1 >/dev/null 2>&1 && break
  sleep 0.5
done
# pg_isready can pass before the init restart finishes; give it a moment to settle.
sleep 2

cd backend
TEST_DATABASE_URL="postgresql+psycopg://postgres:test@127.0.0.1:$PORT/postgres" \
  "../$PYTHON" -m pytest -q "$@"
