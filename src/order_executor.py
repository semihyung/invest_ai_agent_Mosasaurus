"""위험 축소 매도의 "생성 → 게이트 → write-ahead 기록 → 1회 전송 → 결과 기록" 한 흐름을 담당합니다.

명세 6.1·6.3·7.3절. 이 모듈은 `order_manager` 의 부품(의도 저장·시도 기록·전송·전이)을
순서대로 엮을 뿐 새 주문 권한을 만들지 않습니다. 실제 전송은 주입된 `BrokerPort.transport`
(order_manager 가 만든 KIS 전송기) 한 곳으로만 나갑니다.

안전 규칙(요약)
- 진리표(`order_policy.is_allowed`)가 허용한 종류만 진행합니다. `SAFE_HALT` 에서는
  보호성 손절(`PROTECTIVE_EXIT_SELL`)만 예외입니다.
- 위험 축소 매도는 Bid1 **한 번**만 냅니다(재가격 없음). 남은 수량은 `exit_obligations` 로 넘깁니다.
- 같은 날 같은 종목의 같은 사유는 멱등키로 한 번만 만들어집니다.
- 전송 결과가 불확실(타임아웃)하면 `PENDING_CONFIRMATION` 으로 두고 절대 재전송하지 않습니다. 영향 범위는 주문 방향별로 한정됩니다
  (매수=계좌 신규 매수, 매도=해당 티커. `SAFE_HALT` 를 만들지 않음, 명세 v3.7.5 D3).
"""

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping, Protocol

from src.order_manager import (
    ExitObligation,
    IntentConflictError,
    MarketAnomalyError,
    OrderIntent,
    OrderIntentType,
    OrderSide,
    OrderStatus,
    Quote,
    RealOrderGateContext,
    calculate_limit_price,
    etf_tick_size,
    validate_spread_and_nav,
    persist_order_intent,
    persist_order_transition,
    record_exit_obligation,
    record_order_attempt,
    submit_recorded_limit_order,
)
from src.order_policy import OrderKind, is_allowed
from src.risk_manager import DrawdownMode, resolve_operating_mode
from src.safety_flags import active_codes, load_risk_flags, raise_flag

KST = timezone(timedelta(hours=9), name="KST")

_SELL_KINDS = {
    OrderKind.REDUCE_ONLY_SELL: OrderIntentType.RISK_REDUCTION,
    OrderKind.PROTECTIVE_EXIT_SELL: OrderIntentType.RISK_REDUCTION,
    OrderKind.EXIT_OBLIGATION_RETRY: OrderIntentType.EXIT_OBLIGATION_RETRY,
}
# 전략 주문(위험 축소가 아님): 호가 스프레드·iNAV 괴리 검증을 통과해야 한다(7.2절). 매수는 Ask1, 매도는 Bid1.
_STRATEGY_KINDS = {
    OrderKind.RISK_BUY: (OrderIntentType.STRATEGY_REBALANCE, OrderSide.BUY),
    OrderKind.RISK_TOP_UP_BUY: (OrderIntentType.STRATEGY_REBALANCE, OrderSide.BUY),
    OrderKind.RISK_ROTATION_SELL: (OrderIntentType.STRATEGY_REBALANCE, OrderSide.SELL),
    OrderKind.PARKING_BUY: (OrderIntentType.PARKING_ALLOCATION, OrderSide.BUY),
    OrderKind.PARKING_SELL: (OrderIntentType.PARKING_ALLOCATION, OrderSide.SELL),
}
_REDUCE_ONLY_KINDS = frozenset(_SELL_KINDS)
SELL_ALREADY_ACTIVE = "BLOCKED:SELL_ALREADY_ACTIVE"
# 증권사 조회 일시 실패: 의도를 만들지 않으므로 호출자가 다음 주기에 그대로 다시 시도한다(의무로 넘기지 않음).
QUOTE_UNAVAILABLE = "BLOCKED:QUOTE_UNAVAILABLE"
BROKER_UNAVAILABLE = "BLOCKED:BROKER_UNAVAILABLE"
QUOTE_INVALID = "BLOCKED:QUOTE_INVALID"  # Bid1/Ask1 가 0 이하이거나 교차: 계산을 생략하고 종목을 격리한다
OUTSIDE_ORDER_WINDOW = "BLOCKED:OUTSIDE_ORDER_WINDOW"  # 09:00 이전 또는 15:10 이후(취소는 예외, 7.1절 게이트 9)
SPREAD_WIDE = "BLOCKED:SPREAD_WIDE"  # 위험 축소 매도도 스프레드 1.0% 초과면 보류(7.2절 1번)
SPREAD_NAV = "BLOCKED:SPREAD_NAV"  # 전략 주문: 스프레드 0.3%·iNAV 괴리 한도 초과 → 의도를 만들지 않고 재평가
TRANSIENT_BLOCKS = frozenset({QUOTE_UNAVAILABLE, BROKER_UNAVAILABLE, QUOTE_INVALID, OUTSIDE_ORDER_WINDOW, SPREAD_WIDE})
ORDER_WINDOW = (time(9, 0), time(15, 10))  # market.regular_open ~ execution.new_order_cutoff
MAX_SPREAD_REDUCE_ONLY = 0.010  # execution.max_spread_ratio_reduce_only
RECONCILIATION_TTL_SECONDS = 300  # ttl_seconds.reconciliation


class BrokerPort(Protocol):
    """증권사 연동 계약. 운영에서는 KIS 어댑터가, 테스트에서는 가짜가 구현합니다."""

    environment: str  # "PAPER" | "REAL"
    transport: Any    # `submit_limit_order(ticker, side, quantity, price)` 를 가진 객체

    def quote(self, ticker: str) -> Quote:
        """최우선 호가(+iNAV 가 있으면) 조회."""
        ...

    def sellable_qty(self, ticker: str) -> int:
        """KIS 매도가능수량(`ord_psbl_qty`)."""
        ...


@dataclass(frozen=True)
class SellRequest:
    """매도 한 건의 요청. `cycle_id` 가 멱등키의 핵심이라 호출자가 사유·날짜로 결정적으로 만든다."""

    ticker: str
    quantity: int
    kind: OrderKind
    cycle_id: str
    reason: str
    obligation_id: str | None = None
    region: str = "KR"  # 전략 주문의 iNAV 괴리 한도 선택(KR/US)


def stop_cycle_id(ticker: str, trade_date: str) -> str:
    """스탑 이벤트 ID(`S{티커}:{거래일}`): 매도 의무의 `source_event_id` 로 쓴다(종목·거래일당 1건)."""
    return f"S{ticker}:{trade_date}"


def risk_cycle_id(trade_date: str, reason: str) -> str:
    """위험 축소 의도의 `cycle_id`(명세 7.1절): `R{거래일}:{STOP|MA120|REMOVE|VETO}`."""
    if reason not in ("STOP", "MA120", "REMOVE", "VETO"):
        raise ValueError("Unknown risk reduction reason.")
    return f"R{trade_date}:{reason}"


def weekly_cycle_id(trade_date: str) -> str:
    """주간 교체·리밸런싱·위험자산 매수 의도의 `cycle_id`(`W{ISO 연}-{주}`, 승계 실행도 같은 값)."""
    year, week, _ = datetime.fromisoformat(trade_date).isocalendar()
    return f"W{year}-{week:02d}"


def parking_cycle_id(trade_date: str) -> str:
    """개장일별 파킹 매수·현금화 매도의 `cycle_id`(`D{거래일}`)."""
    return f"D{trade_date}"


def obligation_cycle_id(obligation_id: str, trade_date: str) -> str:
    """의무 재주문 사이클: 의무당 하루 1회(`X{의무}:{거래일}`, 명세 7.3절)."""
    return f"X{obligation_id}:{trade_date}"


def _mode_inputs(connection: sqlite3.Connection, protective_enabled: bool):
    """DB 플래그에서 운영 모드·낙폭 단계·HALT 원인을 읽는다."""
    stage_row = connection.execute(
        "SELECT drawdown_stage FROM daily_portfolio ORDER BY snapshot_date DESC LIMIT 1"
    ).fetchone()
    stage = DrawdownMode(stage_row[0]) if stage_row else DrawdownMode.NORMAL
    flags = load_risk_flags(connection, drawdown_mode=stage, protective_exit_enabled=protective_enabled)
    return flags, stage


def open_exit_obligation(
    connection: sqlite3.Connection, *, ticker: str, quantity: int, reason: str, source_event_id: str, now: datetime
) -> bool:
    """매도 의무를 만든다. 이미 OPEN 이 있거나 같은 사건이면 False(중복 생성 안 함)."""
    obligation_id = "OB-" + hashlib.sha256(source_event_id.encode()).hexdigest()[:20]
    try:
        return record_exit_obligation(
            connection,
            ExitObligation(obligation_id, ticker, source_event_id, quantity, reason),
            created_at=now,
        )
    except IntentConflictError:
        return False


def submit_sell(connection: sqlite3.Connection, broker: BrokerPort, request: SellRequest, **kwargs: Any) -> str:
    """위험 축소 매도 전용 진입점(위험 축소 종류가 아니면 거부)."""
    if request.kind not in _SELL_KINDS:
        raise ValueError("submit_sell handles risk-reduction sell kinds only.")
    return submit_order(connection, broker, request, **kwargs)


def submit_order(
    connection: sqlite3.Connection,
    broker: BrokerPort,
    request: SellRequest,
    *,
    now: datetime,
    account_fresh: bool,
    protective_enabled: bool = True,
    operator_approval: bool = False,
    cash_buffer_valid: bool = True,
) -> str:
    """주문 1건을 안전하게 한 번 전송합니다. 결과 문자열을 반환합니다.

    `SUBMITTED` | `BLOCKED:<사유>` | `DUPLICATE` | `ANOMALY`(iNAV 하한 이탈, 당일 미제출).
    위험 축소 매도는 Bid1 1회, 전략 주문(교체·매수)은 스프레드·iNAV 검증 후 매수 Ask1 / 매도 Bid1 입니다.
    """
    if request.kind in _SELL_KINDS:
        intent_type, side, reduce_only = _SELL_KINDS[request.kind], OrderSide.SELL, True
    elif request.kind in _STRATEGY_KINDS:
        (intent_type, side), reduce_only = _STRATEGY_KINDS[request.kind], False
    else:
        raise ValueError("Unsupported order kind.")
    flags, stage = _mode_inputs(connection, protective_enabled)
    mode = resolve_operating_mode(flags)
    if not is_allowed(mode, stage, request.kind, halt_reasons=flags.halt_reasons,
                      protective_exit_enabled=protective_enabled):
        return f"BLOCKED:MODE_{mode.value}"

    if not ORDER_WINDOW[0] <= now.astimezone(KST).time() < ORDER_WINDOW[1]:
        return OUTSIDE_ORDER_WINDOW  # 7.1절 게이트 9: 신규 주문은 09:00~15:10 에만(취소는 예외)
    # 이미 만든 의도면 증권사 조회 없이 끝낸다(10초 주기 감시가 같은 호출을 반복하지 않게).
    key = OrderIntent(cycle_id=request.cycle_id, intent_type=intent_type, slot_id=None, ticker=request.ticker,
                      side=side, quantity=request.quantity, obligation_id=request.obligation_id).idempotency_key()
    if connection.execute("SELECT 1 FROM order_intents WHERE idempotency_key = ?", (key,)).fetchone():
        return "DUPLICATE"
    try:
        sellable = broker.sellable_qty(request.ticker) if side is OrderSide.SELL else request.quantity
    except Exception:  # noqa: BLE001 - 네트워크·한도 오류는 예외로 퍼뜨리지 않는다(잡 오류 → 주문 잠금 방지)
        return BROKER_UNAVAILABLE
    quantity = min(request.quantity, sellable)
    if quantity <= 0:
        return "BLOCKED:NO_SELLABLE_QTY"

    # 티커별 상호배제: 같은 종목에 활성 매도 주문이 있으면 서로 다른 의도의 동시 매도를 막는다(중복 매도·과매도 방지).
    if side is OrderSide.SELL and connection.execute(
            "SELECT 1 FROM orders WHERE ticker = ? AND side = 'SELL' AND status IN ('CREATED','SUBMITTING',"
            "'PENDING_CONFIRMATION','ACCEPTED','PARTIALLY_FILLED','CANCEL_PENDING') LIMIT 1",
            (request.ticker,)).fetchone():
        return SELL_ALREADY_ACTIVE

    try:
        quote = broker.quote(request.ticker)  # 의도를 저장하기 전에 조회: 실패해도 의도가 CREATED 로 고착되지 않는다
    except Exception:  # noqa: BLE001
        return QUOTE_UNAVAILABLE
    if not _quote_is_valid(quote):
        raise_flag(connection, "TICKER_BLOCK", "QUOTE_INVALID", now=now, ticker=request.ticker, detail="NON_POSITIVE_OR_CROSSED")
        return QUOTE_INVALID  # 의도는 만들지 않는다: 호가가 정상으로 돌아오면 다음 틱에 그대로 시도된다

    # 스프레드 검증은 의도를 만들기 전에 한다: 보류된 주문은 같은 cycle 로 다음 평가에서 그대로 다시 시도된다.
    spread = (quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2)
    if reduce_only:
        if spread > MAX_SPREAD_REDUCE_ONLY:
            return SPREAD_WIDE
    elif not validate_spread_and_nav(quote, region=request.region):
        return SPREAD_NAV

    intent = OrderIntent(
        cycle_id=request.cycle_id, intent_type=intent_type, slot_id=None,
        ticker=request.ticker, side=side, quantity=quantity,
        obligation_id=request.obligation_id,
    )
    result = persist_order_intent(
        connection, intent,
        policy_payload={"price_policy": "ASK1" if side is OrderSide.BUY else "BID1", "reduce_only": reduce_only},
        created_at=now,
    )
    if not result.created:
        return "DUPLICATE"

    try:
        price = calculate_limit_price(quote, side, reduce_only=reduce_only)
    except MarketAnomalyError:
        _close_intent(connection, result.parent_intent_id, now)
        return "ANOMALY"

    codes = set(active_codes(connection, "BUY_BLOCK")) | set(active_codes(connection, "HALT"))
    gate = RealOrderGateContext(
        environment=broker.environment,
        operator_approval=operator_approval,
        reconciliation_ok=_state(connection, "reconciliation_ok") == "true",
        reconciliation_fresh=account_fresh and (request.kind is OrderKind.PROTECTIVE_EXIT_SELL
                                                or _reconciliation_fresh(connection, now)),
        unresolved_order_exists=_unresolved_order_exists(connection, request.ticker, side),
        unclassified_cash_flow_exists="UNCLASSIFIED_CASH_FLOW" in codes,
        token_valid=True, calendar_valid="CALENDAR_MISMATCH" not in codes,
        quote_valid=True, adjusted_data_valid=request.ticker not in _blocked_tickers(connection),
        backtest_calibrated=False, mode_allows_order=True, idempotency_conflict=False,
        intent_type=intent_type, cash_buffer_valid=cash_buffer_valid,
        quantity_valid=quantity <= sellable,
        protective_exit=request.kind is OrderKind.PROTECTIVE_EXIT_SELL,
    )
    order_id = "O-" + hashlib.sha256(f"{result.parent_intent_id}:1".encode()).hexdigest()[:20]
    attempt_id = "A-" + hashlib.sha256(f"{order_id}:attempt".encode()).hexdigest()[:20]
    # write-ahead: KIS 호출 전에 SUBMITTING 기록을 먼저 커밋한다(정전 시 고아 주문 방지).
    record_order_attempt(
        connection, order_id=order_id, parent_intent_id=result.parent_intent_id, attempt_id=attempt_id,
        order_price=price, strategy_reason=request.reason, submitted_at=now,
        request_payload_hash=hashlib.sha256(f"{request.ticker}:{quantity}:{price}".encode()).hexdigest(),
    )
    try:
        outcome = submit_recorded_limit_order(
            broker.transport, gate_context=gate, ticker=request.ticker, side=side,
            quantity=quantity, price=price,
        )
    except PermissionError:
        # 게이트 불통과: 전송하지 않았으므로 종결 처리한다(재가격·재전송 없음). 실패한 게이트 번호를 이벤트 사유로 남긴다(7.1절).
        _expire_unsent(connection, order_id, now, reason=f"GATE_{_first_failed_gate(gate) or 0}_FAILED")
        return "BLOCKED:GATE"
    except Exception:
        # 응답 형식 이상 등 불확실: 전송됐을 수 있으므로 확인 대기로 두고 재전송하지 않는다.
        persist_order_transition(
            connection, order_id=order_id, new_status=OrderStatus.PENDING_CONFIRMATION, actor="order_executor",
            reason_code="SUBMIT_RESPONSE_UNCERTAIN", correlation_id=attempt_id, occurred_at=now)
        return "SUBMITTED"
    persist_order_transition(
        connection, order_id=order_id, new_status=outcome.status, actor="order_executor",
        reason_code="SUBMIT_RESULT", correlation_id=attempt_id, occurred_at=now)
    if outcome.broker_order_number:
        with connection:
            connection.execute("UPDATE orders SET kis_order_no = ? WHERE order_id = ?",
                               (outcome.broker_order_number, order_id))
    return "SUBMITTED"


def _reconciliation_fresh(connection: sqlite3.Connection, now: datetime) -> bool:
    """마지막 성공 대조가 `ttl_seconds.reconciliation`(300초) 이내인지(7.1절 게이트 2)."""
    last = _state(connection, "last_reconciliation_at")
    if not last:
        return False
    return (now - datetime.fromisoformat(last)).total_seconds() <= RECONCILIATION_TTL_SECONDS


def _first_failed_gate(ctx: RealOrderGateContext) -> int | None:
    """7.1절 공통 게이트 중 처음 실패한 번호(1~8). 모두 통과하면 None."""
    protective = ctx.protective_exit
    if ctx.environment not in ("PAPER", "REAL") or (ctx.environment == "REAL" and not ctx.operator_approval):
        return 1
    if not ((ctx.reconciliation_ok or protective) and ctx.reconciliation_fresh):
        return 2
    if ctx.unresolved_order_exists:
        return 3
    if not (ctx.token_valid and ctx.calendar_valid and ctx.quote_valid and ctx.adjusted_data_valid) \
            or (ctx.unclassified_cash_flow_exists and not protective):
        return 4
    if not (ctx.quantity_valid and ctx.cash_buffer_valid and ctx.liquidity_limit_valid and ctx.limit_order_only):
        return 5
    if not ctx.mode_allows_order:
        return 6
    if ctx.idempotency_conflict:
        return 7
    if not (ctx.common_order_manager_path and ctx.order_recordable):
        return 8
    return None


def _quote_is_valid(quote: Quote) -> bool:
    """호가가 양의 정수이고 매수호가 ≤ 매도호가인지(0 나누기·가격 0 가드, 명세 시나리오 61)."""
    return (isinstance(quote, Quote) and all(isinstance(v, int) and not isinstance(v, bool) for v in (quote.bid, quote.ask))
            and quote.bid > 0 and quote.ask > quote.bid  # 명세 7.2절: Ask1 > Bid1
            and quote.bid % etf_tick_size(quote.bid) == 0 and quote.ask % etf_tick_size(quote.ask) == 0)


def _unresolved_order_exists(connection: sqlite3.Connection, ticker: str, side: OrderSide) -> bool:
    """7.1절 게이트 3(v3.7.5 D3): 불확실 주문의 영향 범위를 주문 방향별로 한정한다.

    매수는 계좌 전체의 불확실 매수 주문 또는 같은 티커의 미확정·취소대기 주문이 있으면 막히고,
    매도는 같은 티커의 불확실 주문만 막는다(다른 티커의 위험 축소 매도는 허용, 수량은 KIS 매도가능수량으로 제한).
    """
    same_ticker = ("SELECT 1 FROM orders WHERE ticker = ? AND status IN ('SUBMITTING', 'PENDING_CONFIRMATION'%s) LIMIT 1"
                   % (", 'CANCEL_PENDING'" if side is OrderSide.BUY else ""))
    if connection.execute(same_ticker, (ticker,)).fetchone():
        return True
    return side is OrderSide.BUY and connection.execute(
        "SELECT 1 FROM orders WHERE side = 'BUY' AND status IN ('SUBMITTING', 'PENDING_CONFIRMATION') LIMIT 1"
    ).fetchone() is not None


def _state(connection: sqlite3.Connection, key: str) -> str | None:
    """system_state 한 값을 읽는다(없으면 None)."""
    row = connection.execute('SELECT value FROM system_state WHERE "key" = ?', (key,)).fetchone()
    return row[0] if row else None


def _blocked_tickers(connection: sqlite3.Connection) -> set[str]:
    """활성 종목 격리(TICKER_BLOCK) 플래그가 걸린 티커 집합."""
    return {r[0] for r in connection.execute(
        "SELECT ticker FROM safety_flags WHERE flag_type = 'TICKER_BLOCK' AND cleared_at IS NULL")}


def _close_intent(connection: sqlite3.Connection, parent_intent_id: str, now: datetime) -> None:
    """제출하지 않기로 한 의도를 닫는다."""
    with connection:
        connection.execute("UPDATE order_intents SET status = 'CLOSED', updated_at = ? WHERE parent_intent_id = ?",
                           (now.isoformat(), parent_intent_id))


def _expire_unsent(connection: sqlite3.Connection, order_id: str, now: datetime, reason: str = "GATE_BLOCKED_NOT_SENT") -> None:
    """게이트에서 막혀 전송되지 않은 SUBMITTING 시도를 REJECTED 로 닫는다(실제 전송 없음)."""
    persist_order_transition(
        connection, order_id=order_id, new_status=OrderStatus.REJECTED, actor="order_executor",
        reason_code=reason, correlation_id=order_id, occurred_at=now)
    row = connection.execute("SELECT parent_intent_id FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    _close_intent(connection, row[0], now)
