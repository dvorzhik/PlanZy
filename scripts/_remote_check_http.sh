#!/usr/bin/env bash
# Проверка цепочки nginx -> PlanZy.
set -eu
echo "=== /health via nginx ==="
curl -s -m 10 -H 'Host: planzy.dvorzhik.site' http://127.0.0.1/health
echo
echo "=== / via nginx (headers) ==="
curl -s -m 10 -o /dev/null -w 'HTTP %{http_code} -> redirect: %{redirect_url}\n' \
  -H 'Host: planzy.dvorzhik.site' http://127.0.0.1/
echo "=== localhost without Host ==="
curl -s -m 10 -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1/health
