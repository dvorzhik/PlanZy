#!/usr/bin/env bash
# nginx-конфиг PlanZy для planzy.duckdns.org + выпуск SSL.
set -eu

DOMAIN='planzy.duckdns.org'

echo "=== write nginx site (http first) ==="
sudo tee /etc/nginx/sites-available/planzy >/dev/null <<EOF
upstream planzy_app {
    server 127.0.0.1:8000;
    keepalive 32;
}

server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location /static/ {
        alias /opt/planzy/app/app/web/static/;
        expires 7d;
        access_log off;
        try_files \$uri =404;
    }

    location = /health {
        proxy_pass http://planzy_app;
        proxy_set_header Host \$host;
        access_log off;
    }

    location / {
        proxy_pass http://planzy_app;
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Connection        "";
        proxy_connect_timeout 10s;
        proxy_send_timeout    60s;
        proxy_read_timeout    60s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/planzy /etc/nginx/sites-enabled/planzy
sudo nginx -t
sudo systemctl reload nginx
sleep 2

echo "=== local check via Host ==="
curl -s -m 10 -H "Host: ${DOMAIN}" http://127.0.0.1/health
echo

echo "=== external http check ==="
curl -s -m 15 -o /dev/null -w 'HTTP %{http_code}\n' "http://${DOMAIN}/health"

echo "=== certbot ==="
sudo certbot --nginx -d "${DOMAIN}" \
    --non-interactive --agree-tos --redirect \
    --register-unsafely-without-email 2>&1 | tail -20

echo "=== nginx -t after certbot ==="
sudo nginx -t
sudo systemctl reload nginx
sleep 2

echo "=== https check ==="
curl -s -m 15 "https://${DOMAIN}/health" || echo HTTPS_FAILED
echo
curl -s -m 15 -o /dev/null -w 'HTTPS %{http_code}\n' "https://${DOMAIN}/health"
