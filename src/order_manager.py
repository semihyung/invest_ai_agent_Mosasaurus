from dataclasses import dataclass
from enum import Enum


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    UNKNOWN = "UNKNOWN"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"


_ALLOWED_TRANSITIONS = {
    OrderStatus.PENDING: {OrderStatus.SUBMITTING, OrderStatus.REJECTED},
    OrderStatus.SUBMITTING: {OrderStatus.SUBMITTED, OrderStatus.UNKNOWN, OrderStatus.REJECTED},
    OrderStatus.SUBMITTED: {
        OrderStatus.UNKNOWN,
        OrderStatus.ACCEPTED,
        OrderStatus.REJECTED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
    },
    OrderStatus.UNKNOWN: {OrderStatus.ACCEPTED, OrderStatus.REJECTED},
    OrderStatus.ACCEPTED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELED,
    },
    OrderStatus.CANCEL_PENDING: {
        OrderStatus.CANCELED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
    },
}


@dataclass(frozen=True)
class Quote:
    bid: int
    ask: int
    nav: float | None


@dataclass(frozen=True)
class OrderIntent:
    trade_date: str
    run_id: str
    slot_id: str
    ticker: str
    side: OrderSide
    quantity: int
    attempt_seq: int = 1

    def idempotency_key(self) -> str:
        return (
            f"{self.trade_date}:{self.run_id}:{self.slot_id}:{self.ticker}:"
            f"{self.side.value}:{self.attempt_seq}"
        )


def transition_order_status(
    old_status: OrderStatus,
    new_status: OrderStatus,
) -> OrderStatus:
    if not isinstance(old_status, OrderStatus) or not isinstance(new_status, OrderStatus):
        raise TypeError("Statuses must be OrderStatus values.")
    if new_status not in _ALLOWED_TRANSITIONS.get(old_status, set()):
        raise ValueError(f"Invalid order transition: {old_status.value} -> {new_status.value}")
    return new_status


def calculate_limit_price(
    quote: Quote,
    side: OrderSide,
    *,
    emergency_sell: bool = False,
    tick: int = 10,
) -> int:
    if not isinstance(quote, Quote):
        raise TypeError("quote must be a Quote value.")
    if not isinstance(side, OrderSide):
        raise TypeError("side must be an OrderSide value.")
    if isinstance(tick, bool) or tick <= 0:
        raise ValueError("Tick must be positive.")

    if emergency_sell:
        if side is not OrderSide.SELL:
            raise ValueError("Emergency limit price is only valid for SELL.")
        if quote.nav is None:
            raise ValueError("Emergency sell requires NAV.")
        return max(
            quote.bid - 2 * tick,
            int(quote.nav * 0.980 // tick * tick),
        )
    return quote.ask if side is OrderSide.BUY else quote.bid


def validate_spread_and_nav(
    quote: Quote,
    *,
    region: str,
) -> bool:
    if quote.ask < quote.bid:
        return False
    mid_price = (quote.bid + quote.ask) / 2
    if mid_price <= 0:
        return False
    spread_ratio = (quote.ask - quote.bid) / mid_price
    if spread_ratio > 0.003:
        return False
    if quote.nav is None:
        return False
    limit = 0.005 if region == "KR" else 0.012 if region == "US" else None
    if limit is None:
        raise ValueError("Region must be KR or US.")
    return abs((quote.ask - quote.nav) / quote.nav) <= limit


def allows_buy_submission(
    *,
    data_fresh: bool,
    account_fresh: bool,
    account_matched: bool,
    unknown_order_exists: bool,
) -> bool:
    return data_fresh and account_fresh and account_matched and not unknown_order_exists
