#!/usr/bin/env bash
#
# PlanZy — резервное копирование базы данных SQLite.
#
# Использует `sqlite3 .backup`, который безопасен при включённом WAL
# (в отличие от простого `cp`, который может скопировать БД в
# несогласованном состоянии).
#
# Установка в cron (ежедневно в 03:00):
#   sudo crontab -e
#   0 3 * * * /opt/planzy/deploy/backup.sh >> /var/log/planzy-backup.log 2>&1
#
# Ротация: храним последние 14 копий.

set -euo pipefail

PROJECT_DIR="/opt/planzy"
DB_PATH="${PROJECT_DIR}/tutor.sqlite3"
BACKUP_DIR="${PROJECT_DIR}/backups"
KEEP_COPIES=14

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="${BACKUP_DIR}/tutor-${TIMESTAMP}.sqlite3"

mkdir -p "${BACKUP_DIR}"

if [[ ! -f "${DB_PATH}" ]]; then
    echo "[$(date '+%F %T')] ОШИБКА: база не найдена: ${DB_PATH}" >&2
    exit 1
fi

# Атомарный бэкап средствами самого SQLite.
sqlite3 "${DB_PATH}" ".backup '${TARGET}'"

# Проверяем целостность копии.
INTEGRITY="$(sqlite3 "${TARGET}" "PRAGMA integrity_check;")"
if [[ "${INTEGRITY}" != "ok" ]]; then
    echo "[$(date '+%F %T')] ОШИБКА: копия повреждена: ${INTEGRITY}" >&2
    rm -f "${TARGET}"
    exit 1
fi

# Сжимаем для экономии места.
gzip -f "${TARGET}"

# Ротация: удаляем всё, кроме последних KEEP_COPIES архивов.
ls -1t "${BACKUP_DIR}"/tutor-*.sqlite3.gz 2>/dev/null \
    | tail -n "+$((KEEP_COPIES + 1))" \
    | xargs -r rm -f

echo "[$(date '+%F %T')] OK: ${TARGET}.gz (хранится копий: $(ls -1 "${BACKUP_DIR}"/tutor-*.sqlite3.gz 2>/dev/null | wc -l))"
