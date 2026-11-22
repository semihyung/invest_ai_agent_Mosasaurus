from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import math
from typing import Any, Mapping, Sequence


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
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (self.bid, self.ask, self.current_price)
        ):
            raise ValueError("Bid, ask, and current price must be positive integers.")
        if self.nav is not None and (
            isinstance(self.nav, bool)
            or not isinstance(self.nav, (int, float))
            or not math.isfinite(self.nav)
            or self.nav <= 0
        ):
            raise ValueError("NAV must be positive.")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "received_at", _utc(self.received_at, "received_at"))
        if self.received_at < self.observed_at:
            raise ValueError("received_at must not precede observed_at.")


def freshness_status(
    received_at: datetime,
    now: datetime,
    ttl_seconds: float,
) -> FreshnessStatus:
    received = _utc(received_at, "received_at")
    current = _utc(now, "now")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, (int, float))
        or not math.isfinite(ttl_seconds)
        or ttl_seconds <= 0
    ):
        raise ValueError("TTL seconds must be a positive number.")
    if received > current:
        raise ValueError("received_at must not be in the future.")
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
    raw_open: float | None = None
    raw_high: float | None = None
    raw_low: float | None = None
    raw_close: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ticker, str) or not self.ticker.strip():
            raise ValueError("Ticker must be a non-empty string.")
        if not isinstance(self.trade_date, str):
            raise ValueError("trade_date must be an ISO date string.")
        try:
            datetime.strptime(self.trade_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("trade_date must be an ISO date string.") from exc
        if (
            isinstance(self.adjusted_close, bool)
            or not isinstance(self.adjusted_close, (int, float))
            or not math.isfinite(self.adjusted_close)
            or self.adjusted_close <= 0
        ):
            raise ValueError("Adjusted close must be positive.")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume < 0:
            raise ValueError("Volume must be a non-negative integer.")
        raw_prices = (self.raw_open, self.raw_high, self.raw_low, self.raw_close)
        for value in raw_prices:
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("Raw candle prices must be positive when provided.")


def parse_kis_nav_record(record: Mapping[str, Any]) -> float:
    if not isinstance(record, Mapping):
        raise TypeError("NAV record must be a mapping.")
    return _positive_number(record.get("nav"), "nav")


def snapshot_from_kis_records(
    ticker: str,
    quote_output: Mapping[str, Any],
    orderbook_output: Mapping[str, Any],
    *,
    nav: float | None,
    observed_at: datetime,
    received_at: datetime,
) -> IntradaySnapshot:
    if not isinstance(quote_output, Mapping) or not isinstance(orderbook_output, Mapping):
        raise TypeError("KIS quote and orderbook outputs must be mappings.")
    return IntradaySnapshot(
        ticker=ticker,
        bid=_positive_integer(orderbook_output.get("bidp1"), "bidp1"),
        ask=_positive_integer(orderbook_output.get("askp1"), "askp1"),
        current_price=_positive_integer(quote_output.get("stck_prpr"), "stck_prpr"),
        nav=nav,
        observed_at=observed_at,
        received_at=received_at,
    )


def daily_candles_from_kis(
    ticker: str,
    raw_rows: Sequence[Mapping[str, Any]],
    adjusted_rows: Sequence[Mapping[str, Any]],
) -> list[DailyCandle]:
    raw_by_date = _index_kis_daily_rows(raw_rows, "raw")
    adjusted_by_date = _index_kis_daily_rows(adjusted_rows, "adjusted")
    if raw_by_date.keys() != adjusted_by_date.keys():
        raise ValueError("KIS raw and adjusted histories have different trading dates.")

    candles = []
    for trade_date in sorted(raw_by_date):
        raw_row = raw_by_date[trade_date]
        adjusted_row = adjusted_by_date[trade_date]
        raw_volume = _nonnegative_integer(raw_row.get("acml_vol"), "acml_vol")
        adjusted_volume = _nonnegative_integer(
            adjusted_row.get("acml_vol"), "acml_vol"
        )
        if raw_volume != adjusted_volume:
            raise ValueError("KIS raw and adjusted histories have different volumes.")
        candles.append(
            DailyCandle(
                ticker=ticker,
                trade_date=trade_date,
                adjusted_close=_positive_number(
                    adjusted_row.get("stck_clpr"), "adjusted stck_clpr"
                ),
                volume=raw_volume,
                raw_open=_positive_number(raw_row.get("stck_oprc"), "stck_oprc"),
                raw_high=_positive_number(raw_row.get("stck_hgpr"), "stck_hgpr"),
                raw_low=_positive_number(raw_row.get("stck_lwpr"), "stck_lwpr"),
                raw_close=_positive_number(raw_row.get("stck_clpr"), "raw stck_clpr"),
            )
        )
    return candles


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
    dates = [candle.trade_date for candle in recent]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        return False
    for candle in recent:
        if candle.trade_date > previous_trading_day:
            return False

    for previous, current in zip(recent, recent[1:]):
        if current.adjusted_close / previous.adjusted_close - 1 > 0.30:
            return False
        if current.adjusted_close / previous.adjusted_close - 1 < -0.30:
            return False
    return True


def _index_kis_daily_rows(
    rows: Sequence[Mapping[str, Any]],
    price_basis: str,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise TypeError(f"KIS {price_basis} history must be a sequence.")
    indexed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError(f"KIS {price_basis} history contains an invalid row.")
        raw_date = row.get("stck_bsop_date")
        if not isinstance(raw_date, str) or len(raw_date) != 8 or not raw_date.isdigit():
            raise ValueError(f"KIS {price_basis} history has an invalid trade date.")
        trade_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
        if trade_date in indexed:
            raise ValueError(f"KIS {price_basis} history has a duplicate trade date.")
        indexed[trade_date] = row
    return indexed


def _positive_number(value: Any, field_name: str) -> float:
    number = _decimal(value, field_name)
    if number <= 0:
        raise ValueError(f"KIS field {field_name} must be positive.")
    converted = float(number)
    if not math.isfinite(converted):
        raise ValueError(f"KIS field {field_name} is outside the supported range.")
    return converted


def _positive_integer(value: Any, field_name: str) -> int:
    number = _decimal(value, field_name)
    if number <= 0 or number != number.to_integral_value():
        raise ValueError(f"KIS field {field_name} must be a positive integer.")
    return int(number)


def _nonnegative_integer(value: Any, field_name: str) -> int:
    number = _decimal(value, field_name)
    if number < 0 or number != number.to_integral_value():
        raise ValueError(f"KIS field {field_name} must be a non-negative integer.")
    return int(number)


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"KIS field {field_name} is missing or invalid.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"KIS field {field_name} is not numeric.") from exc
    if not number.is_finite():
        raise ValueError(f"KIS field {field_name} is not finite.")
    return number
