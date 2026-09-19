#!/usr/bin/env bash
#
# PlanZy — обновление кода на сервере (запускать на VPS).
#
# Выполняет: git pull -> установка зависимостей -> миграции ->
# перезапуск сервиса -> проверка health-check.
#
# Использование:
#   sudo -u planzy /opt/planzy/deploy/deploy.sh

set -euo pipefail

PROJECT_DIR="/opt/planzy"
VENV="${PROJECT_DIR}/.venv"
SERVICE="planzy"
HEALTH_URL="http://127.0.0.1:8000/health"

cd "${PROJECT_DIR}"

echo "==> [1/5] Получение изменений из git"
git pull --ff-only

echo "==> [2/5] Установка зависимостей"
"${VENV}/bin/pip" install --upgrade pip --quiet
"${VENV}/bin/pip" install -e . --quiet

echo "==> [3/5] Применение миграций Alembic"
"${VENV}/bin/alembic" upgrade head

echo "==> [4/5] Перезапуск сервиса ${SERVICE}"
sudo systemctl restart "${SERVICE}"

echo "==> [5/5] Проверка health-check"
for attempt in $(seq 1 15); do
    if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
        echo "OK: сервис отвечает на ${HEALTH_URL}"
        curl -fsS "${HEALTH_URL}"
        echo
        exit 0
    fi
    sleep 1
done

echo "ОШИБКА: сервис не поднялся за 15 секунд" >&2
sudo systemctl status "${SERVICE}" --no-pager >&2 || true
exit 1
