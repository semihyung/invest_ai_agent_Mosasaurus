#!/bin/sh
set -eu

BACKUP_DIR="/home/pi/mosasaurus/data/backups"

latest="$(find "$BACKUP_DIR" -maxdepth 1 -name 'mosasaurus-*.db' -type f | sort | tail -n 1)"
if [ -z "${latest:-}" ]; then
  echo "No backup found"
  exit 1
fi

python3 - "$latest" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
try:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise SystemExit("Backup integrity check failed")
finally:
    connection.close()
PY
