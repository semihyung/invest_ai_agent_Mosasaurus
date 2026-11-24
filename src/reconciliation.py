"""KIS 계좌 잔고·주문 상태를 내부 DB와 비교하고 불일치 시 SAFE_HALT 합니다."""

from dataclasses import dataclass
from datetime import datetime
import sqlite3
from typing import Iterable

from src.safety_flags import clear_flag, raise_flag


@dataclass(frozen=True)
class Position:
    """브로커 또는 로컬 DB에서 읽은 종목 보유 수량입니다."""

    ticker: str
    quantity: int


@dataclass(frozen=True)
class OrderSnapshot:
    """브로커 또는 DB의 미체결·체결 주문 상태 비교 레코드입니다."""

    order_key: str
    ticker: str
    side: str
    requested_qty: int
    filled_qty: int
    remaining_qty: int
    status: str


@dataclass(frozen=True)
class ReconciliationResult:
    """대조 성공 여부와 종목·주문 불일치 식별자를 반환합니다."""

    reconciliation_ok: bool
    mismatches: tuple[str, ...]
    order_mismatches: tuple[str, ...] = ()
    failure_codes: tuple[str, ...] = ()


def _validate_positions(positions: Iterable[Position]) -> dict[str, int]:
    """보유 수량 레코드의 티커 중복과 음수 수량을 거부합니다."""
    result: dict[str, int] = {}
    for position in positions:
        if not isinstance(position, Position):
            raise TypeError("positions must contain Position values.")
        if not isinstance(position.ticker, str) or not position.ticker.strip():
            raise ValueError("Position ticker must be non-empty.")
        if (
            isinstance(position.quantity, bool)
            or not isinstance(position.quantity, int)
            or position.quantity < 0
        ):
            raise ValueError("Position quantity must be a non-negative integer.")
        if position.ticker in result:
            raise ValueError("Duplicate position ticker in reconciliation input.")
        result[position.ticker] = position.quantity
    return result


def reconcile_positions(
    broker_positions: list[Position],
    sqlite_positions: list[Position],
) -> ReconciliationResult:
    """KIS와 SQLite의 모든 보유 종목 수량을 누락 포함 비교합니다."""
    broker = _validate_positions(broker_positions)
    sqlite = _validate_positions(sqlite_positions)
    mismatches = tuple(
        ticker
        for ticker in sorted(set(broker) | set(sqlite))
        if broker.get(ticker, 0) != sqlite.get(ticker, 0)
    )
    return ReconciliationResult(
        reconciliation_ok=not mismatches,
        mismatches=mismatches,
        failure_codes=("POSITION_MISMATCH",) if mismatches else (),
    )


def _validate_orders(orders: Iterable[OrderSnapshot]) -> dict[str, OrderSnapshot]:
    """주문 수량 및 비교 키가 유효하고 유일한지 검사합니다."""
    result: dict[str, OrderSnapshot] = {}
    for order in orders:
        if not isinstance(order, OrderSnapshot):
            raise TypeError("orders must contain OrderSnapshot values.")
        for name, value in (
            ("order_key", order.order_key),
            ("ticker", order.ticker),
            ("side", order.side),
            ("status", order.status),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string.")
        if order.side not in ("BUY", "SELL"):
            raise ValueError("Order side must be BUY or SELL.")
        quantities = (
            order.requested_qty,
            order.filled_qty,
            order.remaining_qty,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in quantities
        ):
            raise ValueError("Order quantities must be non-negative integers.")
        if (
            order.requested_qty == 0
            or order.filled_qty + order.remaining_qty > order.requested_qty
        ):
            raise ValueError("Order quantity totals are inconsistent.")
        if order.order_key in result:
            raise ValueError("Duplicate order key in reconciliation input.")
        result[order.order_key] = order
    return result


def reconcile_orders(
    broker_orders: Iterable[OrderSnapshot],
    sqlite_orders: Iterable[OrderSnapshot],
) -> tuple[str, ...]:
    """주문번호·방향·수량·체결량·상태를 KIS와 SQLite 사이에서 비교합니다."""
    broker = _validate_orders(broker_orders)
    sqlite = _validate_orders(sqlite_orders)
    return tuple(
        order_key
        for order_key in sorted(set(broker) | set(sqlite))
        if broker.get(order_key) != sqlite.get(order_key)
    )


def reconciliation_safe_halt(
    *,
    position_result: ReconciliationResult,
    unknown_order_exists: bool,
    stale_account_data: bool,
    order_mismatches: Iterable[str] = (),
    unclassified_cash_flow_exists: bool = False,
    data_invalid: bool = False,
) -> bool:
    """불일치·미확정 주문·오래된 데이터·미분류 현금흐름을 안전정지 사유로 묶습니다."""
    if not isinstance(position_result, ReconciliationResult):
        raise TypeError("position_result must be a ReconciliationResult.")
    flags = (
        unknown_order_exists,
        stale_account_data,
        unclassified_cash_flow_exists,
        data_invalid,
    )
    if any(not isinstance(value, bool) for value in flags):
        raise ValueError("Reconciliation flags must be booleans.")
    mismatches = tuple(order_mismatches)
    if any(not isinstance(value, str) or not value for value in mismatches):
        raise ValueError("Order mismatch identifiers must be non-empty strings.")
    return (
        not position_result.reconciliation_ok
        or bool(position_result.order_mismatches)
        or bool(mismatches)
        or bool(position_result.failure_codes)
        or unknown_order_exists
        or stale_account_data
        or unclassified_cash_flow_exists
        or data_invalid
    )


def reconcile_account_state(
    *,
    broker_positions: list[Position],
    sqlite_positions: list[Position],
    broker_orders: Iterable[OrderSnapshot],
    sqlite_orders: Iterable[OrderSnapshot],
    account_data_fresh: bool,
    unclassified_cash_flow_exists: bool = False,
) -> ReconciliationResult:
    """포지션과 주문을 모두 대조하고 데이터 불량 상태를 결과에 기록합니다."""
    if not isinstance(account_data_fresh, bool) or not isinstance(
        unclassified_cash_flow_exists,
        bool,
    ):
        raise ValueError("Account reconciliation flags must be booleans.")
    position_result = reconcile_positions(broker_positions, sqlite_positions)
    order_mismatches = reconcile_orders(broker_orders, sqlite_orders)
    failure_codes = list(position_result.failure_codes)
    if order_mismatches:
        failure_codes.append("ORDER_MISMATCH")
    if not account_data_fresh:
        failure_codes.append("STALE_ACCOUNT_DATA")
    if unclassified_cash_flow_exists:
        failure_codes.append("UNCLASSIFIED_CASH_FLOW")
    return ReconciliationResult(
        reconciliation_ok=not failure_codes,
        mismatches=position_result.mismatches,
        order_mismatches=order_mismatches,
        failure_codes=tuple(failure_codes),
    )


def persist_reconciliation_result(
    connection: sqlite3.Connection,
    result: ReconciliationResult,
    *,
    checked_at: datetime,
) -> None:
    """대조 상태를 system_state에 기록하고 불일치는 SAFE_HALT를 래치합니다."""
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection.")
    if not isinstance(result, ReconciliationResult):
        raise TypeError("result must be a ReconciliationResult.")
    if not isinstance(checked_at, datetime) or checked_at.tzinfo is None:
        raise ValueError("checked_at must be timezone-aware.")
    timestamp = checked_at.isoformat()
    with connection:
        connection.execute(
            """
            INSERT INTO system_state ("key", value, updated_at)
            VALUES ('reconciliation_ok', ?, ?)
            ON CONFLICT("key") DO UPDATE SET
                value = excluded.value, updated_at = excluded.updated_at
            """,
            ("true" if result.reconciliation_ok else "false", timestamp),
        )
        connection.execute(
            """
            INSERT INTO system_state ("key", value, updated_at)
            VALUES ('last_reconciliation_at', ?, ?)
            ON CONFLICT("key") DO UPDATE SET
                value = excluded.value, updated_at = excluded.updated_at
            """,
            (timestamp, timestamp),
        )
    _record_reconciliation_flags(connection, result, checked_at=checked_at)


def _record_reconciliation_flags(
    connection: sqlite3.Connection,
    result: ReconciliationResult,
    *,
    checked_at: datetime,
) -> None:
    """대조 실패 코드를 명세 6.3절의 플래그로 나눠 기록합니다.

    - 포지션·주문 불일치 → `ACCOUNT_MISMATCH`(SAFE_HALT, 수동 해제)
    - 계좌 데이터 오래됨 → `ACCOUNT_DATA_STALE`(매수 차단만, 신선해지면 자동 해제)
    - 미분류 현금흐름 → `UNCLASSIFIED_CASH_FLOW`(매수 차단만, `/classify` 로 해제)
    """
    codes = set(result.failure_codes)
    buy_block_codes = {"STALE_ACCOUNT_DATA", "UNCLASSIFIED_CASH_FLOW"}
    mismatch = (
        bool(result.mismatches)
        or bool(result.order_mismatches)
        or bool(codes - buy_block_codes)
    )
    if mismatch:
        raise_flag(connection, "HALT", "ACCOUNT_MISMATCH", now=checked_at,
                   detail=",".join(sorted(codes)) or "POSITION_OR_ORDER_MISMATCH")
    if "STALE_ACCOUNT_DATA" in codes:
        raise_flag(connection, "BUY_BLOCK", "ACCOUNT_DATA_STALE", now=checked_at)
    else:
        clear_flag(connection, "BUY_BLOCK", "ACCOUNT_DATA_STALE",
                   now=checked_at, cleared_by="SYSTEM")
    if "UNCLASSIFIED_CASH_FLOW" in codes:
        raise_flag(connection, "BUY_BLOCK", "UNCLASSIFIED_CASH_FLOW", now=checked_at)
