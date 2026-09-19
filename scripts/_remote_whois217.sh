#!/usr/bin/env bash
# Кто обслуживает 217.149.24.198 и что там за nginx.
set -eu
echo "=== headers from 217.149.24.198 ==="
curl -s -m 10 -i http://217.149.24.198/ | head -15 || true
echo
echo "=== with our Host header ==="
curl -s -m 10 -i -H 'Host: planzy.dvorzhik.site' http://217.149.24.198/ | head -15 || true
echo
echo "=== dvorzhik.site root ==="
curl -s -m 10 -i http://dvorzhik.site/ | head -15 || true
echo
echo "=== SSH reverse: is 217.x reachable from here? ==="
timeout 5 bash -c 'cat < /dev/null > /dev/tcp/217.149.24.198/22' && echo "22 OPEN" || echo "22 CLOSED/FILTERED"
timeout 5 bash -c 'cat < /dev/null > /dev/tcp/217.149.24.198/443' && echo "443 OPEN" || echo "443 CLOSED/FILTERED"
