#!/bin/sh
# REAL 활성화 승인 파일을 만듭니다(호스트에서 운영자가 수동 실행). 이 스크립트만으로는 실계좌가
# 열리지 않으며, Telegram `/approve_real <approval_id>` 2단계 확인과 DB 승인 레코드가 추가로 필요합니다.
# 사용: approve_real.sh <approval_id> <account_alias> [유효일수<=30] --i-understand
set -eu
SECRETS_DIR="${SECRETS_DIR:-/home/pi/.secrets/mosasaurus}"
ID="${1:-}"; ALIAS="${2:-}"; DAYS="${3:-30}"; CONFIRM="${4:-}"
[ "$CONFIRM" = "--i-understand" ] || { echo "실계좌 활성화 승인입니다. 마지막 인자에 --i-understand 가 필요합니다"; exit 1; }
case "$ID" in ""|*[!a-z0-9_-]*) echo "approval_id 는 소문자·숫자·-·_ 만 허용(4~64자)"; exit 1 ;; esac
case "$ALIAS" in ""|*[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*) echo "account_alias 에 계좌번호(숫자 8자리 이상)를 쓰지 마세요"; exit 1 ;; esac
case "$DAYS" in ""|*[!0-9]*) echo "유효일수는 숫자"; exit 1 ;; esac
[ "$DAYS" -ge 1 ] && [ "$DAYS" -le 30 ] || { echo "유효일수는 1~30"; exit 1; }
mkdir -p "$SECRETS_DIR/approvals"
now="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
exp="$(date -u -d "+$DAYS days" '+%Y-%m-%dT%H:%M:%S+00:00')"
umask 027
printf '{"approval_id": "%s", "approved_at": "%s", "expires_at": "%s", "account_alias": "%s"}\n' \
  "$ID" "$now" "$exp" "$ALIAS" > "$SECRETS_DIR/approvals/real_activation.json"
echo "생성: $SECRETS_DIR/approvals/real_activation.json (만료 $exp). 이제 Telegram 에서 /approve_real $ID 를 실행하세요."
