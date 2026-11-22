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


def main(argv: Sequence[str] | None = None) -> int:
    """KIS 호스트 토큰을 필요할 때 갱신하고 비밀 없는 결과만 출력합니다."""
    import argparse

    parser = argparse.ArgumentParser(description="Refresh the host KIS REST token.")
    parser.add_argument(
        "--trigger",
        choices=[trigger.value for trigger in RefreshTrigger],
        required=True,
        help="morning_prep for the weekday timer or recovery for boot recovery",
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
        return 2

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
