#!/bin/sh
set -eu

REPO_DIR="/home/pi/mosasaurus"
DB_PATH="$REPO_DIR/data/mosasaurus.db"
BACKUP_DIR="$REPO_DIR/data/backups"

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB_PATH" ]; then
  echo "Database does not exist: $DB_PATH"
  exit 1
fi

timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
backup_path="$BACKUP_DIR/mosasaurus-$timestamp.db"

python3 - "$DB_PATH" "$backup_path" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1])
backup = sqlite3.connect(sys.argv[2])
try:
    source.backup(backup)
finally:
    backup.close()
    source.close()
PY

echo "$backup_path"
