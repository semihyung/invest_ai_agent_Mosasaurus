import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.kis_client import (
    KisApiError,
    KisTokenCache,
    KisTokenIssuer,
    KisTokenRefreshService,
    RefreshTrigger,
    TokenRefreshPolicy,
)


def _run_dir() -> Path:
    """호스트↔컨테이너 상태 교환 디렉터리(`HOST_RUN_DIR`)를 반환합니다."""
    return Path(os.environ.get("HOST_RUN_DIR", "/home/pi/mosasaurus/run"))


def _token_policy_from_config(config: dict[str, Any]) -> TokenRefreshPolicy:
    """rules.yaml의 토큰 갱신 정책을 검증해 정책 객체로 변환합니다."""
    account = config.get("account")
    token_policy = account.get("token_policy") if isinstance(account, dict) else None
    if not isinstance(token_policy, dict):
        raise ValueError("Configuration is missing account.token_policy.")
    try:
        return TokenRefreshPolicy(**token_policy)
    except TypeError as exc:
        raise ValueError("Configuration has an invalid account.token_policy.") from exc


def record_refresh_status(run_dir: Path, *, ok: bool, now: datetime, alert_threshold: int = 3) -> dict:
    """갱신 성공·실패를 `run/token_refresh_status.json` 에 원자적으로 기록합니다.

    연속 실패가 `alert_threshold` 회 이상이면 `alert=true` 로 표시하고, 앱이 이 파일을 읽어
    Telegram CRITICAL 경보를 보냅니다. 비밀값은 기록하지 않습니다.
    """
    import tempfile

    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "token_refresh_status.json"
    failures = 0
    try:
        failures = int(json.loads(path.read_text(encoding="utf-8")).get("consecutive_failures", 0))
    except (OSError, ValueError, TypeError):
        failures = 0
    failures = 0 if ok else failures + 1
    payload = {
        "consecutive_failures": failures,
        "alert": failures >= alert_threshold,
        "updated_at": now.astimezone(timezone.utc).isoformat(),
    }
    descriptor, temporary = tempfile.mkstemp(dir=run_dir, prefix="token_refresh_status.")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o644)  # mkstemp 는 0600: 앱(컨테이너, 다른 uid)이 읽어 경보를 보내야 한다(비밀값 없음)
    os.replace(temporary, path)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """KIS 호스트 토큰을 필요할 때 갱신하고 비밀 없는 결과만 출력합니다."""
    import argparse

    parser = argparse.ArgumentParser(description="Refresh the host KIS REST token.")
    parser.add_argument(
        "--trigger",
        choices=[trigger.value for trigger in RefreshTrigger],
        required=True,
        help="morning_prep for the weekday 08:25 timer; recovery for the hourly/boot timers (rule B)",
    )
    arguments = parser.parse_args(argv)

    try:
        app_key = os.environ.get("KIS_APP_KEY")
        app_secret = os.environ.get("KIS_APP_SECRET")
        if not app_key or not app_secret:
            raise ValueError("KIS_APP_KEY and KIS_APP_SECRET must be set in the host environment.")

        config = load_config()
        policy = _token_policy_from_config(config)
        cache = KisTokenCache()
        service = KisTokenRefreshService(
            cache,
            KisTokenIssuer(app_key, app_secret),
            policy,
        )
        result = service.refresh_if_needed(
            trigger=RefreshTrigger(arguments.trigger),
            now=datetime.now(timezone.utc),
        )
    except (KisApiError, OSError, ValueError) as exc:
        print(json.dumps({"action": "error", "error": str(exc)}), file=sys.stderr)
        record_refresh_status(_run_dir(), ok=False, now=datetime.now(timezone.utc))
        return 2
    record_refresh_status(_run_dir(), ok=True, now=datetime.now(timezone.utc))

    print(
        json.dumps(
            {
                "action": result.action,
                "expires_at": (
                    result.expires_at.isoformat() if result.expires_at is not None else None
                ),
                "trigger": arguments.trigger,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
