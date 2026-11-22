#!/bin/sh
set -eu

REPO_DIR="/home/pi/mosasaurus"
KST_ZONE="Asia/Seoul"

current_hour_minute="$(TZ="$KST_ZONE" date '+%H:%M')"
case "$current_hour_minute" in
  08:[5-9][0-9]|09:*|10:*|11:*|12:*|13:*|14:*|15:[0-2][0-9])
    echo "Deployment blocked during 08:50-15:25 KST"
    exit 1
    ;;
esac

cd "$REPO_DIR"

backup_path="$(./scripts/backup_db.sh)"
previous_commit="$(git rev-parse HEAD)"

git fetch origin main
git reset --hard origin/main

docker compose up -d --build --remove-orphans

python3 - <<'PY'
import sqlite3
connection = sqlite3.connect("/home/pi/mosasaurus/data/mosasaurus.db")
try:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("Post-deploy SQLite integrity check failed")
finally:
    connection.close()
PY

healthy=false
attempt=0
while [ "$attempt" -lt 6 ]; do
  status="$(docker inspect mosasaurus-agent --format '{{.State.Health.Status}}' 2>/dev/null || true)"
  if [ "$status" = "healthy" ]; then
    healthy=true
    break
  fi
  attempt=$((attempt + 1))
  sleep 10
done

if [ "$healthy" != "true" ]; then
  echo "Post-deploy health check failed; rolling back to $previous_commit"
  git reset --hard "$previous_commit"
  docker compose up -d --build --remove-orphans
  exit 1
fi
