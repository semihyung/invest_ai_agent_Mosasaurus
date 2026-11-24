"""주문 권한·멱등성·상태 전이와 지정가 정책의 단일 경계입니다."""

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
import sqlite3
from datetime import datetime
from typing import Any, Mapping, Protocol

from src._kis_order_adapter import KisOrderAdapter


class OrderSide(str, Enum):
    """주문 방향입니다."""

    BUY = "BUY"
    SELL = "SELL"


class OrderIntentType(str, Enum):
    """주문이 만들어진 전략·위험 목적을 구분합니다."""

    STRATEGY_REBALANCE = "STRATEGY_REBALANCE"
    RISK_REDUCTION = "RISK_REDUCTION"
    PARKING_ALLOCATION = "PARKING_ALLOCATION"
    EXIT_OBLIGATION_RETRY = "EXIT_OBLIGATION_RETRY"
    SMOKE_TEST = "SMOKE_TEST"


class OrderStatus(str, Enum):
    """KIS 주문의 진행·종결 상태입니다."""

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
    OrderStatus.CREATED: {OrderStatus.SUBMITTING, OrderStatus.EXPIRED},
    OrderStatus.SUBMITTING: {
        OrderStatus.ACCEPTED,
        OrderStatus.REJECTED,
        OrderStatus.PENDING_CONFIRMATION,
    },
    OrderStatus.PENDING_CONFIRMATION: {
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELED,
        OrderStatus.CONFIRMED_NOT_ACCEPTED,
        # 같은 상태로의 재확인은 전이가 아니라 "재조회 기록"이며 상태를 바꾸지 않습니다.
        OrderStatus.PENDING_CONFIRMATION,
    },
    OrderStatus.ACCEPTED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.EXPIRED,
    },
    OrderStatus.CANCEL_PENDING: {
        OrderStatus.CANCELED,
        OrderStatus.FILLED,
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.EXPIRED,
    },
}

# 활성(미해결) 상태 6종과 종결 상태 5종(명세 0.5절).
ACTIVE_ORDER_STATUSES = frozenset(
    {
        OrderStatus.CREATED,
        OrderStatus.SUBMITTING,
        OrderStatus.PENDING_CONFIRMATION,
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCEL_PENDING,
    }
)
TERMINAL_ORDER_STATUSES = frozenset(set(OrderStatus) - ACTIVE_ORDER_STATUSES)


@dataclass(frozen=True)
class Quote:
    """검증에 사용할 최우선 호가와 선택적 NAV 값입니다."""

    bid: int
    ask: int
    nav: float | None


@dataclass(frozen=True)
class OrderIntent:
    """가격과 실행 ID와 무관한 결정론적 논리 주문 의도입니다."""

    cycle_id: str
    intent_type: OrderIntentType
    slot_id: str | None
    ticker: str
    side: OrderSide
    quantity: int
    # 명세 7.3절: 매도 의무 재주문(`EXIT_OBLIGATION_RETRY`)은 어떤 의무의 체결인지 FK 로 연결한다.
    # 멱등성 키에는 넣지 않는다(cycle_id `X{obligation_id}:{거래일}` 에 이미 의무가 반영돼 있음).
    obligation_id: str | None = None

    def idempotency_key(self) -> str:
        """사이클·종류·슬롯·종목·방향으로 키를 결정적으로 생성합니다."""
        if not isinstance(self.intent_type, OrderIntentType):
            raise ValueError("intent_type must be an OrderIntentType value.")
        if not isinstance(self.side, OrderSide):
            raise ValueError("side must be an OrderSide value.")
        for value in (self.cycle_id, self.ticker):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("cycle_id and ticker must be non-empty strings.")
        if self.slot_id is not None and (
            not isinstance(self.slot_id, str) or not self.slot_id.strip()
        ):
            raise ValueError("slot_id must be None or a non-empty string.")
        identity = json.dumps(
            [
                self.cycle_id,
                self.intent_type.value,
                self.slot_id,
                self.ticker,
                self.side.value,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    def payload_hash(self, *, policy_payload: Mapping[str, Any]) -> str:
        """의도 키와 별개로 최초 수량·가격 정책 payload를 고정합니다."""
        if not isinstance(policy_payload, Mapping):
            raise TypeError("policy_payload must be a mapping.")
        if (
            isinstance(self.quantity, bool)
            or not isinstance(self.quantity, int)
            or self.quantity <= 0
        ):
            raise ValueError("quantity must be a positive integer.")
        serialized = json.dumps(
            {
                "quantity": self.quantity,
                "policy": dict(policy_payload),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()


class IntentConflictError(ValueError):
    """동일 멱등성 키에 서로 다른 주문 payload가 요청된 경우입니다."""


@dataclass(frozen=True)
class IntentPersistenceResult:
    """멱등성 저장 결과와 기존 주문 의도 식별자를 반환합니다."""

    parent_intent_id: str
    idempotency_key: str
    created: bool
    status: str


def persist_order_intent(
    connection: sqlite3.Connection,
    intent: OrderIntent,
    *,
    policy_payload: Mapping[str, Any],
    created_at: datetime,
) -> IntentPersistenceResult:
    """DB UNIQUE 제약과 payload 비교를 함께 사용해 중복 의도를 방지합니다."""
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection.")
    if not isinstance(intent, OrderIntent):
        raise TypeError("intent must be an OrderIntent value.")
    if not isinstance(created_at, datetime) or created_at.tzinfo is None:
        raise ValueError("created_at must be timezone-aware.")
    if not isinstance(intent.side, OrderSide):
        raise ValueError("side must be an OrderSide value.")
    if intent.intent_type is OrderIntentType.SMOKE_TEST:
        if intent.side is not OrderSide.BUY or intent.quantity != 1:
            raise ValueError("Smoke tests permit only a one-share BUY intent.")
    if intent.intent_type is OrderIntentType.EXIT_OBLIGATION_RETRY:
        if (
            intent.side is not OrderSide.SELL
            or not isinstance(intent.obligation_id, str)
            or not intent.obligation_id.strip()
        ):
            raise ValueError("An exit-obligation retry must be a SELL linked to an obligation_id.")
    elif intent.obligation_id is not None and (
        not isinstance(intent.obligation_id, str) or not intent.obligation_id.strip()
    ):
        raise ValueError("obligation_id must be None or a non-empty string.")
    key = intent.idempotency_key()
    payload_hash = intent.payload_hash(policy_payload=policy_payload)
    parent_id = hashlib.sha256(key.encode("ascii")).hexdigest()
    timestamp = created_at.isoformat()

    try:
        existing = connection.execute(
            """
            SELECT parent_intent_id, intent_payload_hash, status
            FROM order_intents
            WHERE idempotency_key = ?
            """,
            (key,),
        ).fetchone()
        if existing is not None:
            if existing["intent_payload_hash"] != payload_hash:
                raise IntentConflictError(
                    "INTENT_CONFLICT: existing idempotency key has a different payload."
                )
            return IntentPersistenceResult(
                existing["parent_intent_id"],
                key,
                False,
                existing["status"],
            )

        connection.execute(
            """
            INSERT INTO order_intents (
                parent_intent_id, idempotency_key, cycle_id, intent_type,
                slot_id, ticker, side, requested_qty, obligation_id,
                intent_payload_hash, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CREATED', ?, ?)
            """,
            (
                parent_id,
                key,
                intent.cycle_id,
                intent.intent_type.value,
                intent.slot_id,
                intent.ticker,
                intent.side.value,
                intent.quantity,
                intent.obligation_id,
                payload_hash,
                timestamp,
                timestamp,
            ),
        )
        return IntentPersistenceResult(parent_id, key, True, "CREATED")
    except sqlite3.IntegrityError:
        # 다른 프로세스가 같은 키를 먼저 넣었을 수 있으므로 결과를 재조회합니다.
        existing = connection.execute(
            """
            SELECT parent_intent_id, idempotency_key, intent_payload_hash, status
            FROM order_intents
            WHERE idempotency_key = ?
            """,
            (key,),
        ).fetchone()
        if existing is None:
            raise
        if existing["intent_payload_hash"] != payload_hash:
            raise IntentConflictError(
                "INTENT_CONFLICT: concurrent intent has a different payload."
            )
        return IntentPersistenceResult(
            existing["parent_intent_id"],
            existing["idempotency_key"],
            False,
            existing["status"],
        )


def record_order_attempt(
    connection: sqlite3.Connection,
    *,
    order_id: str,
    parent_intent_id: str,
    attempt_id: str,
    order_price: int,
    strategy_reason: str,
    submitted_at: datetime,
    request_payload_hash: str,
) -> None:
    """주문 API 호출 전에 새 LIMIT 시도를 DB에 원자적으로 기록합니다."""
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection.")
    for name, value in (
        ("order_id", order_id),
        ("parent_intent_id", parent_intent_id),
        ("attempt_id", attempt_id),
        ("strategy_reason", strategy_reason),
        ("request_payload_hash", request_payload_hash),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string.")
    if (
        isinstance(order_price, bool)
        or not isinstance(order_price, int)
        or order_price <= 0
    ):
        raise ValueError("order_price must be a positive integer.")
    if not isinstance(submitted_at, datetime) or submitted_at.tzinfo is None:
        raise ValueError("submitted_at must be timezone-aware.")

    intent_row = connection.execute(
        """
        SELECT cycle_id, intent_type, slot_id, ticker, side, requested_qty, status
        FROM order_intents
        WHERE parent_intent_id = ?
        """,
        (parent_intent_id,),
    ).fetchone()
    if intent_row is None:
        raise ValueError("Order intent does not exist.")
    previous_attempts = connection.execute(
        """
        SELECT status
        FROM orders
        WHERE parent_intent_id = ?
        ORDER BY rowid DESC
        """,
        (parent_intent_id,),
    ).fetchall()
    if previous_attempts:
        latest_status = OrderStatus(previous_attempts[0]["status"])
        if latest_status is not OrderStatus.CONFIRMED_NOT_ACCEPTED:
            raise ValueError("Retry requires authoritative not-accepted confirmation.")
    elif intent_row["status"] != "CREATED":
        raise ValueError("A new order attempt requires a CREATED intent.")

    timestamp = submitted_at.isoformat()
    with connection:
        connection.execute(
            """
            INSERT INTO orders (
                order_id, parent_intent_id, attempt_id, ticker, side,
                requested_qty, filled_qty, remaining_qty, order_type,
                order_price, status, request_payload_hash, slot_id,
                strategy_reason, submitted_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'LIMIT', ?, 'SUBMITTING', ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                parent_intent_id,
                attempt_id,
                intent_row["ticker"],
                intent_row["side"],
                intent_row["requested_qty"],
                intent_row["requested_qty"],
                order_price,
                request_payload_hash,
                intent_row["slot_id"],
                strategy_reason,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            UPDATE order_intents
            SET status = 'IN_PROGRESS', updated_at = ?
            WHERE parent_intent_id = ?
            """,
            (timestamp, parent_intent_id),
        )
        connection.execute(
            """
            INSERT INTO order_events (
                order_id, event_type, old_status, new_status, actor,
                reason_code, correlation_id, created_at
            ) VALUES (?, 'SUBMISSION_ATTEMPT', NULL, 'SUBMITTING',
                      'order_manager', 'LIMIT_ATTEMPT_RECORDED', ?, ?)
            """,
            (order_id, attempt_id, timestamp),
        )


class LimitOrderTransport(Protocol):
    """주문 매니저 전용 KIS 지정가 전송 어댑터 계약입니다."""

    def submit_limit_order(
        self,
        *,
        ticker: str,
        side: OrderSide,
        quantity: int,
        price: int,
    ) -> Mapping[str, Any]:
        """KIS 지정가 주문을 한 번 전송합니다."""
        ...


@dataclass(frozen=True)
class OrderSubmissionResult:
    """KIS 접수 응답 또는 확인 대기 상태를 나타냅니다."""

    status: OrderStatus
    broker_order_number: str | None = None


def submit_recorded_limit_order(
    transport: LimitOrderTransport,
    *,
    gate_context: RealOrderGateContext,
    ticker: str,
    side: OrderSide,
    quantity: int,
    price: int,
) -> OrderSubmissionResult:
    """통과된 주문 게이트와 DB 기록이 있는 LIMIT 주문만 한 번 전송합니다."""
    if not validate_order_submission_gate(gate_context):
        raise PermissionError("Order submission blocked by the common safety gate.")
    if not isinstance(side, OrderSide):
        raise ValueError("side must be an OrderSide value.")
    if (
        not isinstance(ticker, str)
        or not ticker
        or isinstance(quantity, bool)
        or not isinstance(quantity, int)
        or quantity <= 0
        or isinstance(price, bool)
        or not isinstance(price, int)
        or price <= 0
    ):
        raise ValueError("Limit order fields are invalid.")
    try:
        response = transport.submit_limit_order(
            ticker=ticker,
            side=side,
            quantity=quantity,
            price=price,
        )
    except TimeoutError:
        return OrderSubmissionResult(OrderStatus.PENDING_CONFIRMATION)
    if not isinstance(response, Mapping):
        raise ValueError("KIS order response has an invalid shape.")
    if response.get("accepted") is True:
        order_number = response.get("order_number")
        if not isinstance(order_number, str) or not order_number:
            raise ValueError("Accepted KIS response is missing an order number.")
        return OrderSubmissionResult(OrderStatus.ACCEPTED, order_number)
    if response.get("rejected") is True:
        return OrderSubmissionResult(OrderStatus.REJECTED)
    return OrderSubmissionResult(OrderStatus.PENDING_CONFIRMATION)


def transition_order_status(
    old_status: OrderStatus,
    new_status: OrderStatus,
) -> OrderStatus:
    """종결 상태를 되돌리지 않고 허용된 주문 상태 전이만 반환합니다."""
    if not isinstance(old_status, OrderStatus) or not isinstance(new_status, OrderStatus):
        raise TypeError("Statuses must be OrderStatus values.")
    if new_status not in _ALLOWED_TRANSITIONS.get(old_status, set()):
        raise ValueError(f"Invalid order transition: {old_status.value} -> {new_status.value}")
    return new_status


@dataclass(frozen=True)
class PendingConfirmationEvidence:
    """브로커 조회가 충분히 완료됐는지 나타내는 권위 있는 복구 증거입니다.

    명세 7.3절의 두 가지 확정 경로를 표현합니다.
    ① 장 마감 후 완전 조회(`market_closed` + 두 조회 모두 완전·미발견)
    ② 장중 확정: SELL 의도 중 전량 매도에 한하며 120초 대기·체결통보 WebSocket
       연속 연결·접수 통보 없음·30초 이상 간격 연속 3회 미발견을 모두 만족해야 합니다.
    BUY 의도는 장중 확정하지 않습니다(중복 매수는 브로커가 막아 주지 않기 때문).
    """

    found_status: OrderStatus | None = None
    explicit_rejection: bool = False
    authoritative_acceptance_query_complete: bool = False
    authoritative_order_not_found: bool = False
    execution_query_complete: bool = False
    execution_not_found: bool = False
    market_closed: bool = False
    side: OrderSide | None = None
    full_quantity_sell: bool = False
    min_wait_elapsed: bool = False
    websocket_continuously_connected: bool = False
    acceptance_notice_seen: bool = False
    consecutive_empty_polls: int = 0


def resolve_pending_confirmation(
    evidence: PendingConfirmationEvidence,
) -> OrderStatus:
    """미발견 횟수만으로 재전송을 허용하지 않고 권위 있는 증거만 적용합니다."""
    if not isinstance(evidence, PendingConfirmationEvidence):
        raise TypeError("evidence must be PendingConfirmationEvidence.")
    if any(
        not isinstance(value, bool)
        for value in (
            evidence.explicit_rejection,
            evidence.authoritative_acceptance_query_complete,
            evidence.authoritative_order_not_found,
            evidence.execution_query_complete,
            evidence.execution_not_found,
            evidence.market_closed,
            evidence.full_quantity_sell,
            evidence.min_wait_elapsed,
            evidence.websocket_continuously_connected,
            evidence.acceptance_notice_seen,
        )
    ):
        raise ValueError("Recovery evidence flags must be booleans.")
    if (
        isinstance(evidence.consecutive_empty_polls, bool)
        or not isinstance(evidence.consecutive_empty_polls, int)
        or evidence.consecutive_empty_polls < 0
    ):
        raise ValueError("consecutive_empty_polls must be a non-negative integer.")
    if evidence.found_status is not None:
        if evidence.found_status not in {
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
        }:
            raise ValueError("Broker returned an invalid recovered order status.")
        return evidence.found_status
    if evidence.explicit_rejection:
        return OrderStatus.REJECTED

    both_queries_empty = (
        evidence.authoritative_acceptance_query_complete
        and evidence.authoritative_order_not_found
        and evidence.execution_query_complete
        and evidence.execution_not_found
    )
    if both_queries_empty and evidence.market_closed:
        return OrderStatus.CONFIRMED_NOT_ACCEPTED
    if (
        both_queries_empty
        and evidence.side is OrderSide.SELL
        and evidence.full_quantity_sell
        and evidence.min_wait_elapsed
        and evidence.websocket_continuously_connected
        and not evidence.acceptance_notice_seen
        and evidence.consecutive_empty_polls >= 3
    ):
        return OrderStatus.CONFIRMED_NOT_ACCEPTED
    return OrderStatus.PENDING_CONFIRMATION


def persist_order_transition(
    connection: sqlite3.Connection,
    *,
    order_id: str,
    new_status: OrderStatus,
    actor: str,
    reason_code: str,
    correlation_id: str,
    occurred_at: datetime,
    sanitized_payload: str | None = None,
) -> OrderStatus:
    """주문 상태와 감사 이벤트를 한 SQLite 트랜잭션으로 함께 기록합니다."""
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection.")
    for name, value in (
        ("order_id", order_id),
        ("actor", actor),
        ("reason_code", reason_code),
        ("correlation_id", correlation_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string.")
    if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware.")
    if sanitized_payload is not None and not isinstance(sanitized_payload, str):
        raise ValueError("sanitized_payload must be text or None.")

    row = connection.execute(
        "SELECT status FROM orders WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Order does not exist.")
    old_status = OrderStatus(row["status"])
    transition_order_status(old_status, new_status)
    timestamp = occurred_at.isoformat()
    with connection:
        connection.execute(
            """
            UPDATE orders
            SET status = ?, updated_at = ?
            WHERE order_id = ?
            """,
            (new_status.value, timestamp, order_id),
        )
        connection.execute(
            """
            INSERT INTO order_events (
                order_id, event_type, old_status, new_status, actor,
                reason_code, correlation_id, payload_sanitized, created_at
            ) VALUES (?, 'STATUS_TRANSITION', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                old_status.value,
                new_status.value,
                actor,
                reason_code,
                correlation_id,
                sanitized_payload,
                timestamp,
            ),
        )
    return new_status


# 명세 7.2절: KRX ETF 호가 단위 [UNVERIFIED: KRX 업무규정 현행값 구현 전 확인].
ETF_TICK_TABLE = ((2000, 1), (None, 5))


def etf_tick_size(price: int) -> int:
    """가격 구간별 ETF 호가 단위(2,000원 미만 1원, 이상 5원)를 반환합니다."""
    if isinstance(price, bool) or not isinstance(price, int) or price <= 0:
        raise ValueError("price must be a positive integer.")
    for upper, tick in ETF_TICK_TABLE:
        if upper is None or price < upper:
            return tick
    raise AssertionError("unreachable")


def round_down_to_tick(price: int, tick: int | None = None) -> int:
    """가격을 호가 단위 배수로 내림합니다(`tick` 생략 시 ETF 호가표)."""
    size = etf_tick_size(price) if tick is None else tick
    return price - price % size


def cancel_can_finalize(
    *,
    consecutive_matches: int,
    market_closed_full_query: bool,
    required_matches: int = 2,
) -> bool:
    """`CANCEL_PENDING → CANCELED` 확정 여부: 연속 일치 조회 또는 장 마감 후 완전 조회가 필요합니다.

    취소·정정 응답 직후에는 체결조회가 늦게 반영될 수 있으므로, 즉시 종결하지 않고 잔량·누적 체결
    수량이 연속 조회에서 같은 값으로 확인될 때만 종결합니다.
    """
    if isinstance(consecutive_matches, bool) or not isinstance(consecutive_matches, int):
        raise ValueError("consecutive_matches must be an integer.")
    return market_closed_full_query is True or consecutive_matches >= required_matches


def fill_delta(
    *,
    previous_cumulative_qty: int,
    previous_average_price: float,
    new_cumulative_qty: int,
    new_average_price: float,
) -> tuple[int, float]:
    """누적 체결(수량·평균가)의 증분(수량, 증분 평균가)을 계산합니다(명세 4.4절 fill 기록 규칙).

    누적 수량이 줄었으면 브로커 상태와 DB 가 모순이므로 ValueError 를 올립니다(호출자가
    `ACCOUNT_MISMATCH` 로 처리).
    """
    for name, value in (
        ("previous_cumulative_qty", previous_cumulative_qty),
        ("new_cumulative_qty", new_cumulative_qty),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer.")
    if new_cumulative_qty < previous_cumulative_qty:
        raise ValueError("Cumulative filled quantity decreased.")
    quantity = new_cumulative_qty - previous_cumulative_qty
    if quantity == 0:
        raise ValueError("No new fill quantity.")
    if (
        not isinstance(new_average_price, (int, float))
        or isinstance(new_average_price, bool)
        or not math.isfinite(new_average_price)
        or new_average_price <= 0
    ):
        raise ValueError("new_average_price must be a positive finite number.")
    total = new_average_price * new_cumulative_qty - previous_average_price * previous_cumulative_qty
    price = total / quantity
    if not math.isfinite(price) or price <= 0:
        raise ValueError("Derived fill price is invalid.")
    return quantity, price


def record_fill_from_cumulative(
    connection: sqlite3.Connection,
    *,
    order_id: str,
    new_cumulative_qty: int,
    new_average_price: float,
    executed_at: datetime,
    source: str = "REST_DELTA",
    fee: int = 0,
    tax: int = 0,
) -> bool:
    """누적 체결 증분으로 `trade_fills` 한 행을 기록하고 `orders` 수량을 갱신합니다.

    같은 누적 수량이 이미 기록돼 있으면(WS 통보와 REST 조회가 같은 체결을 본 경우) 중복 없이
    병합하고 False 를 반환합니다. 누적 수량이 감소하면 ValueError 입니다.
    """
    if source not in ("REST_DELTA", "WS_NOTICE"):
        raise ValueError("source must be REST_DELTA or WS_NOTICE.")
    if not isinstance(executed_at, datetime) or executed_at.tzinfo is None:
        raise ValueError("executed_at must be timezone-aware.")
    order = connection.execute(
        "SELECT ticker, side, requested_qty, kis_order_no, status, parent_intent_id "
        "FROM orders WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    if order is None:
        raise ValueError("Order does not exist.")
    last = connection.execute(
        """
        SELECT cumulative_qty,
               (SELECT SUM(qty * price) FROM trade_fills WHERE order_id = ?) AS notional
        FROM trade_fills WHERE order_id = ? ORDER BY cumulative_qty DESC LIMIT 1
        """,
        (order_id, order_id),
    ).fetchone()
    previous_qty = last["cumulative_qty"] if last else 0
    previous_avg = (last["notional"] / previous_qty) if last else 0.0
    if new_cumulative_qty == previous_qty:
        return False  # 이미 같은 누적 수량이 기록됨(병합)
    quantity, price = fill_delta(
        previous_cumulative_qty=previous_qty,
        previous_average_price=previous_avg,
        new_cumulative_qty=new_cumulative_qty,
        new_average_price=new_average_price,
    )
    if new_cumulative_qty > order["requested_qty"]:
        raise ValueError("Cumulative fill exceeds the requested quantity.")
    execution_id = f"{order['kis_order_no'] or order_id}:{new_cumulative_qty}"
    if source == "WS_NOTICE":
        execution_id = f"{order['kis_order_no'] or order_id}:WS:{new_cumulative_qty}"
    # 종결 주문의 늦은 체결 보정은 잔량을 0 으로 고정한다(DB 트리거 trg_terminal_immutable, 7.3절).
    is_terminal = order["status"] in {status.value for status in TERMINAL_ORDER_STATUSES}
    remaining = 0 if is_terminal else order["requested_qty"] - new_cumulative_qty
    with connection:
        connection.execute(
            """
            INSERT INTO trade_fills (
                order_id, kis_execution_id, ticker, side, qty, cumulative_qty,
                price, price_source, fee, tax, executed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id, execution_id, order["ticker"], order["side"], quantity,
                new_cumulative_qty, price, source, fee, tax, executed_at.isoformat(),
            ),
        )
        connection.execute(
            """
            UPDATE orders SET filled_qty = ?, remaining_qty = ?, updated_at = ?
            WHERE order_id = ? AND filled_qty < ?
            """,
            (new_cumulative_qty, remaining, executed_at.isoformat(),
             order_id, new_cumulative_qty),
        )
        _sync_obligation_filled_qty(connection, order["parent_intent_id"], executed_at)
    return True


def _sync_obligation_filled_qty(
    connection: sqlite3.Connection,
    parent_intent_id: str,
    occurred_at: datetime,
) -> None:
    """의도가 매도 의무에 연결돼 있으면 의무의 `filled_qty` 를 체결 기록 합계로 다시 계산합니다(7.3절).

    의무의 `filled_qty` 는 `obligation_id` 가 그 의무인 의도들의 `trade_fills.qty` 합계이며,
    체결 기록과 같은 트랜잭션에서 갱신합니다. 의무와 무관한 매도 체결은 집계하지 않습니다.
    합계가 필요 수량에 도달하면 `RESOLVED` 로 닫습니다.
    """
    link = connection.execute(
        "SELECT obligation_id FROM order_intents WHERE parent_intent_id = ?",
        (parent_intent_id,),
    ).fetchone()
    if link is None or link["obligation_id"] is None:
        return
    obligation_id = link["obligation_id"]
    filled = connection.execute(
        """
        SELECT COALESCE(SUM(f.qty), 0)
        FROM trade_fills AS f
        JOIN orders AS o ON o.order_id = f.order_id
        JOIN order_intents AS i ON i.parent_intent_id = o.parent_intent_id
        WHERE i.obligation_id = ?
        """,
        (obligation_id,),
    ).fetchone()[0]
    row = connection.execute(
        "SELECT required_qty, status FROM exit_obligations WHERE obligation_id = ?",
        (obligation_id,),
    ).fetchone()
    if row is None:
        return
    capped = min(filled, row["required_qty"])
    connection.execute(
        "UPDATE exit_obligations SET filled_qty = ? WHERE obligation_id = ?",
        (capped, obligation_id),
    )
    if capped >= row["required_qty"] and row["status"] == "OPEN":
        connection.execute(
            "UPDATE exit_obligations SET status = 'RESOLVED', resolved_at = ? WHERE obligation_id = ?",
            (occurred_at.isoformat(), obligation_id),
        )


def apply_late_fill_correction(
    connection: sqlite3.Connection,
    *,
    order_id: str,
    new_cumulative_qty: int,
    new_average_price: float,
    occurred_at: datetime,
) -> bool:
    """종결된 주문에 늦게 반영된 체결을 `LATE_FILL_CORRECTION` 으로 보정합니다(status 불변).

    체결 수량만 늘릴 수 있고(DB 트리거가 감소·상태 변경을 거부) 감사 이벤트를 남깁니다.
    """
    row = connection.execute(
        "SELECT status, filled_qty FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Order does not exist.")
    if row["status"] not in {s.value for s in TERMINAL_ORDER_STATUSES}:
        raise ValueError("Late fill correction applies only to terminal orders.")
    if new_cumulative_qty <= row["filled_qty"]:
        return False
    recorded = record_fill_from_cumulative(
        connection,
        order_id=order_id,
        new_cumulative_qty=new_cumulative_qty,
        new_average_price=new_average_price,
        executed_at=occurred_at,
    )
    if recorded:
        with connection:
            connection.execute(
                """
                INSERT INTO order_events (
                    order_id, event_type, old_status, new_status, actor,
                    reason_code, correlation_id, created_at
                ) VALUES (?, 'LATE_FILL_CORRECTION', ?, ?, 'order_manager',
                          'LATE_FILL_CORRECTION', ?, ?)
                """,
                (order_id, row["status"], row["status"], f"late:{order_id}",
                 occurred_at.isoformat()),
            )
    return recorded


def void_exit_obligation(
    connection: sqlite3.Connection,
    *,
    obligation_id: str,
    resolved_at: datetime,
) -> None:
    """보유 수량이 0이 된 의무를 `VOIDED` 로 닫습니다(수동 매도 등, 감사 기록 대상)."""
    with connection:
        cursor = connection.execute(
            """
            UPDATE exit_obligations SET status = 'VOIDED', resolved_at = ?
            WHERE obligation_id = ? AND status = 'OPEN'
            """,
            (resolved_at.isoformat(), obligation_id),
        )
    if cursor.rowcount != 1:
        raise ValueError("Only an OPEN exit obligation can be voided.")


def supersede_exit_obligation(
    connection: sqlite3.Connection,
    *,
    obligation_id: str,
    new_obligation: ExitObligation,
    created_at: datetime,
) -> None:
    """필요 수량이 바뀌면 기존 의무를 `SUPERSEDED` 로 닫고 같은 트랜잭션에서 새 `OPEN` 을 만듭니다."""
    with connection:
        old = connection.execute(
            "SELECT status FROM exit_obligations WHERE obligation_id = ?", (obligation_id,)
        ).fetchone()
        if old is None or old["status"] != "OPEN":
            raise ValueError("Only an OPEN exit obligation can be superseded.")
        active = connection.execute(
            """
            SELECT 1
            FROM order_intents AS i
            JOIN orders AS o ON o.parent_intent_id = i.parent_intent_id
            WHERE i.obligation_id = ?
              AND o.status IN ('CREATED', 'SUBMITTING', 'PENDING_CONFIRMATION',
                               'ACCEPTED', 'PARTIALLY_FILLED', 'CANCEL_PENDING')
            LIMIT 1
            """,
            (obligation_id,),
        ).fetchone()
        if active is not None:
            raise ValueError("An exit obligation with an active order cannot be superseded.")
        # 순서 고정(티커당 OPEN 1건 유일 인덱스·FK 즉시 검사): 기존 UPDATE → 새 INSERT → 기존 superseded_by.
        connection.execute(
            "UPDATE exit_obligations SET status = 'SUPERSEDED', resolved_at = ? WHERE obligation_id = ?",
            (created_at.isoformat(), obligation_id),
        )
        connection.execute(
            """
            INSERT INTO exit_obligations (
                obligation_id, ticker, source_event_id, required_qty,
                filled_qty, status, reason_code, created_at
            ) VALUES (?, ?, ?, ?, 0, 'OPEN', ?, ?)
            """,
            (
                new_obligation.obligation_id, new_obligation.ticker,
                new_obligation.source_event_id, new_obligation.required_qty,
                new_obligation.reason_code, created_at.isoformat(),
            ),
        )
        connection.execute(
            "UPDATE exit_obligations SET superseded_by = ? WHERE obligation_id = ?",
            (new_obligation.obligation_id, obligation_id),
        )


def create_kis_order_transport(environment: str, **kwargs: Any) -> "KisOrderAdapter":
    """주문 전송 어댑터를 만듭니다. 비공개 어댑터는 이 함수로만 노출됩니다.

    `http_post` 는 호출자가 명시적으로 주입해야 하며(기본값 없음), 실계좌 연결을 활성화하는 별도
    승인 전에는 어디에서도 주입하지 않습니다.
    """
    return KisOrderAdapter(environment, **kwargs)


# 명세 7.2절: 위험 축소 매도는 iNAV 가 유효할 때 Bid1 이 iNAV×(1-2%) 아래면 이상 시장으로 보고 제출하지 않습니다.
REDUCE_ONLY_NAV_FLOOR_RATIO = 0.020


class MarketAnomalyError(ValueError):
    """위험 축소 매도 호가가 iNAV 하한보다 낮아 당일 제출하지 않아야 하는 이상 시장입니다."""


def calculate_limit_price(
    quote: Quote,
    side: OrderSide,
    *,
    reduce_only: bool = False,
    tick: int | None = None,
) -> int:
    """일반 최우선 호가 또는 위험 축소 매도 가격(Bid1 1회)을 LIMIT으로 산출합니다.

    `tick` 을 생략하면 KRX ETF 호가표(2,000원 미만 1원, 이상 5원)로 가격마다 자동 선택합니다.
    위험 축소 매도(`reduce_only=True`)는 재가격하지 않고 Bid1 한 번만 쓰며, iNAV 가 유효한데
    Bid1 이 iNAV×(1-2%) 보다 낮으면 `MarketAnomalyError` 를 올려 당일 제출을 막습니다
    (iNAV 가 없거나 무효여도 위험 축소 매도 자체는 막지 않습니다).
    """
    if not isinstance(quote, Quote):
        raise TypeError("quote must be a Quote value.")
    if not isinstance(side, OrderSide):
        raise TypeError("side must be an OrderSide value.")
    if tick is not None and (
        isinstance(tick, bool)
        or not isinstance(tick, int)
        or tick <= 0
    ):
        raise ValueError("Tick must be positive.")
    if (
        isinstance(quote.bid, bool)
        or isinstance(quote.ask, bool)
        or not isinstance(quote.bid, int)
        or not isinstance(quote.ask, int)
        or quote.bid <= 0
        or quote.ask < quote.bid
    ):
        raise ValueError("Quote must have a positive, non-crossed integer spread.")

    if reduce_only:
        if side is not OrderSide.SELL:
            raise ValueError("Reduce-only limit price is only valid for SELL.")
        nav = quote.nav
        if (
            nav is not None
            and not isinstance(nav, bool)
            and isinstance(nav, (int, float))
            and math.isfinite(nav)
            and nav > 0
            and quote.bid < nav * (1 - REDUCE_ONLY_NAV_FLOOR_RATIO)
        ):
            raise MarketAnomalyError("Bid1 is below the iNAV floor; do not submit today.")
        return round_down_to_tick(quote.bid, tick)
    reference_price = quote.ask if side is OrderSide.BUY else quote.bid
    return round_down_to_tick(reference_price, tick)


@dataclass(frozen=True)
class RealOrderGateContext:
    """REAL 제출 직전 공통·전략·스모크 게이트의 검증 자료입니다."""

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
    intent_type: OrderIntentType = OrderIntentType.STRATEGY_REBALANCE
    common_order_manager_path: bool = True
    order_recordable: bool = True
    smoke_test_approved: bool = False
    smoke_test_ticker_allowed: bool = False
    smoke_test_order_limit: bool = False
    quantity_valid: bool = True
    cash_buffer_valid: bool = True
    liquidity_limit_valid: bool = True
    limit_order_only: bool = True
    # v3.7.5 D1: `SAFE_HALT` 중 보호성 손절. 공통 게이트 2번은 "KIS 잔고 조회가 TTL 이내 성공 + 수량 ≤ KIS 매도가능수량"
    # (호출자가 reconciliation_fresh·quantity_valid 로 전달)만 확인하고, 매수 전용 조건(미분류 현금흐름)은 적용하지 않는다.
    protective_exit: bool = False


def validate_real_order_gate(context: RealOrderGateContext) -> bool:
    """REAL 계좌 공통 8대 게이트와 목적별 제한을 확인합니다."""
    if not isinstance(context, RealOrderGateContext):
        raise TypeError("context must be a RealOrderGateContext value.")
    if not isinstance(context.intent_type, OrderIntentType):
        raise ValueError("intent_type must be an OrderIntentType value.")
    bool_fields = (
        context.operator_approval,
        context.reconciliation_ok,
        context.reconciliation_fresh,
        context.unresolved_order_exists,
        context.unclassified_cash_flow_exists,
        context.token_valid,
        context.calendar_valid,
        context.quote_valid,
        context.adjusted_data_valid,
        context.backtest_calibrated,
        context.mode_allows_order,
        context.idempotency_conflict,
        context.common_order_manager_path,
        context.order_recordable,
        context.smoke_test_approved,
        context.smoke_test_ticker_allowed,
        context.smoke_test_order_limit,
        context.quantity_valid,
        context.cash_buffer_valid,
        context.liquidity_limit_valid,
        context.limit_order_only,
        context.protective_exit,
    )
    if any(not isinstance(value, bool) for value in bool_fields):
        raise ValueError("REAL order gate fields must be booleans.")
    common_gates_pass = (
        context.environment == "REAL"
        and context.operator_approval
        and (context.reconciliation_ok or context.protective_exit)
        and context.reconciliation_fresh
        and not context.unresolved_order_exists
        and (not context.unclassified_cash_flow_exists or context.protective_exit)
        and context.token_valid
        and context.calendar_valid
        and context.quote_valid
        and context.adjusted_data_valid
        and context.mode_allows_order
        and not context.idempotency_conflict
        and context.common_order_manager_path
        and context.order_recordable
        and context.quantity_valid
        and context.cash_buffer_valid
        and context.liquidity_limit_valid
        and context.limit_order_only
    )
    if not common_gates_pass:
        return False
    if context.intent_type is OrderIntentType.STRATEGY_REBALANCE:
        return context.backtest_calibrated
    if context.intent_type is OrderIntentType.SMOKE_TEST:
        return (
            context.smoke_test_approved
            and context.smoke_test_ticker_allowed
            and context.smoke_test_order_limit
        )
    return True


def validate_order_submission_gate(context: RealOrderGateContext) -> bool:
    """PAPER·REAL 공통 게이트를 확인하고 REAL 주문에는 추가 게이트를 적용합니다."""
    if not isinstance(context, RealOrderGateContext):
        raise TypeError("context must be a RealOrderGateContext value.")
    if context.environment == "REAL":
        return validate_real_order_gate(context)
    if context.environment != "PAPER":
        return False
    if not isinstance(context.intent_type, OrderIntentType):
        raise ValueError("intent_type must be an OrderIntentType value.")
    bool_fields = (
        context.reconciliation_ok,
        context.reconciliation_fresh,
        context.unresolved_order_exists,
        context.unclassified_cash_flow_exists,
        context.token_valid,
        context.calendar_valid,
        context.quote_valid,
        context.adjusted_data_valid,
        context.mode_allows_order,
        context.idempotency_conflict,
        context.common_order_manager_path,
        context.order_recordable,
        context.quantity_valid,
        context.cash_buffer_valid,
        context.liquidity_limit_valid,
        context.limit_order_only,
        context.protective_exit,
    )
    if any(not isinstance(value, bool) for value in bool_fields):
        raise ValueError("PAPER order gate fields must be booleans.")
    return (
        context.intent_type is not OrderIntentType.SMOKE_TEST
        and (context.reconciliation_ok or context.protective_exit)
        and context.reconciliation_fresh
        and not context.unresolved_order_exists
        and (not context.unclassified_cash_flow_exists or context.protective_exit)
        and context.token_valid
        and context.calendar_valid
        and context.quote_valid
        and context.adjusted_data_valid
        and context.mode_allows_order
        and not context.idempotency_conflict
        and context.common_order_manager_path
        and context.order_recordable
        and context.quantity_valid
        and context.cash_buffer_valid
        and context.liquidity_limit_valid
        and context.limit_order_only
        )


def validate_spread_and_nav(
    quote: Quote,
    *,
    region: str,
) -> bool:
    """호가 스프레드와 NAV 괴리율을 수치 검증 후 적용합니다."""
    if not isinstance(quote, Quote):
        raise TypeError("quote must be a Quote value.")
    if region not in ("KR", "US"):
        raise ValueError("Region must be KR or US.")
    if (
        isinstance(quote.bid, bool)
        or isinstance(quote.ask, bool)
        or not isinstance(quote.bid, int)
        or not isinstance(quote.ask, int)
        or quote.bid <= 0
        or quote.ask <= 0
    ):
        return False
    if quote.ask < quote.bid:
        return False
    mid_price = (quote.bid + quote.ask) / 2
    if mid_price <= 0:
        return False
    spread_ratio = (quote.ask - quote.bid) / mid_price
    if spread_ratio > 0.003:
        return False
    if (
        quote.nav is None
        or isinstance(quote.nav, bool)
        or not isinstance(quote.nav, (int, float))
        or not math.isfinite(quote.nav)
        or quote.nav <= 0
    ):
        return False
    limit = 0.005 if region == "KR" else 0.012 if region == "US" else None
    return abs((quote.ask - quote.nav) / quote.nav) <= limit


def allows_buy_submission(
    *,
    data_fresh: bool,
    account_fresh: bool,
    account_matched: bool,
    unknown_order_exists: bool,
    unclassified_cash_flow_exists: bool = False,
    mode_allows_order: bool = True,
) -> bool:
    """신규 매수 전 필수 데이터·계좌·주문·현금흐름·모드 게이트를 확인합니다."""
    flags = (
        data_fresh,
        account_fresh,
        account_matched,
        unknown_order_exists,
        unclassified_cash_flow_exists,
        mode_allows_order,
    )
    if any(not isinstance(value, bool) for value in flags):
        raise ValueError("Buy gate inputs must be booleans.")
    return (
        data_fresh
        and account_fresh
        and account_matched
        and not unknown_order_exists
        and not unclassified_cash_flow_exists
        and mode_allows_order
    )


@dataclass(frozen=True)
class ExitObligation:
    """장 마감까지 해결하지 못한 위험 축소 매도 수량을 보존합니다."""

    obligation_id: str
    ticker: str
    source_event_id: str
    required_qty: int
    reason_code: str


def record_exit_obligation(
    connection: sqlite3.Connection,
    obligation: ExitObligation,
    *,
    created_at: datetime,
) -> bool:
    """OPEN 매도 의무를 저장하고 티커별 중복 의무를 DB에서 차단합니다."""
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection.")
    if not isinstance(obligation, ExitObligation):
        raise TypeError("obligation must be an ExitObligation value.")
    for name, value in (
        ("obligation_id", obligation.obligation_id),
        ("ticker", obligation.ticker),
        ("source_event_id", obligation.source_event_id),
        ("reason_code", obligation.reason_code),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string.")
    if (
        isinstance(obligation.required_qty, bool)
        or not isinstance(obligation.required_qty, int)
        or obligation.required_qty <= 0
    ):
        raise ValueError("required_qty must be a positive integer.")
    if not isinstance(created_at, datetime) or created_at.tzinfo is None:
        raise ValueError("created_at must be timezone-aware.")
    existing = connection.execute(
        """
        SELECT obligation_id, ticker, required_qty
        FROM exit_obligations
        WHERE source_event_id = ?
        """,
        (obligation.source_event_id,),
    ).fetchone()
    if existing is not None:
        if (
            existing["ticker"] != obligation.ticker
            or existing["required_qty"] != obligation.required_qty
        ):
            raise IntentConflictError("Exit obligation source event has conflicting data.")
        return False

    try:
        with connection:
            connection.execute(
                """
                INSERT INTO exit_obligations (
                    obligation_id, ticker, source_event_id, required_qty,
                    filled_qty, status, reason_code, created_at
                ) VALUES (?, ?, ?, ?, 0, 'OPEN', ?, ?)
                """,
                (
                    obligation.obligation_id,
                    obligation.ticker,
                    obligation.source_event_id,
                    obligation.required_qty,
                    obligation.reason_code,
                    created_at.isoformat(),
                ),
            )
        return True
    except sqlite3.IntegrityError:
        existing_open = connection.execute(
            """
            SELECT obligation_id
            FROM exit_obligations
            WHERE ticker = ? AND status = 'OPEN'
            """,
            (obligation.ticker,),
        ).fetchone()
        if existing_open is not None:
            raise IntentConflictError(
                "An OPEN exit obligation already exists for this ticker."
            )
        raise


def recover_exit_obligation_quantity(
    connection: sqlite3.Connection,
    *,
    obligation_id: str,
    broker_position_qty: int,
    reconciliation_ok: bool,
    existing_order_terminal: bool,
    account_data_fresh: bool,
    quote_and_adjustment_valid: bool,
    checked_at: datetime,
) -> int | None:
    """익일 검문소 조건이 모두 확인된 경우에만 미해결 매도 수량을 반환합니다."""
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection.")
    if not isinstance(obligation_id, str) or not obligation_id:
        raise ValueError("obligation_id must be a non-empty string.")
    if (
        isinstance(broker_position_qty, bool)
        or not isinstance(broker_position_qty, int)
        or broker_position_qty < 0
    ):
        raise ValueError("broker_position_qty must be a non-negative integer.")
    gate_values = (
        reconciliation_ok,
        existing_order_terminal,
        account_data_fresh,
        quote_and_adjustment_valid,
    )
    if any(not isinstance(value, bool) for value in gate_values):
        raise ValueError("Exit recovery gate inputs must be booleans.")
    if not isinstance(checked_at, datetime) or checked_at.tzinfo is None:
        raise ValueError("checked_at must be timezone-aware.")
    row = connection.execute(
        """
        SELECT ticker, required_qty, filled_qty, status
        FROM exit_obligations
        WHERE obligation_id = ?
        """,
        (obligation_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Exit obligation does not exist.")
    with connection:
        connection.execute(
            "UPDATE exit_obligations SET last_checked_at = ? WHERE obligation_id = ?",
            (checked_at.isoformat(), obligation_id),
        )
    if (
        row["status"] != "OPEN"
        or not reconciliation_ok
        or not existing_order_terminal
        or not account_data_fresh
        or not quote_and_adjustment_valid
    ):
        return None
    # 의무의 체결 수량은 연결된 의도의 `trade_fills` 합계만 인정한다(잔고 감소로 추정하지 않는다).
    filled = row["filled_qty"]
    if filled >= row["required_qty"]:
        with connection:
            connection.execute(
                "UPDATE exit_obligations SET status = 'RESOLVED', resolved_at = ? WHERE obligation_id = ?",
                (checked_at.isoformat(), obligation_id),
            )
        return 0
    if broker_position_qty == 0:
        # 대조 결과 보유 수량이 0 이면(수동 매도 등) 의무를 무효화한다(감사 기록 대상).
        with connection:
            connection.execute(
                "UPDATE exit_obligations SET status = 'VOIDED', resolved_at = ? WHERE obligation_id = ?",
                (checked_at.isoformat(), obligation_id),
            )
        return 0
    return min(row["required_qty"] - filled, broker_position_qty)


def orders_due_for_cancel(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    cancel_deadline: datetime,
) -> tuple[str, ...]:
    """마감 시각 이후 취소 가능한 모든 미체결 주문 ID를 정렬해 반환합니다."""
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection.")
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or not isinstance(cancel_deadline, datetime)
        or cancel_deadline.tzinfo is None
    ):
        raise ValueError("Cancellation timestamps must be timezone-aware.")
    if now < cancel_deadline:
        return ()
    rows = connection.execute(
        """
        SELECT order_id
        FROM orders
        WHERE status IN ('ACCEPTED', 'PARTIALLY_FILLED')
          AND remaining_qty > 0
        ORDER BY submitted_at, order_id
        """
    ).fetchall()
    return tuple(row["order_id"] for row in rows)
