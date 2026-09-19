#!/usr/bin/env bash
# Выпуск SSL-сертификата Let's Encrypt для planzy.dvorzhik.site.
set -eu

echo "=== DNS check ==="
getent hosts planzy.dvorzhik.site || true

echo "=== certbot issue ==="
sudo certbot --nginx -d planzy.dvorzhik.site \
    --non-interactive --agree-tos --redirect \
    --register-unsafely-without-email 2>&1 | tail -25

echo "=== nginx -t ==="
sudo nginx -t

echo "=== reload ==="
sudo systemctl reload nginx
sleep 2

echo "=== https check ==="
curl -s -m 15 -o /dev/null -w 'HTTPS %{http_code}\n' https://planzy.dvorzhik.site/health || echo HTTPS_FAILED
curl -s -m 15 https://planzy.dvorzhik.site/health || true
echo
echo "=== http redirect check ==="
curl -s -m 15 -o /dev/null -w 'HTTP %{http_code} -> %{redirect_url}\n' http://planzy.dvorzhik.site/health || true
