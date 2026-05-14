#!/bin/sh
# Run database migrations to head, then launch the API server.
# Migrations are idempotent; running on every boot is safe and means a
# single-service deploy doesn't need a separate release step.
set -eu

alembic upgrade head

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
