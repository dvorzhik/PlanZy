#!/usr/bin/env bash
# Регистрация поддомена planzy.duckdns.org и привязка к текущему внешнему IP.
set -eu

TOKEN='19c3f4bc-ad23-4900-949f-f1f8a7389b06'
DOMAIN='planzy'

echo "=== current external IP ==="
EXT_IP="$(curl -s -m 10 https://ifconfig.me)"
echo "${EXT_IP}"

echo "=== duckdns update (set IP explicitly) ==="
curl -s -m 15 "https://www.duckdns.org/update?domains=${DOMAIN}&token=${TOKEN}&ip=${EXT_IP}"
echo

echo "=== resolve check ==="
getent hosts "${DOMAIN}.duckdns.org" || echo "NOT_RESOLVED_YET"
