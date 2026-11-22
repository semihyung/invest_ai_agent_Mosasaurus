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

./scripts/backup_db.sh

git fetch origin main
git reset --hard origin/main

docker compose up -d --build --remove-orphans
