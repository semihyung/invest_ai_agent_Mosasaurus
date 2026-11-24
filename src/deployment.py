"""배포 금지 시간(명세 3.4절 `deployment.freeze_window`)과 배포 요청 검증을 판정합니다.

쉘 스크립트(`scripts/mosasaurus-deploy`, `scripts/safe_deploy.sh`)는 이 모듈의 CLI
(`python -m src.deployment check`)를 호출해 같은 규칙을 쓰므로, 시간 규칙이 한 곳에만 존재합니다.
"""

import re
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), name="KST")

# (시작, 종료) 분 단위 KST. 월~금: 토큰 갱신·장 준비·장중·종가 단일가·마감 대조 시간.
_WEEKDAY_FREEZE = (8 * 60 + 20, 15 * 60 + 45)
_SATURDAY_FREEZE = (9 * 60 + 50, 10 * 60 + 30)   # 토요일 10:00 주간 프리뷰
_SUNDAY_FREEZE = (2 * 60 + 50, 3 * 60 + 30)      # 일요일 03:00 주간 백업
_SHA = re.compile(r"[0-9a-f]{40}")


def _minute_of_day(now: datetime) -> tuple[int, int]:
    """(요일 0=월, KST 하루 중 분) 을 반환합니다."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    kst_now = now.astimezone(KST)
    return kst_now.weekday(), kst_now.hour * 60 + kst_now.minute


def is_deployment_blocked(
    now: datetime,
    *,
    post_close_required: bool = False,
    post_close_done: bool = True,
    weekly_maintenance_done: bool = True,
) -> bool:
    """지금 배포를 하면 안 되는지 반환합니다(차단이면 True → 지연 배포 큐로).

    - 평일 08:20–15:45 와 토요일 09:50–10:30, 일요일 02:50–03:30 은 항상 차단합니다.
    - 금지 시간의 종료는 고정 시각이 아니라, 평일 15:45 이후에도 당일 마감 대조 완료 신호
      (`post_close_done`)가 있어야 풀립니다(개장일에만 `post_close_required=True`).
      일요일 백업 구간도 `weekly_maintenance_done` 신호가 있어야 풀립니다.
    """
    weekday, minute = _minute_of_day(now)
    if weekday <= 4:
        start, end = _WEEKDAY_FREEZE
        if start <= minute <= end:
            return True
        return post_close_required and minute > end and not post_close_done
    if weekday == 5:
        start, end = _SATURDAY_FREEZE
        return start <= minute <= end
    start, end = _SUNDAY_FREEZE
    if start <= minute <= end:
        return True
    return minute > end and minute <= end + 90 and not weekly_maintenance_done


def validate_deploy_request(
    commit_sha: str,
    *,
    is_ancestor_of_main: bool,
    signature_valid: bool,
    ci_passed: bool,
    actor_allowed: bool = True,
) -> None:
    """배포 래퍼가 확인한 사실을 모아 하나라도 어긋나면 PermissionError 로 거부합니다.

    - 커밋 SHA 는 40자리 소문자 16진수여야 합니다(쉘 인젝션 방지).
    - `origin/main` 의 조상이어야 하고, 허용된 운영자 키로 서명되어야 하며,
      같은 SHA 의 클라우드 테스트가 성공해야 합니다.
    """
    if not isinstance(commit_sha, str) or _SHA.fullmatch(commit_sha) is None:
        raise PermissionError("Commit SHA must be a 40-character lowercase hex string.")
    checks = (
        ("commit is not an ancestor of origin/main", is_ancestor_of_main),
        ("commit signature is not from an allowed signer", signature_valid),
        ("cloud tests did not pass for this commit", ci_passed),
        ("actor is not allowed to deploy", actor_allowed),
    )
    for message, passed in checks:
        if passed is not True:
            raise PermissionError(f"Deployment refused: {message}.")


def _main(argv: list[str]) -> int:
    """`check [--post-close-required] [--post-close-done] [--maintenance-done]` CLI: 0=배포 가능, 1=차단."""
    if not argv or argv[0] != "check":
        print("usage: python -m src.deployment check [flags]", file=sys.stderr)
        return 2
    blocked = is_deployment_blocked(
        datetime.now(timezone.utc),
        post_close_required="--post-close-required" in argv,
        post_close_done="--post-close-done" in argv,
        weekly_maintenance_done="--maintenance-done" in argv,
    )
    print("BLOCKED" if blocked else "ALLOWED")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
