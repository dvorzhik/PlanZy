#!/usr/bin/env bash
# Обновление BASE_URL на duckdns-домен + автообновление IP.
set -eu

APP="/opt/planzy/app"
DOMAIN="planzy.duckdns.org"
TOKEN='19c3f4bc-ad23-4900-949f-f1f8a7389b06'

echo "=== update BASE_URL in .env ==="
sudo sed -i "s|^BASE_URL=.*|BASE_URL=https://${DOMAIN}|" "${APP}/.env"
sudo grep -E '^BASE_URL' "${APP}/.env"

echo "=== restart service ==="
sudo systemctl restart planzy
sleep 8
sudo systemctl is-active planzy
curl -s -m 10 http://127.0.0.1:8000/health
echo

echo "=== duckdns cron for planzy ==="
sudo tee /usr/local/bin/duck-planzy.sh >/dev/null <<EOF
#!/bin/sh
# Автообновление динамического IP для planzy.duckdns.org
url="https://www.duckdns.org/update?domains=${DOMAIN%%.*}&token=${TOKEN}&ip="
curl -k -o /dev/null -s "\$url"
echo "duckdns(planzy) updated at \$(date)" >> /var/log/duckdns.log
EOF
sudo chmod +x /usr/local/bin/duck-planzy.sh

echo "=== add cron entry ==="
if ! sudo grep -q duck-planzy /etc/cron.d/duckdns; then
    echo '*/5 * * * * root /usr/local/bin/duck-planzy.sh >/dev/null 2>&1' | sudo tee -a /etc/cron.d/duckdns >/dev/null
fi
sudo cat /etc/cron.d/duckdns

echo "=== verify BASE_URL visible to app ==="
sudo -u planzy -H bash -c "cd ${APP} && .venv/bin/python -c 'from app.core.config import settings; print(settings.base_url)'"
