#!/usr/bin/env bash
# Обновление скриптов деплоя из репозитория и прогон бэкапа.
set -eu

APP="/opt/planzy/app"

echo "=== git pull ==="
sudo -u planzy -H git -C "${APP}" fetch origin main
sudo -u planzy -H git -C "${APP}" reset --hard origin/main

echo "=== fix line endings ==="
sudo find "${APP}/deploy" -type f -exec sed -i 's/\r$//' {} +
sudo find "${APP}/deploy" -maxdepth 1 -name '*.sh' -exec chmod +x {} +

echo "=== verify PROJECT_DIR ==="
sudo grep -n 'PROJECT_DIR=' "${APP}/deploy/backup.sh" "${APP}/deploy/deploy.sh"

echo "=== run backup ==="
sudo -u planzy -H "${APP}/deploy/backup.sh"

echo "=== backups ==="
sudo ls -la "${APP}/backups/"
