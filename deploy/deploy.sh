#!/usr/bin/env bash
#
# PlanZy — обновление кода на сервере (запускать на VPS).
#
# Выполняет: git pull -> установка зависимостей -> миграции ->
# перезапуск сервиса -> проверка health-check.
#
# Использование (обычно от root — тогда шаг 4 не требует sudo):
#   sudo bash /opt/planzy/app/deploy/deploy.sh
#
# Если скрипт запускается от пользователя planzy по SSH (без TTY),
# шаг перезапуска использует `sudo -n` и требует sudoers-правила
# из deploy/sudoers-planzy-deploy (см. комментарий ниже у restart_service).

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/planzy/app}"
VENV="${PROJECT_DIR}/.venv"
SERVICE="planzy"
HEALTH_URL="http://127.0.0.1:8000/health"
HEALTH_RETRIES="${HEALTH_RETRIES:-30}"

cd "${PROJECT_DIR}"

# Перезапускает systemd-сервис, не требуя интерактивного ввода пароля.
#
# Почему не просто `sudo systemctl restart`: при запуске скрипта по SSH
# нет TTY, а sudo по умолчанию пытается запросить пароль и падает с
# «a terminal is required to read the password». Поэтому используем
# неинтерактивный режим `-n` и внятно объясняем, что делать при отказе.
#
# Требуется одно из двух:
#   1. скрипт запущен от root — sudo не нужен;
#   2. для пользователя planzy установлено правило из
#      deploy/sudoers-planzy-deploy (разрешает только restart/status planzy).
restart_service() {
    if [[ "$(id -u)" -eq 0 ]]; then
        systemctl restart "${SERVICE}"
        return
    fi

    if sudo -n systemctl restart "${SERVICE}" 2>/dev/null; then
        return
    fi

    echo "ОШИБКА: нет прав на перезапуск ${SERVICE} без пароля." >&2
    echo "Код в ${PROJECT_DIR} обновлён, но сервис работает на старом коде." >&2
    echo "Выполните одно из действий и перезапустите:" >&2
    echo "  * запустите скрипт от root: sudo bash ${PROJECT_DIR}/deploy/deploy.sh" >&2
    echo "  * или установите sudoers-правило:" >&2
    echo "      sudo install -m 0440 -o root -g root \\" >&2
    echo "          ${PROJECT_DIR}/deploy/sudoers-planzy-deploy \\" >&2
    echo "          /etc/sudoers.d/planzy-deploy" >&2
    exit 1
}

echo "==> [1/5] Получение изменений из git"
git pull --ff-only

echo "==> [2/5] Установка зависимостей"
"${VENV}/bin/pip" install --upgrade pip --quiet
"${VENV}/bin/pip" install -e . --quiet

echo "==> [3/5] Применение миграций Alembic"
"${VENV}/bin/alembic" upgrade head

echo "==> [4/5] Перезапуск сервиса ${SERVICE}"
restart_service

echo "==> [5/5] Проверка health-check"
for _attempt in $(seq 1 "${HEALTH_RETRIES}"); do
    if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
        echo "OK: сервис отвечает на ${HEALTH_URL}"
        curl -fsS "${HEALTH_URL}"
        echo
        exit 0
    fi
    sleep 1
done

echo "ОШИБКА: сервис не поднялся за ${HEALTH_RETRIES} секунд" >&2
systemctl status "${SERVICE}" --no-pager >&2 || true
exit 1
