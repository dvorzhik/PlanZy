#!/usr/bin/env bash
# Настройка ежедневного бэкапа SQLite PlanZy.
set -eu

APP="/opt/planzy/app"

echo "=== backup dir ==="
sudo mkdir -p "${APP}/backups"
sudo chown planzy:planzy "${APP}/backups"

echo "=== make backup.sh executable ==="
sudo chmod +x "${APP}/deploy/backup.sh"
sudo ls -la "${APP}/deploy/backup.sh"

echo "=== dry run ==="
sudo -u planzy -H "${APP}/deploy/backup.sh"

echo "=== backups ==="
sudo ls -la "${APP}/backups/" | tail -5

echo "=== cron entry (daily 03:00) ==="
CRON_FILE=/etc/cron.d/planzy-backup
if [[ ! -f "${CRON_FILE}" ]]; then
    sudo tee "${CRON_FILE}" >/dev/null <<EOF
# PlanZy — ежедневный бэкап SQLite в 03:00
0 3 * * * planzy ${APP}/deploy/backup.sh >> /var/log/planzy-backup.log 2>&1
EOF
    sudo chmod 644 "${CRON_FILE}"
    echo "cron created"
else
    echo "cron already exists"
fi
sudo cat "${CRON_FILE}"
