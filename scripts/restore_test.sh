#!/bin/sh
set -eu

BACKUP_DIR="/home/pi/mosasaurus/data/backups"

latest="$(find "$BACKUP_DIR" -maxdepth 1 -name 'mosasaurus-*.db' -type f | sort | tail -n 1)"
if [ -z "${latest:-}" ]; then
  echo "No backup found"
  exit 1
fi

if [ ! -f "$latest.sha256" ]; then
  echo "Backup checksum is missing"
  exit 1
fi
sha256sum -c "$latest.sha256"

test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT HUP INT TERM
restore_path="$test_dir/restore-test.db"
cp "$latest" "$restore_path"

python3 - "$restore_path" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
try:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise SystemExit("Backup integrity check failed")
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SystemExit("Backup foreign-key check could not be enabled")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SystemExit("Backup foreign-key integrity check failed")
finally:
    connection.close()
PY

echo "Backup restore test passed"
