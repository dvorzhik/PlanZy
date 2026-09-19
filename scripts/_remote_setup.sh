#!/usr/bin/env bash
# Установка зависимостей PlanZy в venv (идемпотентно).
set -eu

APP="/opt/planzy/app"

echo "=== venv check ==="
if [[ ! -x "${APP}/.venv/bin/python" ]]; then
    sudo -u planzy -H bash -c "cd ${APP} && python3.12 -m venv .venv"
    echo "venv created"
else
    echo "venv exists"
fi

echo "=== pip install (may take a while) ==="
sudo -u planzy -H bash -c "cd ${APP} && .venv/bin/pip install --upgrade pip -q && .venv/bin/pip install -e . -q"
echo "PIP_DONE"

echo "=== verify ==="
sudo -u planzy -H bash -c "cd ${APP} && .venv/bin/python -c 'import app.main as m; print(\"IMPORT_OK\", m.app.title)'"
