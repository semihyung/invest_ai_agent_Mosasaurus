#!/bin/sh
# 네트워크 감시 (명세 3.3절 4항): DNS 와 KIS HTTPS 를 분리 점검하고, 실패가 이어지면
# 지수 백오프로 재연결하며, 재부팅은 1시간 1회·장중 금지 규칙을 src.net_watchdog 에 위임합니다.
# systemd 타이머가 1분마다 실행합니다.
set -eu

REPO_DIR="${REPO_DIR:-/home/pi/mosasaurus}"
RUN_DIR="${HOST_RUN_DIR:-$REPO_DIR/run}"
KIS_HOST="${KIS_HOST:-openapi.koreainvestment.com}"          # 환경(PAPER/REAL)에 맞게 service 에서 지정
KIS_URL="${KIS_URL:-https://openapi.koreainvestment.com:9443}"
IFACE_RESTART_CMD="${IFACE_RESTART_CMD:-systemctl restart NetworkManager}"
mkdir -p "$RUN_DIR"
failure_file="$RUN_DIR/net_failures"
reboot_file="$RUN_DIR/last_reboot_at"

check_network() {
  dns_ok=1
  for resolver in 1.1.1.1 8.8.8.8; do
    if nslookup "$KIS_HOST" "$resolver" >/dev/null 2>&1; then dns_ok=0; break; fi
  done
  [ "$dns_ok" -eq 0 ] || { echo "DNS 점검 실패"; return 1; }
  curl -fsS --connect-timeout 5 --max-time 10 "$KIS_URL" >/dev/null 2>&1 \
    || { echo "KIS HTTPS 점검 실패"; return 1; }
  return 0
}

if check_network; then
  rm -f "$failure_file"
  echo "네트워크 정상"
  exit 0
fi

failures=0
[ -f "$failure_file" ] && failures="$(cat "$failure_file")"
failures=$((failures + 1))
printf '%s\n' "$failures" > "$failure_file"

last_reboot=""
[ -f "$reboot_file" ] && last_reboot="$(cat "$reboot_file")"
cd "$REPO_DIR"
action="$(python3 -m src.net_watchdog decide "$failures" "$last_reboot")"
case "$action" in
  RESTART_INTERFACE)
    echo "연속 실패 $failures 회: 네트워크 인터페이스를 재시작합니다"
    sleep "$(python3 -c "from src.net_watchdog import backoff_seconds; print(backoff_seconds($failures))")"
    # shellcheck disable=SC2086
    $IFACE_RESTART_CMD || true
    ;;
  REBOOT)
    echo "연속 실패 $failures 회: 재부팅 조건(장외·1시간 1회)을 충족해 재부팅합니다"
    date -u '+%Y-%m-%dT%H:%M:%S+00:00' > "$reboot_file"
    systemctl reboot
    ;;
  *) ;;
esac
exit 1
