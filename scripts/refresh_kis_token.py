import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.kis_client import KisTokenCache, RefreshTrigger, TokenRefreshPolicy, should_refresh_access_token


def main() -> int:
    cache_path = Path(
        os.environ.get(
            "TOKEN_CACHE_PATH",
            "/home/pi/.secrets/mosasaurus/token-cache/token.json",
        )
    )
    cache = KisTokenCache(cache_path)
    token = cache.load()
    policy = TokenRefreshPolicy(
        proactive_refresh_min_age_hours=6,
        hard_guard_before_expiry_hours=1,
        min_refresh_cooldown_seconds=65,
    )
    now = datetime.now(timezone.utc)
    trigger = (
        RefreshTrigger.MORNING_PREP
        if now.weekday() < 5
        else RefreshTrigger.RECOVERY
    )
    if not should_refresh_access_token(
        token,
        trigger=trigger,
        now=now,
        policy=policy,
        last_request_at=None,
    ):
        print(json.dumps({"action": "reuse", "expires_at": token.expires_at.isoformat() if token else None}))
        return 0
    print(json.dumps({"action": "refresh_required", "trigger": trigger.value}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
