#!/bin/sh
# Apex Retail · API container entrypoint.
#
# Runs Alembic migrations to head, then exec's the API server. This makes
# `docker compose up` self-contained: a fresh DB is migrated on first boot,
# and a re-deploy applies any new migrations automatically.

set -eu

echo "▶ apex-api · running migrations…"
alembic upgrade head

echo "▶ apex-api · starting uvicorn on ${API_HOST:-0.0.0.0}:${API_PORT:-8000}"
exec uvicorn app.main:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8000}"
