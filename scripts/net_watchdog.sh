#!/bin/sh
set -eu

KIS_HOST="openapivts.koreainvestment.com"
KIS_URL="https://openapivts.koreainvestment.com:29443"
STATE_DIR="/home/pi/mosasaurus/data"
mkdir -p "$STATE_DIR"

dns_ok=0
for resolver in 1.1.1.1 8.8.8.8; do
  if nslookup "$KIS_HOST" "$resolver" >/dev/null 2>&1; then
    dns_ok=1
    break
  fi
done

if [ "$dns_ok" -ne 1 ]; then
  echo "DNS network check failed"
  exit 1
fi

if ! curl -fsS --connect-timeout 5 --max-time 10 "$KIS_URL" >/dev/null 2>&1; then
  echo "KIS HTTPS endpoint check failed"
  exit 1
fi

echo "Network checks passed"
