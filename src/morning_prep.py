"""`morning_prep`(평일 08:30): 200거래일 일봉 Upsert와 기업행위(분배락·분할) 감지(명세 4.2절).

KIS 수정주가는 최신 거래일 기준으로 과거를 소급 조정하므로, 저장된 시계열과 새로 받은 시계열의
조정계수 A = 수정종가/원주가 를 일자별로 비교해 소급 변경을 찾습니다. 감지되면 해당 **종목만**
`CORPORATE_ACTION_UNVERIFIED` 로 격리하고 `verified=0` 후보를 만듭니다(시스템 전체 SAFE_HALT 금지).
수정/원주가 차이만으로는 절대 `verified=1` 로 만들지 않습니다(검증은 `/ca_confirm` 또는 승인 일정).
"""

import sqlite3
from datetime import datetime, timezone
from typing import Callable, Sequence

from src.market_data import DailyCandle
from src.safety_flags import clear_flag, raise_flag

MAX_DAILY_MOVE = 0.30  # 4.3절 #2: 일간 수익률 ±30% 초과 미검증 이상치


def detect_retroactive_adjustment(
    stored: dict[str, float], fresh: Sequence[DailyCandle], *, tolerance: float
) -> tuple[bool, float | None, str | None]:
    """저장된 조정계수(`일자→A`)와 새 시계열의 A 를 비교한다.

    반환: (소급 조정 여부, 암시 조정계수 f = 새 A / 기존 A, 추정 ex_date). 겹치는 일자가 없으면 감지 불가(False).
    ex_date 는 마지막으로 바뀐 일자의 다음 거래일(= 바뀌지 않은 첫 날)로 추정한다.
    """
    ordered = sorted(fresh, key=lambda c: c.trade_date)
    changed: list[tuple[int, float]] = []
    for index, candle in enumerate(ordered):
        old = stored.get(candle.trade_date)
        if old is None or candle.raw_close is None:
            continue
        ratio = (candle.adjusted_close / candle.raw_close) / old
        if abs(ratio - 1.0) > tolerance:
            changed.append((index, ratio))
    if not changed:
        return False, None, None
    last_index, factor = changed[-1]
    ex_date = ordered[last_index + 1].trade_date if last_index + 1 < len(ordered) else ordered[last_index].trade_date
    return True, factor, ex_date


def series_has_outlier(candles: Sequence[DailyCandle]) -> bool:
    """수정종가 일간 변동이 ±30% 를 넘거나 가격이 비정상이면 True(`PRICE_SERIES_INVALID` 사유)."""
    ordered = sorted(candles, key=lambda c: c.trade_date)
    return any(abs(b.adjusted_close / a.adjusted_close - 1) > MAX_DAILY_MOVE for a, b in zip(ordered, ordered[1:]))


def _upsert(connection: sqlite3.Connection, candles: Sequence[DailyCandle], now: datetime) -> None:
    """최근 200거래일을 갱신한다(원주가 종가 필수)."""
    with connection:
        connection.executemany(
            """
            INSERT INTO daily_candles (ticker, trade_date, raw_open, raw_high, raw_low, raw_close,
                                       adjusted_close, volume, source, source_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'KIS', ?)
            ON CONFLICT(ticker, trade_date) DO UPDATE SET
                raw_open = excluded.raw_open, raw_high = excluded.raw_high, raw_low = excluded.raw_low,
                raw_close = excluded.raw_close, adjusted_close = excluded.adjusted_close,
                volume = excluded.volume, source_timestamp = excluded.source_timestamp
            """,
            [(c.ticker, c.trade_date, c.raw_open, c.raw_high, c.raw_low, c.raw_close, c.adjusted_close,
              c.volume, now.isoformat()) for c in candles])


def _candidate(connection: sqlite3.Connection, ticker: str, factor: float, ex_date: str,
               min_cash_factor: float, now: datetime) -> None:
    """미검증 기업행위 후보를 멱등하게 만든다(분배 추정액은 계수로부터 환산하지 않고 비워 둔다)."""
    action = "CASH_DISTRIBUTION" if min_cash_factor <= factor < 1.0 else "OTHER"
    with connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO corporate_actions (provider_event_id, ticker, action_type, ex_date,
                detected_factor, verified, source)
            VALUES (?, ?, ?, ?, ?, 0, 'DETECTED_MORNING_PREP')
            """,
            (f"DETECT:{ticker}:{ex_date}", ticker, action, ex_date, factor))


def create_morning_prep_handler(
    connection_factory: Callable[[], sqlite3.Connection],
    candle_source: Callable[[str], list[DailyCandle]],
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    notify: Callable[[str, str], None] = lambda severity, message: None,
    ratio_change_tolerance: float = 1e-6,
    min_cash_factor: float = 0.95,
    min_history: int = 200,
) -> Callable[[], None]:
    """활성 종목마다 일봉을 받아 검사하고 갱신합니다. 한 종목의 실패가 다른 종목을 막지 않습니다."""

    def handler() -> None:
        now = clock()
        connection = connection_factory()
        try:
            for row in connection.execute("SELECT ticker FROM universe_assets WHERE status = 'ACTIVE'").fetchall():
                ticker = row["ticker"]
                try:
                    fresh = candle_source(ticker)
                except Exception as error:  # noqa: BLE001 - 조회 실패는 해당 종목 격리
                    raise_flag(connection, "TICKER_BLOCK", "PRICE_SERIES_INVALID", now=now, ticker=ticker,
                               detail=f"FETCH_FAILED:{type(error).__name__}")
                    continue
                if len(fresh) < min_history or series_has_outlier(fresh) or any(c.raw_close is None for c in fresh):
                    raise_flag(connection, "TICKER_BLOCK", "PRICE_SERIES_INVALID", now=now, ticker=ticker,
                               detail="SERIES_INVALID")
                    with connection:
                        connection.execute("UPDATE universe_assets SET data_status = 'DATA_INVALID' WHERE ticker = ?",
                                           (ticker,))
                    continue
                stored = {r[0]: r[1] / r[2] for r in connection.execute(
                    "SELECT trade_date, adjusted_close, raw_close FROM daily_candles WHERE ticker = ? "
                    "AND raw_close IS NOT NULL", (ticker,))}
                retro, factor, ex_date = detect_retroactive_adjustment(stored, fresh, tolerance=ratio_change_tolerance)
                latest = max(fresh, key=lambda c: c.trade_date)
                latest_a_off = abs(latest.adjusted_close / latest.raw_close - 1.0) > ratio_change_tolerance
                _upsert(connection, fresh[-min_history:] if len(fresh) > min_history else fresh, now)
                clear_flag(connection, "TICKER_BLOCK", "PRICE_SERIES_INVALID", now=now, cleared_by="SYSTEM",
                           ticker=ticker)
                unverified = connection.execute(
                    "SELECT 1 FROM corporate_actions WHERE ticker = ? AND verified = 0 LIMIT 1", (ticker,)).fetchone()
                if retro or latest_a_off:
                    if retro:
                        _candidate(connection, ticker, factor, ex_date, min_cash_factor, now)
                    raise_flag(connection, "TICKER_BLOCK", "CORPORATE_ACTION_UNVERIFIED", now=now, ticker=ticker,
                               detail="RETROACTIVE_ADJUSTMENT" if retro else "LATEST_ADJ_FACTOR_NOT_ONE")
                    with connection:
                        connection.execute("UPDATE universe_assets SET data_status = 'DATA_INVALID' WHERE ticker = ?",
                                           (ticker,))
                    try:
                        notify("WARN", f"{ticker} 기업행위 의심: /ca_confirm 으로 확인 필요")
                    except Exception:  # noqa: BLE001
                        pass
                elif not unverified:
                    with connection:
                        connection.execute("UPDATE universe_assets SET data_status = 'VALID' WHERE ticker = ?",
                                           (ticker,))
        finally:
            connection.close()

    return handler
