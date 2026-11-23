#!/bin/sh
set -eu

REPO_DIR="/home/pi/mosasaurus"
KST_ZONE="Asia/Seoul"

current_hour_minute="$(TZ="$KST_ZONE" date '+%H:%M')"
case "$current_hour_minute" in
  08:[5-9][0-9]|09:*|10:*|11:*|12:*|13:*|14:*|15:0[0-9]|15:1[0-9]|15:2[0-5])
    echo "Deployment blocked during 08:50-15:25 KST"
    exit 1
    ;;
esac

cd "$REPO_DIR"

if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
  echo "Deployment blocked because the working tree is not clean"
  exit 1
fi

previous_commit="$(git rev-parse HEAD)"
backup_path="$(./scripts/backup_db.sh)"
if [ ! -f "$backup_path.sha256" ]; then
  echo "Database backup checksum was not created"
  exit 1
fi

deployment_started=0
rollback_deployment() {
  status=$?
  if [ "$status" -ne 0 ] && [ "$deployment_started" -eq 1 ]; then
    echo "Deployment failed; restoring source revision $previous_commit"
    git checkout --detach "$previous_commit" || true
    docker compose up -d --build --remove-orphans || true
  fi
}
trap rollback_deployment EXIT

git fetch origin main
git checkout --detach origin/main
deployment_started=1

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
  echo "Post-deploy health check failed"
  exit 1
fi

commit_sha="$(git rev-parse HEAD)"
backup_id="$(basename "$backup_path")"
python3 - "$commit_sha" "$backup_id" <<'PY'
import sqlite3
import sys
from datetime import datetime, timezone

connection = sqlite3.connect("/home/pi/mosasaurus/data/mosasaurus.db")
try:
    connection.execute(
        """
        INSERT INTO deployment_events (
            version, commit_sha, backup_id, migration_status,
            healthcheck_status, rollback_triggered, created_at
        ) VALUES (?, ?, ?, 'APPLIED', 'PASSED', 0, ?)
        """,
        (
            sys.argv[1],
            sys.argv[1],
            sys.argv[2],
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    connection.commit()
finally:
    connection.close()
PY
deployment_started=0
