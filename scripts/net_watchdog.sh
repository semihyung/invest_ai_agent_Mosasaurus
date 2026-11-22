#!/bin/sh
set -eu

KIS_HOST="https://api.portal-korea-investment.com"
STATE_DIR="/home/pi/mosasaurus/data"
mkdir -p "$STATE_DIR"

dns_ok=0
for host in 1.1.1.1 8.8.8.8; do
  if ping -c 1 -W 2 "$host" >/dev/null 2>&1; then
    dns_ok=1
    break
  fi
done

if [ "$dns_ok" -ne 1 ]; then
  echo "DNS network check failed"
  exit 1
fi

if ! curl -fsS --connect-timeout 5 --max-time 10 "$KIS_HOST" >/dev/null 2>&1; then
  echo "KIS HTTPS endpoint check failed"
  exit 1
fi

echo "Network checks passed"
