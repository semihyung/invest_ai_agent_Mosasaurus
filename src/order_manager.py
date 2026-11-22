from dataclasses import dataclass
from enum import Enum


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    CONFIRMED_NOT_ACCEPTED = "CONFIRMED_NOT_ACCEPTED"
    EXPIRED = "EXPIRED"


_ALLOWED_TRANSITIONS = {
    OrderStatus.CREATED: {OrderStatus.SUBMITTING, OrderStatus.EXPIRED, OrderStatus.REJECTED},
    OrderStatus.SUBMITTING: {
        OrderStatus.PENDING_CONFIRMATION,
        OrderStatus.ACCEPTED,
        OrderStatus.REJECTED,
    },
    OrderStatus.PENDING_CONFIRMATION: {
        OrderStatus.ACCEPTED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELED,
        OrderStatus.CONFIRMED_NOT_ACCEPTED,
        OrderStatus.PENDING_CONFIRMATION,
    },
    OrderStatus.ACCEPTED: {
        OrderStatus.CANCEL_PENDING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
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
    OrderStatus.CONFIRMED_NOT_ACCEPTED: {OrderStatus.SUBMITTING},
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
        if abs((quote.bid - quote.nav) / quote.nav) > 0.020:
            raise ValueError("Emergency sell NAV deviation exceeds 2%.")
        return quote.bid // tick * tick
    return quote.ask if side is OrderSide.BUY else quote.bid


def calculate_emergency_reprice(
    quote: Quote,
    *,
    reprice_count: int,
    tick: int = 10,
) -> int | None:
    if reprice_count < 0 or reprice_count > 5:
        return None
    if quote.nav is None or quote.bid <= 0 or quote.ask <= 0:
        return None
    if abs((quote.bid - quote.nav) / quote.nav) > 0.020:
        return None
    return max(0, quote.bid // tick * tick - reprice_count * tick)


@dataclass(frozen=True)
class RealOrderGateContext:
    environment: str
    operator_approval: bool
    reconciliation_ok: bool
    reconciliation_fresh: bool
    unresolved_order_exists: bool
    unclassified_cash_flow_exists: bool
    token_valid: bool
    calendar_valid: bool
    quote_valid: bool
    adjusted_data_valid: bool
    backtest_calibrated: bool
    mode_allows_order: bool
    idempotency_conflict: bool


def validate_real_order_gate(context: RealOrderGateContext) -> bool:
    return (
        context.environment == "REAL"
        and context.operator_approval
        and context.reconciliation_ok
        and context.reconciliation_fresh
        and not context.unresolved_order_exists
        and not context.unclassified_cash_flow_exists
        and context.token_valid
        and context.calendar_valid
        and context.quote_valid
        and context.adjusted_data_valid
        and context.backtest_calibrated
        and context.mode_allows_order
        and not context.idempotency_conflict
    )


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
