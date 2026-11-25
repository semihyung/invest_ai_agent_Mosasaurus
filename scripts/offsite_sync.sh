#!/bin/sh
# 오프사이트 백업 복사 (감사 3번): data/backups 의 DB 백업을 SD카드 밖(USB·NAS 등 마운트된 폴더)으로 복사합니다.
# DB 와 백업이 같은 SD카드에만 있으면 카드 고장 한 번에 주문 이력·TWR/HWM·승인 기록이 모두 사라집니다.
#
# 대상은 호스트 환경 파일(`host.env`)의 OFFSITE_TARGET 입니다(마운트된 쓰기 가능 폴더).
# 결과는 run/offsite_status.json 에 원자적으로 기록되고, 앱이 읽어 실패·정체를 텔레그램으로 알립니다.
# 복사본은 SHA-256 으로 원본과 대조한 뒤에만 확정합니다(.part → 최종 이름).
set -eu

REPO_DIR="${REPO_DIR:-/home/pi/mosasaurus}"
RUN_DIR="${HOST_RUN_DIR:-$REPO_DIR/run}"
BACKUP_DIR="${BACKUP_DIR:-$REPO_DIR/data/backups}"
TARGET="${OFFSITE_TARGET:-}"
mkdir -p "$RUN_DIR"

write_status() {  # $1 = true|false, $2 = 사유 코드(영문 대문자·숫자·_)
  tmp="$(mktemp "$RUN_DIR/offsite_status.XXXXXX")"
  printf '{"ok": %s, "detail": "%s", "updated_at": "%s"}\n' \
    "$1" "$2" "$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')" > "$tmp"
  chmod 644 "$tmp"   # 컨테이너(다른 사용자)가 읽기 전용으로 읽는다. 비밀값 없음.
  mv "$tmp" "$RUN_DIR/offsite_status.json"
}

[ -n "$TARGET" ] || { write_status false OFFSITE_TARGET_NOT_SET; echo "OFFSITE_TARGET 이 설정되지 않았습니다"; exit 1; }
{ [ -d "$TARGET" ] && [ -w "$TARGET" ]; } || { write_status false TARGET_UNAVAILABLE; echo "대상 폴더를 쓸 수 없습니다: $TARGET"; exit 1; }
ls "$BACKUP_DIR"/mosasaurus-*.db >/dev/null 2>&1 || { write_status false NO_BACKUPS; echo "복사할 백업이 없습니다"; exit 1; }

copied=0
for source in "$BACKUP_DIR"/mosasaurus-*.db; do
  name="$(basename "$source")"
  [ -f "$TARGET/$name" ] && continue
  cp "$source" "$TARGET/$name.part"
  want="$(sha256sum "$source" | cut -d' ' -f1)"
  got="$(sha256sum "$TARGET/$name.part" | cut -d' ' -f1)"
  if [ "$want" != "$got" ]; then
    rm -f "$TARGET/$name.part"
    write_status false CHECKSUM_MISMATCH
    echo "복사본 체크섬이 다릅니다: $name"
    exit 1
  fi
  mv "$TARGET/$name.part" "$TARGET/$name"
  [ -f "$source.sha256" ] && cp "$source.sha256" "$TARGET/$name.sha256"
  copied=$((copied + 1))
done

write_status true "COPIED_$copied"
echo "오프사이트 복사 완료: 새 파일 $copied 개"
