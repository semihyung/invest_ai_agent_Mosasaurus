#!/bin/sh
# 네트워크 감시 (명세 3.3절 4항): DNS 와 KIS HTTPS 를 분리 점검하고, 실패가 이어지면
# 지수 백오프로 재연결하며, 재부팅은 1시간 1회·장중 금지 규칙을 src.net_watchdog 에 위임합니다.
# systemd 타이머가 1분마다 실행합니다.
set -eu

REPO_DIR="${REPO_DIR:-/home/pi/mosasaurus}"
RUN_DIR="${HOST_RUN_DIR:-$REPO_DIR/run}"
# 기본값은 봇이 실제로 쓰는 모의투자(PAPER) 서버다(deployment.environment). REAL 로 바꾸면 service 의
# Environment= 로 KIS_HOST·KIS_URL 도 함께 바꾼다. 예전 기본값(실전 서버)은 모의 서버 장애를 못 잡았다.
KIS_HOST="${KIS_HOST:-openapivts.koreainvestment.com}"
KIS_URL="${KIS_URL:-https://openapivts.koreainvestment.com:29443}"
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
  # 서버가 어떤 HTTP 응답이든 돌려주면 네트워크는 정상이다. `curl -f` 는 루트 주소의 404 같은 응답을 실패로 보아
  # 멀쩡한 네트워크에서 장외 재부팅을 반복하게 만들 수 있다. 연결·TLS 실패일 때만 코드가 000 이다.
  code="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 "$KIS_URL" 2>/dev/null || true)"
  case "$code" in
    ""|000) echo "KIS HTTPS 점검 실패"; return 1 ;;
  esac
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
