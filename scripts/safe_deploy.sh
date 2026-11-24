#!/bin/sh
# 안전 배포 10단계 (명세 3.4절). 운영 실행본은 root 소유 /usr/local/sbin/ 의 승인본이며,
# 이 파일은 저장소 원본입니다. 사용자가 명시적으로 요청하기 전에는 실행하지 않습니다.
#
# 사용: safe_deploy.sh <40자리 커밋 SHA>
#   종료 코드 0 = 배포 성공 또는 지연 배포 큐에 기록, 1 = 실패(자동 롤백 시도 후)
set -eu

REPO_DIR="${REPO_DIR:-/home/pi/mosasaurus}"
RUN_DIR="${HOST_RUN_DIR:-$REPO_DIR/run}"
DB_PATH="${DB_PATH_HOST:-$REPO_DIR/data/mosasaurus.db}"
COMMIT_SHA="${1:-}"

case "$COMMIT_SHA" in
  *[!0-9a-f]*|"") echo "배포 대상 커밋 SHA(40자리 소문자 16진수)가 필요합니다"; exit 1 ;;
esac
[ "${#COMMIT_SHA}" -eq 40 ] || { echo "커밋 SHA 길이가 올바르지 않습니다"; exit 1; }

cd "$REPO_DIR"
mkdir -p "$RUN_DIR"

# ---- 1단계: 금지 시간이면 지연 배포 큐에 넣고 정상 종료 ------------------------------
flags=""
[ -f "$RUN_DIR/post_close_required" ] && flags="$flags --post-close-required"
[ -f "$RUN_DIR/post_close_done" ] && flags="$flags --post-close-done"
[ -f "$RUN_DIR/weekly_maintenance_done" ] && flags="$flags --maintenance-done"
# shellcheck disable=SC2086
if ! python3 -m src.deployment check $flags >/dev/null; then
  printf '%s\n' "$COMMIT_SHA" > "$RUN_DIR/deploy_queue"
  echo "배포 금지 시간: 커밋 $COMMIT_SHA 을(를) 지연 배포 큐에 기록했습니다"
  exit 0
fi

# ---- 2단계: 신규 주문 생성 잠금(DB) --------------------------------------------------
python3 - "$DB_PATH" <<'PY'
import sqlite3, sys
from datetime import datetime, timezone
c = sqlite3.connect(sys.argv[1])
try:
    now = datetime.now(timezone.utc).isoformat()
    c.execute("""INSERT INTO system_state ("key", value, updated_at) VALUES ('scheduler_order_lock','true',?)
                 ON CONFLICT("key") DO UPDATE SET value='true', updated_at=excluded.updated_at""", (now,))
    c.commit()
finally:
    c.close()
PY

# ---- 3단계: 앱이 실행 중인 상태에서 활성 주문·SAFE_HALT 원인 점검 ------------------------
blocked="$(python3 - "$DB_PATH" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
try:
    active = c.execute("SELECT COUNT(*) FROM orders WHERE status IN ('SUBMITTING','PENDING_CONFIRMATION','CANCEL_PENDING')").fetchone()[0]
    pending_halt = c.execute("SELECT COUNT(*) FROM safety_flags WHERE flag_type='HALT' AND code='PENDING_CONFIRMATION' AND cleared_at IS NULL").fetchone()[0]
    print("yes" if active or pending_halt else "no")
finally:
    c.close()
PY
)"
if [ "$blocked" = "yes" ]; then
  printf '%s\n' "$COMMIT_SHA" > "$RUN_DIR/deploy_queue"
  echo "전송 여부가 불명확한 주문이 있어 배포를 큐로 되돌립니다"
  exit 0
fi

previous_commit="$(git rev-parse HEAD)"
deployment_started=0
backup_path=""
backward_incompatible=0

rollback_deployment() {
  status=$?
  if [ "$status" -ne 0 ] && [ "$deployment_started" -eq 1 ]; then
    echo "배포 실패: 이전 버전으로 롤백합니다"
    docker compose stop -t 60 || true
    # ---- 9단계: 역호환 불가 마이그레이션이 적용됐으면 이미지 롤백 전에 백업을 복원 ----
    if [ "$backward_incompatible" -eq 1 ] && [ -n "$backup_path" ]; then
      echo "backward_compatible: false 마이그레이션 → 백업 복원"
      cp "$backup_path" "$DB_PATH"
      rm -f "$DB_PATH-wal" "$DB_PATH-shm"
    fi
    git checkout --detach "$previous_commit" || true
    docker tag mosasaurus-agent:previous mosasaurus-agent:latest 2>/dev/null || true
    docker compose up -d --no-build --remove-orphans || true
    python3 - "$DB_PATH" "$COMMIT_SHA" "$(basename "${backup_path:-none}")" <<'PY' || true
import sqlite3, sys
from datetime import datetime, timezone
sys.path.insert(0, ".")
c = sqlite3.connect(sys.argv[1])
try:
    now = datetime.now(timezone.utc)
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("""INSERT INTO deployment_events (version, commit_sha, deploy_status, backup_id,
                 migration_status, healthcheck_status, rollback_triggered, created_at)
                 VALUES (?, ?, 'ROLLED_BACK', ?, 'REVERTED', 'FAILED', 1, ?)""",
              (sys.argv[2], sys.argv[2], sys.argv[3], now.isoformat()))
    c.commit()
finally:
    c.close()
# ---- 10단계: DEPLOY_ROLLBACK 원인 활성화(운영자 /clear_halt 전까지 신규 주문 금지) ----
from src.db import connect_database
from src.safety_flags import raise_flag
conn = connect_database(sys.argv[1])
try:
    raise_flag(conn, "HALT", "DEPLOY_ROLLBACK", now=datetime.now(timezone.utc), detail="배포 롤백")
finally:
    conn.close()
PY
  fi
}
trap rollback_deployment EXIT

# ---- 4단계: 앱 정상 종료(SIGTERM, 60초 후 SIGKILL) ---------------------------------
docker compose stop -t 60

# ---- 5단계: 온라인 백업 + SHA-256 --------------------------------------------------
backup_path="$("$REPO_DIR/scripts/backup_db.sh")"
if [ ! -f "$backup_path.sha256" ]; then
  echo "DB 백업 체크섬이 만들어지지 않았습니다"
  exit 1
fi

# ---- 6단계: 이전 이미지 보존 → 새 소스 빌드 → 백업 사본으로 마이그레이션 dry-run ---------
docker tag mosasaurus-agent:latest mosasaurus-agent:previous 2>/dev/null || true
git fetch origin main
deployment_started=1
git checkout --detach "$COMMIT_SHA"
docker compose build

dry_run_dir="$(mktemp -d)"
cp "$backup_path" "$dry_run_dir/dryrun.db"
python3 - "$dry_run_dir/dryrun.db" <<'PY'
import sys
sys.path.insert(0, ".")
from src.db import initialize_database
initialize_database(sys.argv[1]).close()
PY
rm -rf "$dry_run_dir"

# 새로 적용될 마이그레이션 중 역호환 불가(헤더 없음 포함)가 있는지 미리 판정합니다.
backward_incompatible="$(python3 - "$DB_PATH" <<'PY'
import sqlite3, sys
from pathlib import Path
c = sqlite3.connect(sys.argv[1])
try:
    applied = {r[0] for r in c.execute("SELECT version FROM schema_migrations")}
finally:
    c.close()
result = 0
for path in sorted(Path("migrations").glob("*.sql")):
    if path.stem in applied:
        continue
    first = path.read_text(encoding="utf-8").splitlines()[0].strip().lower()
    if first != "-- backward_compatible: true":
        result = 1
print(result)
PY
)"

# ---- 7단계: 마이그레이션 적용(앱 기동 시) + 헬스체크 + 무결성 -------------------------
docker compose up -d --remove-orphans
healthy=false
attempt=0
while [ "$attempt" -lt 18 ]; do        # 최대 180초
  status="$(docker inspect mosasaurus-agent --format '{{.State.Health.Status}}' 2>/dev/null || true)"
  if [ "$status" = "healthy" ]; then healthy=true; break; fi
  attempt=$((attempt + 1))
  sleep 10
done
if [ "$healthy" != "true" ]; then
  echo "배포 후 헬스체크 실패"
  exit 1
fi
python3 - "$DB_PATH" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
try:
    if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("배포 후 SQLite 무결성 검사 실패")
finally:
    c.close()
PY

# ---- 8단계: 새 버전 확정 + deployment_events 기록 ---------------------------------------
python3 - "$DB_PATH" "$COMMIT_SHA" "$(basename "$backup_path")" <<'PY'
import sqlite3, sys
from datetime import datetime, timezone
c = sqlite3.connect(sys.argv[1])
try:
    c.execute("""INSERT INTO deployment_events (version, commit_sha, deploy_status, backup_id,
                 migration_status, healthcheck_status, rollback_triggered, created_at)
                 VALUES (?, ?, 'DEPLOYED', ?, 'APPLIED', 'PASSED', 0, ?)""",
              (sys.argv[2], sys.argv[2], sys.argv[3], datetime.now(timezone.utc).isoformat()))
    c.commit()
finally:
    c.close()
PY
rm -f "$RUN_DIR/deploy_queue"
deployment_started=0
echo "배포 완료: $COMMIT_SHA (신규 주문 잠금은 앱의 boot_recovery 대조 성공 후 자동 해제됩니다)"
