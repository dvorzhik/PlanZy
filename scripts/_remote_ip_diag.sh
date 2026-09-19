#!/usr/bin/env bash
# Диагностика: какой IP реально доступен извне у oracle1.
set -eu

echo "=== oracle1 external IP ==="
curl -s -m 10 https://ifconfig.me || echo UNKNOWN
echo

echo "=== is the app reachable on the real external IP? ==="
curl -s -m 15 -o /dev/null -w 'HTTP %{http_code}\n' \
  -H 'Host: planzy.dvorzhik.site' http://129.159.151.179/health || echo FAILED
curl -s -m 15 -H 'Host: planzy.dvorzhik.site' http://129.159.151.179/health || true
echo
echo "=== HTTPS on 443 external ==="
curl -sk -m 15 -o /dev/null -w 'HTTPS %{http_code}\n' https://129.159.151.179/health || echo FAILED
echo
echo "=== DNS records ==="
for h in planzy.dvorzhik.site dvorzhik.site; do
  echo "-- $h"
  getent hosts "$h" || true
done
echo
echo "=== what serves dvorzhik.site now ==="
curl -s -m 15 -o /dev/null -w 'dvorzhik.site HTTP %{http_code}\n' https://dvorzhik.site/ || true
