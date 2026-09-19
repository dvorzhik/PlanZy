#!/usr/bin/env bash
# Установка nginx-конфига PlanZy (HTTP-only, до выпуска сертификата).
set -eu

APP="/opt/planzy/app"

echo "=== backup default site ==="
if [[ -e /etc/nginx/sites-enabled/default ]]; then
    sudo rm -f /etc/nginx/sites-enabled/default
    echo "default disabled"
fi

echo "=== write planzy site (http only, for certbot) ==="
sudo tee /etc/nginx/sites-available/planzy >/dev/null <<'EOF'
upstream planzy_app {
    server 127.0.0.1:8000;
    keepalive 32;
}

server {
    listen 80;
    listen [::]:80;
    server_name planzy.dvorzhik.site;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location /static/ {
        alias /opt/planzy/app/app/web/static/;
        expires 7d;
        access_log off;
        try_files $uri =404;
    }

    location /health {
        proxy_pass http://planzy_app;
        proxy_set_header Host $host;
        access_log off;
    }

    location / {
        proxy_pass http://planzy_app;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection        "";
        proxy_connect_timeout 10s;
        proxy_send_timeout    60s;
        proxy_read_timeout    60s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/planzy /etc/nginx/sites-enabled/planzy

echo "=== nginx -t ==="
sudo nginx -t

echo "=== reload ==="
sudo systemctl reload nginx
sleep 2

echo "=== local http check ==="
curl -s -m 10 -H 'Host: planzy.dvorzhik.site' http://127.0.0.1/health || echo LOCAL_HTTP_FAILED
echo
