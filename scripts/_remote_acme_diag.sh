#!/usr/bin/env bash
# Диагностика: куда реально попадает внешний трафик на 217.149.24.198.
set -eu

echo "=== local IPs ==="
ip -brief a

echo "=== does 217.149.24.198 belong here? ==="
ip addr | grep -c '217.149.24.198' || echo "NOT_LOCAL_IP"

echo "=== acme dir ==="
sudo ls -la /var/www/html/ | head
sudo ls -la /var/www/html/.well-known/ 2>/dev/null || echo "no .well-known"

echo "=== test acme path locally ==="
sudo mkdir -p /var/www/html/.well-known/acme-challenge
echo "test-token-123" | sudo tee /var/www/html/.well-known/acme-challenge/testfile >/dev/null
curl -s -m 10 -H 'Host: planzy.dvorzhik.site' \
  http://127.0.0.1/.well-known/acme-challenge/testfile || echo LOCAL_FAILED
echo

echo "=== what does the public IP serve? ==="
curl -s -m 10 -o /dev/null -w 'HTTP %{http_code}\n' \
  http://planzy.dvorzhik.site/.well-known/acme-challenge/testfile || echo EXTERNAL_FAILED
curl -s -m 10 -i http://planzy.dvorzhik.site/.well-known/acme-challenge/testfile | head -20 || true
echo
echo "=== who answers on 217.149.24.198:80 ==="
curl -s -m 10 -o /dev/null -w 'HTTP %{http_code}\n' http://217.149.24.198/ || true
echo "=== traceroute-ish: my external IP ==="
curl -s -m 10 https://ifconfig.me || true
echo
