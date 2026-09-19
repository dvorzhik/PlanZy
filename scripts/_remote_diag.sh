#!/usr/bin/env bash
# Диагностика nginx на oracle1.
set -eu
echo "=== listen ports ==="
sudo ss -tln | grep -E ':80|:443' || echo "NOTHING_ON_80_443"
echo
echo "=== nginx.conf includes ==="
grep -nE 'include|listen' /etc/nginx/nginx.conf | head -10
echo
echo "=== enabled sites ==="
sudo ls -l /etc/nginx/sites-enabled/
echo
echo "=== active planzy site ==="
sudo cat /etc/nginx/sites-available/planzy | grep -nE 'listen|server_name|proxy_pass|alias'
echo
echo "=== nginx status ==="
sudo systemctl status nginx --no-pager 2>&1 | head -8
echo
echo "=== recent nginx errors ==="
sudo journalctl -u nginx -n 15 --no-pager --output=cat | tail -15
