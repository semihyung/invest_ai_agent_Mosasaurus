"""네트워크 단절 시 취할 조치(재연결·재부팅)를 결정합니다(명세 3.3절 4항).

재부팅 루프를 막기 위해 재부팅은 최대 `max_reboots_per_hour`(기본 1)회로 제한하고,
개장일 `08:50–15:45` 장중에는 재부팅하지 않고 네트워크 인터페이스 재시작만 허용합니다.
쉘 스크립트 `scripts/net_watchdog.sh` 가 이 모듈의 CLI 로 결정을 받아 실행합니다.
"""

import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), name="KST")
NONE, RESTART_INTERFACE, REBOOT = "NONE", "RESTART_INTERFACE", "REBOOT"


def backoff_seconds(failures: int) -> int:
    """연속 실패 횟수에 대한 지수 백오프(1,2,4,… 최대 300초)입니다."""
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        raise ValueError("failures must be a non-negative integer.")
    return min(300, 2 ** max(failures - 1, 0)) if failures else 0


def in_market_hours(now: datetime) -> bool:
    """개장일 장중·정산 시간(평일 08:50–15:45 KST)인지 반환합니다(휴장일은 구분하지 않아 보수적)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    kst_now = now.astimezone(KST)
    minute = kst_now.hour * 60 + kst_now.minute
    return kst_now.weekday() <= 4 and 8 * 60 + 50 <= minute <= 15 * 60 + 45


def decide_action(
    *,
    failures: int,
    now: datetime,
    last_reboot_at: datetime | None,
    restart_attempts_before_reboot: int = 5,
    max_reboots_per_hour: int = 1,
) -> str:
    """연속 실패 횟수·현재 시각·마지막 재부팅 시각으로 NONE/RESTART_INTERFACE/REBOOT 를 고릅니다."""
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        raise ValueError("failures must be a non-negative integer.")
    if now.tzinfo is None or (last_reboot_at is not None and last_reboot_at.tzinfo is None):
        raise ValueError("Timestamps must be timezone-aware.")
    if failures == 0:
        return NONE
    if failures < restart_attempts_before_reboot:
        return RESTART_INTERFACE
    if in_market_hours(now):
        return RESTART_INTERFACE  # 장중 재부팅 금지
    if max_reboots_per_hour <= 0:
        return RESTART_INTERFACE
    if last_reboot_at is not None and now - last_reboot_at < timedelta(hours=1) / max_reboots_per_hour:
        return RESTART_INTERFACE  # 1시간 1회 제한
    return REBOOT


def _main(argv: list[str]) -> int:
    """`decide <failures> [last_reboot_iso]` → 조치 이름을 출력합니다."""
    if len(argv) < 2 or argv[0] != "decide":
        print("usage: python -m src.net_watchdog decide <failures> [last_reboot_iso]", file=sys.stderr)
        return 2
    last = datetime.fromisoformat(argv[2]) if len(argv) > 2 and argv[2] else None
    print(decide_action(
        failures=int(argv[1]), now=datetime.now(timezone.utc), last_reboot_at=last))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
