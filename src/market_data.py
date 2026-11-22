from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Sequence


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime.")
    return value.astimezone(timezone.utc)


class FreshnessStatus(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"


@dataclass(frozen=True)
class IntradaySnapshot:
    ticker: str
    bid: int
    ask: int
    current_price: int
    nav: float | None
    observed_at: datetime
    received_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.ticker, str) or not self.ticker.strip():
            raise ValueError("Ticker must be a non-empty string.")
        if any(isinstance(value, bool) or value <= 0 for value in (self.bid, self.ask, self.current_price)):
            raise ValueError("Bid, ask, and current price must be positive integers.")
        if self.nav is not None and (isinstance(self.nav, bool) or not float(self.nav) > 0):
            raise ValueError("NAV must be positive.")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "received_at", _utc(self.received_at, "received_at"))


def freshness_status(
    received_at: datetime,
    now: datetime,
    ttl_seconds: float,
) -> FreshnessStatus:
    received = _utc(received_at, "received_at")
    current = _utc(now, "now")
    if isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
        raise ValueError("TTL seconds must be a positive number.")
    if current - received > timedelta(seconds=ttl_seconds):
        return FreshnessStatus.STALE
    return FreshnessStatus.FRESH


def validate_quote(
    snapshot: IntradaySnapshot,
    now: datetime,
    *,
    ttl_seconds: float = 15,
    max_spread_ratio: float = 0.003,
) -> bool:
    if freshness_status(snapshot.received_at, now, ttl_seconds) is FreshnessStatus.STALE:
        return False
    if snapshot.ask < snapshot.bid:
        return False
    mid_price = (snapshot.bid + snapshot.ask) / 2
    if mid_price <= 0:
        return False
    spread_ratio = (snapshot.ask - snapshot.bid) / mid_price
    return spread_ratio <= max_spread_ratio


def validate_nav_deviation(
    snapshot: IntradaySnapshot,
    now: datetime,
    *,
    region: str,
    ttl_seconds: float = 60,
) -> bool:
    if freshness_status(snapshot.received_at, now, ttl_seconds) is FreshnessStatus.STALE:
        return False
    if snapshot.nav is None:
        return False
    limit = 0.005 if region == "KR" else 0.012 if region == "US" else None
    if limit is None:
        raise ValueError("Region must be KR or US.")
    quote = snapshot.current_price
    deviation = abs((quote - snapshot.nav) / snapshot.nav)
    return deviation <= limit


@dataclass(frozen=True)
class DailyCandle:
    ticker: str
    trade_date: str
    adjusted_close: float
    volume: int

    def __post_init__(self) -> None:
        if not isinstance(self.ticker, str) or not self.ticker.strip():
            raise ValueError("Ticker must be a non-empty string.")
        if (
            isinstance(self.adjusted_close, bool)
            or not isinstance(self.adjusted_close, (int, float))
            or self.adjusted_close <= 0
        ):
            raise ValueError("Adjusted close must be positive.")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume < 0:
            raise ValueError("Volume must be a non-negative integer.")


def validate_daily_history(
    candles: Sequence[DailyCandle],
    *,
    previous_trading_day: str,
) -> bool:
    if not candles:
        return False
    if len(candles) < 200:
        return False

    recent = list(candles[-200:])
    for candle in recent:
        if candle.trade_date > previous_trading_day:
            return False

    for previous, current in zip(recent, recent[1:]):
        if current.adjusted_close / previous.adjusted_close - 1 > 0.30:
            return False
        if current.adjusted_close / previous.adjusted_close - 1 < -0.30:
            return False
    return True
