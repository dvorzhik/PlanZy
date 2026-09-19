#!/usr/bin/env bash
# Переустановка systemd-юнита PlanZy (без --log-config).
set -eu

APP="/opt/planzy/app"

sudo systemctl stop planzy 2>/dev/null || true

echo "=== write unit ==="
sed -e 's|/opt/planzy/.venv|/opt/planzy/app/.venv|' \
    -e 's|WorkingDirectory=/opt/planzy$|WorkingDirectory=/opt/planzy/app|' \
    -e 's|ReadWritePaths=/opt/planzy$|ReadWritePaths=/opt/planzy/app|' \
    -e '/--log-config/d' \
    "${APP}/deploy/planzy.service" | sudo tee /etc/systemd/system/planzy.service >/dev/null

echo "=== ExecStart block ==="
grep -A5 '^ExecStart' /etc/systemd/system/planzy.service

sudo systemctl daemon-reload
sudo systemctl restart planzy
sleep 8

echo "=== active? ==="
sudo systemctl is-active planzy || true
echo "=== health ==="
curl -s -m 10 http://127.0.0.1:8000/health || echo HEALTH_FAILED
echo
echo "=== journal ==="
sudo journalctl -u planzy -n 15 --no-pager | tail -15
