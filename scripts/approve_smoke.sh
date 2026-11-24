#!/bin/sh
# 1주 스모크 테스트 세션 승인 파일을 만듭니다(최대 60분). REAL 활성화가 먼저 필요합니다.
# 사용: approve_smoke.sh <approval_id> [유효분<=60] --i-understand
set -eu
SECRETS_DIR="${SECRETS_DIR:-/home/pi/.secrets/mosasaurus}"
ID="${1:-}"; MINUTES="${2:-60}"; CONFIRM="${3:-}"
[ "$CONFIRM" = "--i-understand" ] || { echo "마지막 인자에 --i-understand 가 필요합니다"; exit 1; }
case "$ID" in ""|*[!a-z0-9_-]*) echo "approval_id 는 소문자·숫자·-·_ 만 허용(4~64자)"; exit 1 ;; esac
case "$MINUTES" in ""|*[!0-9]*) echo "유효분은 숫자"; exit 1 ;; esac
[ "$MINUTES" -ge 1 ] && [ "$MINUTES" -le 60 ] || { echo "유효분은 1~60"; exit 1; }
[ -f "$SECRETS_DIR/approvals/real_activation.json" ] || { echo "REAL 활성화 승인 파일이 먼저 필요합니다"; exit 1; }
now="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
exp="$(date -u -d "+$MINUTES minutes" '+%Y-%m-%dT%H:%M:%S+00:00')"
umask 027
printf '{"approval_id": "%s", "approved_at": "%s", "expires_at": "%s", "account_alias": "smoke-session"}\n' \
  "$ID" "$now" "$exp" > "$SECRETS_DIR/approvals/smoke_session.json"
echo "생성: $SECRETS_DIR/approvals/smoke_session.json (만료 $exp). 이제 Telegram 에서 /approve_smoke $ID 를 실행하세요."
