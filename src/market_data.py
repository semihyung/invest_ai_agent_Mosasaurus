from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
    nav_observed_at: datetime | None = None
    nav_received_at: datetime | None = None

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
        observed_at = _utc(self.observed_at, "observed_at")
        received_at = _utc(self.received_at, "received_at")
        if (self.nav_observed_at is None) != (self.nav_received_at is None):
            raise ValueError("Both NAV timestamps must be provided together.")
        if self.nav is not None and self.nav_observed_at is None:
            raise ValueError("NAV timestamps are required when NAV is present.")
        nav_observed_at = (
            _utc(self.nav_observed_at, "nav_observed_at")
            if self.nav_observed_at is not None
            else None
        )
        nav_received_at = (
            _utc(self.nav_received_at, "nav_received_at")
            if self.nav_received_at is not None
            else None
        )
        if received_at < observed_at:
            raise ValueError("received_at must not precede observed_at.")
        if (
            nav_observed_at is not None
            and nav_received_at is not None
            and nav_received_at < nav_observed_at
        ):
            raise ValueError("nav_received_at must not precede nav_observed_at.")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "received_at", received_at)
        object.__setattr__(self, "nav_observed_at", nav_observed_at)
        object.__setattr__(self, "nav_received_at", nav_received_at)


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
    """관측·수신 시각, 양방향 호가 및 스프레드 한도를 검증합니다."""
    if (
        freshness_status(snapshot.observed_at, now, ttl_seconds)
        is FreshnessStatus.STALE
        or freshness_status(snapshot.received_at, now, ttl_seconds)
        is FreshnessStatus.STALE
    ):
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
    """NAV 신선도와 지역별 시세 괴리 한도를 검증합니다."""
    if (
        snapshot.nav is None
        or snapshot.nav_observed_at is None
        or snapshot.nav_received_at is None
    ):
        return False
    if (
        freshness_status(snapshot.nav_observed_at, now, ttl_seconds)
        is FreshnessStatus.STALE
        or freshness_status(snapshot.nav_received_at, now, ttl_seconds)
        is FreshnessStatus.STALE
    ):
        return False
    limit = 0.005 if region == "KR" else 0.012 if region == "US" else None
    if limit is None:
        raise ValueError("Region must be KR or US.")
    quote = snapshot.current_price
    deviation = abs((quote - snapshot.nav) / snapshot.nav)
    return deviation <= limit


@dataclass(frozen=True)
class DailyCandle:
    """거래일별 수정 종가와 검증용 원주가를 보관합니다."""

    ticker: str
    trade_date: str
    adjusted_close: float
    volume: int
    raw_open: float | None = None
    raw_high: float | None = None
    raw_low: float | None = None
    raw_close: float | None = None

    def __post_init__(self) -> None:
        """가격·거래량·원주가 봉의 기본 무결성을 확인합니다."""
        if not isinstance(self.ticker, str) or not self.ticker.strip():
            raise ValueError("Ticker must be a non-empty string.")
        if not isinstance(self.trade_date, str):
            raise ValueError("trade_date must be an ISO date string.")
        try:
            parsed_trade_date = date.fromisoformat(self.trade_date)
        except ValueError as exc:
            raise ValueError("trade_date must be an ISO date string.") from exc
        if parsed_trade_date.isoformat() != self.trade_date:
            raise ValueError("trade_date must be an ISO date string.")
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
        if (
            self.raw_open is not None
            and self.raw_high is not None
            and self.raw_low is not None
            and self.raw_close is not None
        ):
            if self.raw_high < max(self.raw_open, self.raw_low, self.raw_close):
                raise ValueError("Raw high must not be below another candle price.")
            if self.raw_low > min(self.raw_open, self.raw_high, self.raw_close):
                raise ValueError("Raw low must not be above another candle price.")


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
    nav_observed_at: datetime | None = None,
    nav_received_at: datetime | None = None,
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
        nav_observed_at=nav_observed_at,
        nav_received_at=nav_received_at,
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
    expected_trading_dates: Sequence[str],
) -> bool:
    """최근 200개 봉이 거래일 달력과 명세의 가격 품질 기준에 맞는지 검사합니다."""
    if not candles:
        return False
    if (
        isinstance(candles, (str, bytes))
        or not isinstance(candles, Sequence)
        or len(candles) < 200
        or isinstance(expected_trading_dates, (str, bytes))
        or not isinstance(expected_trading_dates, Sequence)
        or len(expected_trading_dates) < 200
    ):
        return False

    recent = list(candles[-200:])
    expected = list(expected_trading_dates[-200:])
    if any(not isinstance(candle, DailyCandle) for candle in recent):
        return False
    dates = [candle.trade_date for candle in recent]
    if (
        not _are_iso_dates(dates)
        or not _are_iso_dates(expected)
        or not isinstance(previous_trading_day, str)
        or not _are_iso_dates([previous_trading_day])
    ):
        return False
    if (
        dates != expected
        or dates != sorted(dates)
        or len(set(dates)) != len(dates)
        or dates[-1] != previous_trading_day
    ):
        return False
    if expected != sorted(expected) or len(set(expected)) != len(expected):
        return False

    for candle in recent:
        if any(
            price is None
            for price in (
                candle.raw_open,
                candle.raw_high,
                candle.raw_low,
                candle.raw_close,
            )
        ):
            return False

    for previous, current in zip(recent, recent[1:]):
        if current.adjusted_close / previous.adjusted_close - 1 > 0.30:
            return False
        if current.adjusted_close / previous.adjusted_close - 1 < -0.30:
            return False
    return True


def _are_iso_dates(values: Sequence[str]) -> bool:
    """모든 값이 정규 ISO 거래일 문자열인지 확인합니다."""
    for value in values:
        if not isinstance(value, str):
            return False
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return False
        if parsed.isoformat() != value:
            return False
    return True


@dataclass(frozen=True)
class DataQualityGateResult:
    """데이터 게이트 판정과 실패 사유를 보관합니다."""

    passed: bool
    failure_codes: tuple[str, ...]


class DataQualityGate:
    """매수 판단 전에 데이터·계좌·주문 상태를 fail-closed 방식으로 검사합니다."""

    _UNRESOLVED_ORDER_STATUSES = {
        "SUBMITTING",
        "PENDING_CONFIRMATION",
        "CANCEL_PENDING",
    }
    _KNOWN_ORDER_STATUSES = {
        "CREATED",
        "SUBMITTING",
        "PENDING_CONFIRMATION",
        "ACCEPTED",
        "REJECTED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCEL_PENDING",
        "CANCELED",
        "CONFIRMED_NOT_ACCEPTED",
        "EXPIRED",
    }
    _KNOWN_CASH_FLOW_CLASSIFICATIONS = {
        "CONFIRMED",
        "UNCLASSIFIED_CASH_FLOW",
    }

    def evaluate(
        self,
        *,
        now: datetime,
        snapshot: IntradaySnapshot | None,
        region: str,
        candles: Sequence[DailyCandle],
        previous_trading_day: str,
        expected_trading_dates: Sequence[str],
        raw_adjusted_history_matches: bool,
        corporate_actions_verified: bool,
        account_data_fresh: bool,
        account_matched: bool,
        order_statuses: Sequence[str],
        cash_flow_classifications: Sequence[str],
        calendar_matches: bool,
    ) -> DataQualityGateResult:
        """필수 입력 중 하나라도 불량이면 실패 코드와 함께 거부합니다."""
        for field_name, value in (
            ("raw_adjusted_history_matches", raw_adjusted_history_matches),
            ("corporate_actions_verified", corporate_actions_verified),
            ("account_data_fresh", account_data_fresh),
            ("account_matched", account_matched),
            ("calendar_matches", calendar_matches),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{field_name} must be a boolean.")

        current_time = _utc(now, "now")
        failures: list[str] = []
        if not isinstance(snapshot, IntradaySnapshot):
            failures.append("MISSING_INTRADAY_DATA")
        else:
            if not validate_quote(snapshot, current_time):
                failures.append("STALE_OR_INVALID_QUOTE")
            if not validate_nav_deviation(snapshot, current_time, region=region):
                failures.append("STALE_OR_INVALID_NAV")

        if not validate_daily_history(
            candles,
            previous_trading_day=previous_trading_day,
            expected_trading_dates=expected_trading_dates,
        ):
            failures.append("INVALID_DAILY_HISTORY")
        if not raw_adjusted_history_matches:
            failures.append("RAW_ADJUSTED_HISTORY_MISMATCH")
        if not corporate_actions_verified:
            failures.append("UNVERIFIED_CORPORATE_ACTION")
        if not account_data_fresh:
            failures.append("STALE_ACCOUNT_DATA")
        if not account_matched:
            failures.append("ACCOUNT_MISMATCH")
        if isinstance(order_statuses, (str, bytes)) or not isinstance(
            order_statuses, Sequence
        ):
            raise TypeError("order_statuses must be a sequence of status strings.")
        for status in order_statuses:
            if not isinstance(status, str) or status not in self._KNOWN_ORDER_STATUSES:
                failures.append("UNKNOWN_ORDER_STATUS")
                break
            if status in self._UNRESOLVED_ORDER_STATUSES:
                failures.append("UNRESOLVED_ORDER")
                break
        if isinstance(cash_flow_classifications, (str, bytes)) or not isinstance(
            cash_flow_classifications, Sequence
        ):
            raise TypeError(
                "cash_flow_classifications must be a sequence of status strings."
            )
        for classification in cash_flow_classifications:
            if (
                not isinstance(classification, str)
                or classification not in self._KNOWN_CASH_FLOW_CLASSIFICATIONS
            ):
                failures.append("UNKNOWN_CASH_FLOW_CLASSIFICATION")
                break
            if classification == "UNCLASSIFIED_CASH_FLOW":
                failures.append("UNCLASSIFIED_CASH_FLOW")
                break
        if not calendar_matches:
            failures.append("CALENDAR_MISMATCH")

        failure_codes = tuple(failures)
        return DataQualityGateResult(
            passed=not failure_codes,
            failure_codes=failure_codes,
        )


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
