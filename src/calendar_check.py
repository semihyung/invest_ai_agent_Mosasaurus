"""휴장일 이중 검증(명세 4.3절 #6): `exchange_calendars`(XKRX)와 KIS 국내휴장일조회의 개장 여부가 다르면
`CALENDAR_MISMATCH`(SAFE_HALT, 두 소스가 다시 일치하면 자동 해제)를 올립니다.

`exchange_calendars` 는 pandas·numpy 를 끌고 오는 무거운 의존성이라 처음 쓸 때만 불러옵니다.
불러오지 못하면 개장 여부를 알 수 없으므로 fail-closed(불일치로 취급)합니다.
"""

import sqlite3
from datetime import date, datetime
from typing import Callable

from src.safety_flags import clear_flag, raise_flag

_CALENDAR = {}


def xkrx_is_open(day: date) -> bool:
    """XKRX(한국거래소) 세션 달력 기준 개장 여부. 라이브러리가 없으면 ImportError."""
    if "xkrx" not in _CALENDAR:
        import exchange_calendars  # 지연 import: 무거운 의존성(pandas·numpy)

        _CALENDAR["xkrx"] = exchange_calendars.get_calendar("XKRX")
    return bool(_CALENDAR["xkrx"].is_session(day.isoformat()))


def verify_calendar(
    connection: sqlite3.Connection,
    day: date,
    now: datetime,
    *,
    kis_is_open: Callable[[date], bool],
    xkrx_open: Callable[[date], bool] = xkrx_is_open,
) -> bool | None:
    """두 달력이 일치하면 개장 여부(True/False)를, 불일치·조회 불가면 None 을 돌려준다.

    None 이면 `CALENDAR_MISMATCH` 를 올려 주문을 막고(호출자는 개장일로 보지 않는다), 일치하면 플래그를 자동 해제한다.
    """
    try:
        kis, xkrx = bool(kis_is_open(day)), bool(xkrx_open(day))
    except Exception as error:  # noqa: BLE001 - 어느 쪽이든 알 수 없으면 fail-closed
        raise_flag(connection, "HALT", "CALENDAR_MISMATCH", now=now, detail=f"UNAVAILABLE:{type(error).__name__}")
        return None
    if kis != xkrx:
        raise_flag(connection, "HALT", "CALENDAR_MISMATCH", now=now, detail=f"KIS={kis},XKRX={xkrx}")
        return None
    clear_flag(connection, "HALT", "CALENDAR_MISMATCH", now=now, cleared_by="SYSTEM")
    return kis
