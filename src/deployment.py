from datetime import datetime
from datetime import timedelta, timezone


KST = timezone(timedelta(hours=9), name="KST")


def is_deployment_blocked(now: datetime) -> bool:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    kst_now = now.astimezone(KST)
    minute_of_day = kst_now.hour * 60 + kst_now.minute
    return 8 * 60 + 50 <= minute_of_day <= 15 * 60 + 25
