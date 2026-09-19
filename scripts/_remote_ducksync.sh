#!/usr/bin/env bash
# Добавление cron-задачи автообновления IP для planzy.duckdns.org.
set -eu

TOKEN='19c3f4bc-ad23-4900-949f-f1f8a7389b06'

echo "=== create updater script ==="
sudo tee /usr/local/bin/duck-planzy.sh >/dev/null <<EOF
#!/bin/sh
# Автообновление динамического IP для planzy.duckdns.org
url="https://www.duckdns.org/update?domains=planzy&token=${TOKEN}&ip="
curl -k -o /dev/null -s "\$url"
echo "duckdns(planzy) updated at \$(date)" >> /var/log/duckdns.log
EOF
sudo chmod +x /usr/local/bin/duck-planzy.sh

echo "=== add cron entry ==="
if ! sudo grep -q 'duck-planzy' /etc/cron.d/duckdns; then
    echo '*/5 * * * * root /usr/local/bin/duck-planzy.sh >/dev/null 2>&1' | sudo tee -a /etc/cron.d/duckdns >/dev/null
    echo "cron entry added"
else
    echo "cron entry already present"
fi
sudo cat /etc/cron.d/duckdns

echo "=== run once ==="
sudo /usr/local/bin/duck-planzy.sh
echo "updater exit: $?"

echo "=== resolve ==="
getent hosts planzy.duckdns.org

echo "=== BASE_URL in app ==="
sudo -u planzy -H bash -c "cd /opt/planzy/app && .venv/bin/python -c 'from app.core.config import settings; print(\"BASE_URL =\", settings.base_url)'"
