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
    # v3.7.5: KIS 당일 주문 중 DB 의 어떤 attempt 와도 매칭되지 않는 주문(정전으로 write-ahead 기록이 사라진 경우 등).
    orphan_orders: tuple[str, ...] = ()


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
    """주문번호·종목·방향·주문 수량·누적 체결량을 KIS와 SQLite 사이에서 비교합니다.

    상태 문자열과 잔량은 비교하지 않습니다: DB 는 마감 후 `EXPIRED`·`CANCELED` 같은 내부 종결 상태를 쓰지만
    KIS 정규화 상태에는 그런 값이 없어(미체결 = `ACCEPTED`), 같은 주문이 매일 불일치로 잡히기 때문입니다.
    보유 수량을 바꾸는 것은 체결량이므로 체결량이 같으면 같은 주문으로 봅니다.
    """
    broker = _validate_orders(broker_orders)
    sqlite = _validate_orders(sqlite_orders)

    def identity(order: OrderSnapshot | None):
        """비교에 쓰는 필드만 뽑는다(없으면 None)."""
        if order is None:
            return None
        return order.ticker, order.side, order.requested_qty, order.filled_qty

    return tuple(
        order_key
        for order_key in sorted(set(broker) | set(sqlite))
        if identity(broker.get(order_key)) != identity(sqlite.get(order_key))
    )


def find_orphan_orders(
    broker_orders: Iterable[OrderSnapshot],
    sqlite_orders: Iterable[OrderSnapshot],
) -> tuple[str, ...]:
    """KIS 에는 있으나 DB 에는 없는 주문 키를 정렬해 반환합니다(고아 주문, 명세 3.3절 Step 5)."""
    broker = _validate_orders(broker_orders)
    sqlite = _validate_orders(sqlite_orders)
    return tuple(sorted(set(broker) - set(sqlite)))


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
    broker_orders = tuple(broker_orders)
    sqlite_orders = tuple(sqlite_orders)
    order_mismatches = reconcile_orders(broker_orders, sqlite_orders)
    orphans = find_orphan_orders(broker_orders, sqlite_orders)
    failure_codes = list(position_result.failure_codes)
    # 고아 주문은 일반 주문 불일치(ACCOUNT_MISMATCH)가 아니라 별도 코드로 처리한다(PENDING_CONFIRMATION 원인).
    if set(order_mismatches) - set(orphans):
        failure_codes.append("ORDER_MISMATCH")
    if orphans:
        failure_codes.append("ORPHAN_ORDER")
    if not account_data_fresh:
        failure_codes.append("STALE_ACCOUNT_DATA")
    if unclassified_cash_flow_exists:
        failure_codes.append("UNCLASSIFIED_CASH_FLOW")
    return ReconciliationResult(
        reconciliation_ok=not failure_codes,
        mismatches=position_result.mismatches,
        order_mismatches=tuple(sorted(set(order_mismatches) - set(orphans))),
        failure_codes=tuple(failure_codes),
        orphan_orders=orphans,
    )


_MISMATCH_FIRST_SEEN_KEY = "account_mismatch_first_seen_at"
_MISMATCH_FINGERPRINT_KEY = "account_mismatch_fingerprint"


def _state_value(connection: sqlite3.Connection, key: str) -> str | None:
    """system_state 한 값을 읽습니다(없으면 None)."""
    row = connection.execute('SELECT value FROM system_state WHERE "key" = ?', (key,)).fetchone()
    return row[0] if row else None


def _set_state(connection: sqlite3.Connection, key: str, value: str, timestamp: str) -> None:
    """system_state 한 값을 UPSERT 합니다(트랜잭션은 호출자 책임)."""
    connection.execute(
        """
        INSERT INTO system_state ("key", value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT("key") DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, timestamp),
    )


def evaluate_account_mismatch(
    connection: sqlite3.Connection,
    result: ReconciliationResult,
    *,
    now: datetime,
    confirm_interval_seconds: int = 60,
    quiescent: bool = True,
) -> str:
    """수량·주문 불일치를 2단계로 판정합니다(명세 4.3절 3번, v3.7.5).

    - 반환값 `"SKIPPED"`: 주문·체결이 진행 중(`quiescent=False`)이라 판정을 건너뜀(불일치로 보지 않음).
    - `"OK"`: 불일치 없음 → 기존 관측 기록을 지운다.
    - `"PROVISIONAL"`: 첫 관측(또는 간격 전 재관측) → 신규 매수만 대기(`ACCOUNT_DATA_STALE` 매수 차단),
      `ACCOUNT_MISMATCH` 는 만들지 않는다.
    - `"CONFIRMED"`: 간격 이상 뒤에도 같은 불일치가 재현 → `ACCOUNT_MISMATCH`(SAFE_HALT, 수동 해제).
    조회 자체의 실패는 불일치가 아니므로 이 함수가 아니라 호출자가 `ACCOUNT_DATA_STALE` 로 처리한다.
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    mismatch_codes = set(result.failure_codes) - {
        "STALE_ACCOUNT_DATA", "UNCLASSIFIED_CASH_FLOW", "ORPHAN_ORDER"
    }
    mismatched = bool(result.mismatches) or bool(result.order_mismatches) or bool(mismatch_codes)
    timestamp = now.isoformat()
    if not quiescent:
        return "SKIPPED"
    if not mismatched:
        with connection:
            connection.execute(
                'DELETE FROM system_state WHERE "key" IN (?, ?)',
                (_MISMATCH_FIRST_SEEN_KEY, _MISMATCH_FINGERPRINT_KEY),
            )
        return "OK"
    fingerprint = ",".join(sorted(result.mismatches) + sorted(result.order_mismatches)
                           + sorted(mismatch_codes))
    first_seen_raw = _state_value(connection, _MISMATCH_FIRST_SEEN_KEY)
    previous = _state_value(connection, _MISMATCH_FINGERPRINT_KEY)
    first_seen = datetime.fromisoformat(first_seen_raw) if first_seen_raw else None
    if first_seen is not None and previous == fingerprint:
        if (now - first_seen).total_seconds() >= confirm_interval_seconds:
            raise_flag(connection, "HALT", "ACCOUNT_MISMATCH", now=now, detail=fingerprint)
            return "CONFIRMED"
        return "PROVISIONAL"
    with connection:
        _set_state(connection, _MISMATCH_FIRST_SEEN_KEY, timestamp, timestamp)
        _set_state(connection, _MISMATCH_FINGERPRINT_KEY, fingerprint, timestamp)
    raise_flag(connection, "BUY_BLOCK", "ACCOUNT_DATA_STALE", now=now,
               detail="PROVISIONAL_ACCOUNT_MISMATCH")
    return "PROVISIONAL"


def persist_reconciliation_result(
    connection: sqlite3.Connection,
    result: ReconciliationResult,
    *,
    checked_at: datetime,
    confirm_interval_seconds: int = 60,
    quiescent: bool = True,
) -> str:
    """대조 상태를 system_state에 기록하고, 불일치는 2회 재현될 때만 SAFE_HALT를 래치합니다.

    반환값은 `evaluate_account_mismatch` 의 판정(`SKIPPED`/`OK`/`PROVISIONAL`/`CONFIRMED`)입니다.
    """
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
    verdict = evaluate_account_mismatch(
        connection, result, now=checked_at,
        confirm_interval_seconds=confirm_interval_seconds, quiescent=quiescent,
    )
    _record_reconciliation_flags(connection, result, checked_at=checked_at, verdict=verdict)
    return verdict


def _record_reconciliation_flags(
    connection: sqlite3.Connection,
    result: ReconciliationResult,
    *,
    checked_at: datetime,
    verdict: str = "OK",
) -> None:
    """대조 실패 코드를 명세 6.3절의 플래그로 나눠 기록합니다.

    - 포지션·주문 불일치 → 2회 재현 시 `ACCOUNT_MISMATCH`(SAFE_HALT, 수동 해제). 첫 관측은 매수 대기만.
    - 계좌 데이터 오래됨 → `ACCOUNT_DATA_STALE`(매수 차단만, 신선해지면 자동 해제)
    - 미분류 현금흐름 → `UNCLASSIFIED_CASH_FLOW`(매수 차단만, `/classify` 로 해제)
    """
    codes = set(result.failure_codes)
    if "STALE_ACCOUNT_DATA" in codes or verdict == "PROVISIONAL":
        # 첫 관측(PROVISIONAL)의 매수 대기는 `evaluate_account_mismatch` 가 이미 올렸으므로 유지한다.
        if "STALE_ACCOUNT_DATA" in codes:
            raise_flag(connection, "BUY_BLOCK", "ACCOUNT_DATA_STALE", now=checked_at)
    elif verdict != "SKIPPED":
        clear_flag(connection, "BUY_BLOCK", "ACCOUNT_DATA_STALE",
                   now=checked_at, cleared_by="SYSTEM")
    if "UNCLASSIFIED_CASH_FLOW" in codes:
        raise_flag(connection, "BUY_BLOCK", "UNCLASSIFIED_CASH_FLOW", now=checked_at)
