#!/usr/bin/env bash
# Миграции Alembic и создание тенанта-владельца.
set -eu
cd /opt/planzy/app

echo "=== alembic upgrade head ==="
.venv/bin/alembic upgrade head

echo "=== tables ==="
sqlite3 tutor.sqlite3 ".tables"

echo "=== seed ==="
.venv/bin/python -m scripts.seed

echo "=== tables after seed ==="
sqlite3 tutor.sqlite3 "SELECT id, name, telegram_user_id FROM tenants;"
