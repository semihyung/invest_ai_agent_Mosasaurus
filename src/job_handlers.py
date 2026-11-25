"""스케줄러 잡 핸들러(명세 9.1절): 스탑 감시·09:10 검문소·15:14:30 취소·마감 후 대조/스냅샷·안전 점검.

각 팩토리는 `connection_factory`, `BrokerPort`(증권사 연동), `clock` 을 주입받아 인자 없는 콜백을
돌려줍니다(`scheduler.build_scheduler` 규격). 증권사 호출 실패는 "불일치"가 아니라
`ACCOUNT_DATA_STALE`(신규 매수 차단)로만 처리합니다(명세 4.3절). 이 모듈은 주문을 직접 전송하지 않고
`order_executor.submit_sell` 한 곳만 호출합니다.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Protocol

from src.cash_recon import (
    DiscrepancyDecision,
    excluded_unclassified_amount, hwm_frozen_for_cash_flow, in_learning_period,
    reconciliation_allowed, record_discrepancy, unexplained_cash_delta,
)
from src.order_executor import (
    KST, SELL_ALREADY_ACTIVE, TRANSIENT_BLOCKS, BrokerPort, SellRequest, obligation_cycle_id, open_exit_obligation,
    risk_cycle_id, stop_cycle_id,
    submit_sell,
)
from src.order_manager import OrderStatus, orders_due_for_cancel, persist_order_transition, void_exit_obligation
from src.order_policy import OrderKind
from src.order_sync import sync_orders
from src.rate_limit import guard_poll_interval
from src.scheduler_jobs import deliver_previews
from src.performance import (
    PerformanceState, TWRQualityStatus, initialize_flow_adjusted_performance, rebase_hwm,
    update_flow_adjusted_performance,
)
from src.reconciliation import OrderSnapshot, Position, ReconciliationResult, persist_reconciliation_result, reconcile_account_state
from src.scheduler import is_order_scheduler_locked, set_order_scheduler_lock
from src.risk_manager import DrawdownMode, get_drawdown_mode, next_drawdown_stage, should_trailing_sell
from src.safety_flags import (
    active_codes, clear_flag, due_unresolved_halt_alerts, load_risk_flags, raise_flag, ticker_is_blocked,
    sync_host_state_flags,
)

Notify = Callable[[str, str], None]  # (severity, message) → Telegram 등. 실패해도 잡을 막지 않는다.


class AccountBrokerPort(BrokerPort, Protocol):
    """조회 계약 추가분: 잔고·주문·시세 조회."""

    def last_price(self, ticker: str) -> float:
        """종목 현재가(원주가)."""
    def positions(self) -> list[Position]:
        """증권사 보유 종목·수량."""
    def orders(self, trade_date: date) -> list[OrderSnapshot]:
        """지정 거래일의 주문·체결 내역(정규화된 상태)."""
    def cash_d2(self) -> int:
        """D+2 정산 예수금."""
    def total_equity(self) -> int:
        """총평가금액."""
    def cancel_order(self, kis_order_no: str, ticker: str) -> bool:
        """미체결 주문 취소를 요청하고 접수 여부를 돌려준다."""


def _noop(_severity: str, _message: str) -> None:
    """알림 콜백이 없을 때 쓰는 빈 함수."""


def _safe_notify(notify: Notify, severity: str, message: str) -> None:
    """알림 실패가 매매 잡을 중단시키지 않도록 삼킨다(감사 기록은 system_events 에 이미 남음)."""
    try:
        notify(severity, message)
    except Exception:  # noqa: BLE001 - 알림 채널 장애는 별도 헬스체크 대상
        pass


def _log(connection: sqlite3.Connection, component: str, event_type: str, severity: str, message: str,
         now: datetime) -> None:
    """system_events 에 한 줄 기록한다(민감정보 없는 문구만)."""
    with connection:
        connection.execute(
            "INSERT INTO system_events (component, event_type, severity, message, created_at) VALUES (?,?,?,?,?)",
            (component, event_type, severity, message, now.isoformat()))


def _adjust_factor(connection: sqlite3.Connection, ticker: str) -> float | None:
    """최근 일봉의 조정계수 A = adjusted_close / raw_close (실시간 가격을 수정주가로 환산)."""
    row = connection.execute(
        "SELECT adjusted_close, raw_close FROM daily_candles WHERE ticker = ? ORDER BY trade_date DESC LIMIT 1",
        (ticker,)).fetchone()
    return float(row[0]) / float(row[1]) if row and row[1] else None


TRANSIENT_PREFIX = "TRANSIENT:"


def _mark_stale(connection: sqlite3.Connection, now: datetime, why: str) -> None:
    """조회 실패는 불일치가 아니다: 신규 매수만 막고 경보한다(일시 실패 표식 → 다음 성공 조회에서 자동 해제)."""
    raise_flag(connection, "BUY_BLOCK", "ACCOUNT_DATA_STALE", now=now, detail=(TRANSIENT_PREFIX + why)[:200])


def _clear_transient_stale(connection: sqlite3.Connection, now: datetime) -> None:
    """조회가 다시 성공했으면 일시 실패로 걸린 `ACCOUNT_DATA_STALE` 만 해제한다(대조 불일치 대기 플래그는 건드리지 않음)."""
    row = connection.execute(
        "SELECT detail_sanitized FROM safety_flags WHERE flag_type = 'BUY_BLOCK' AND code = 'ACCOUNT_DATA_STALE' "
        "AND cleared_at IS NULL").fetchone()
    if row is not None and (row[0] or "").startswith(TRANSIENT_PREFIX):
        clear_flag(connection, "BUY_BLOCK", "ACCOUNT_DATA_STALE", now=now, cleared_by="SYSTEM")


def _set_guard_interval(connection: sqlite3.Connection, interval: int, now: datetime) -> None:
    """현재 스탑 감시 폴링 주기(초)를 기록한다(10초 초과 = 한도 때문에 연장됨)."""
    row = connection.execute("SELECT value FROM system_state WHERE \"key\" = 'guard_poll_interval_seconds'").fetchone()
    if row is not None and row[0] == str(interval):
        return
    with connection:
        connection.execute(
            "INSERT INTO system_state (\"key\", value, updated_at) VALUES ('guard_poll_interval_seconds', ?, ?) "
            "ON CONFLICT(\"key\") DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (str(interval), now.isoformat()))


# ───────────────────────── intraday_guard / intraday_guard_alert_only ─────────────────────────

def _set_state_kv(connection: sqlite3.Connection, key: str, value: str, now: datetime) -> None:
    """system_state 한 값을 UPSERT 한다."""
    with connection:
        connection.execute(
            'INSERT INTO system_state ("key", value, updated_at) VALUES (?, ?, ?) '
            'ON CONFLICT("key") DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at',
            (key, value, now.isoformat()))


def auto_emergency_if_critical(connection: sqlite3.Connection, drawdown: float, now: datetime) -> bool:
    """유량 보정 낙폭이 CRITICAL(-15%)이면 `EMERGENCY`(원인 DRAWDOWN)를 자동으로 켠다(6.2절). 새로 켰으면 True.

    이미 EMERGENCY 이거나, 직전 해제로 HWM 리베이스가 예약돼 있으면(해제 직후 재발동 금지) 아무것도 하지 않는다.
    """
    if get_drawdown_mode(max(-1.0, min(0.0, drawdown))) is not DrawdownMode.CRITICAL:
        return False
    state = {r[0]: r[1] for r in connection.execute(
        "SELECT \"key\", value FROM system_state WHERE \"key\" IN ('emergency_mode', 'hwm_rebase_pending')")}
    if state.get("emergency_mode") == "true" or state.get("hwm_rebase_pending") == "true":
        return False
    _set_state_kv(connection, "emergency_mode", "true", now)
    _set_state_kv(connection, "emergency_reason", "DRAWDOWN", now)
    _log(connection, "risk", "EMERGENCY_AUTO", "CRITICAL", f"낙폭 {drawdown * 100:.1f}%: EMERGENCY 자동 발동", now)
    return True


def intraday_drawdown(connection: sqlite3.Connection, equity: int) -> float | None:
    """마지막 마감 스냅샷의 단위 수·HWM 으로 현재 평가액의 낙폭을 추정한다(미분류 현금흐름은 오염 제거)."""
    row = connection.execute("SELECT units_outstanding, hwm_flow_adjusted, unclassified_excluded_amount "
                             "FROM daily_portfolio ORDER BY snapshot_date DESC LIMIT 1").fetchone()
    if row is None or row["units_outstanding"] <= 0 or row["hwm_flow_adjusted"] <= 0:
        return None
    unit_nav = max(0.0, equity - row["unclassified_excluded_amount"]) / row["units_outstanding"]
    return min(0.0, unit_nav / row["hwm_flow_adjusted"] - 1.0)


def _emergency_by_drawdown(connection: sqlite3.Connection) -> bool:
    """마지막 스냅샷 낙폭 단계가 CRITICAL 이면 EMERGENCY 로 본다(6.3절 파생 규칙)."""
    row = connection.execute("SELECT drawdown_stage FROM daily_portfolio ORDER BY snapshot_date DESC LIMIT 1").fetchone()
    return row is not None and row[0] == "CRITICAL"


def _quote_recovered(connection: sqlite3.Connection, broker: "AccountBrokerPort", ticker: str, now: datetime) -> bool:
    """`QUOTE_INVALID` 로만 격리된 종목의 호가가 정상으로 돌아왔으면 격리를 풀고 True."""
    if active_codes(connection, "TICKER_BLOCK", ticker=ticker) != ("QUOTE_INVALID",):
        return False
    try:
        quote = broker.quote(ticker)
        if not (quote.bid > 0 and quote.ask >= quote.bid):
            return False
    except Exception:  # noqa: BLE001
        return False
    clear_flag(connection, "TICKER_BLOCK", "QUOTE_INVALID", now=now, cleared_by="SYSTEM", ticker=ticker)
    return True


def create_intraday_guard_handler(
    connection_factory: Callable[[], sqlite3.Connection],
    broker: AccountBrokerPort,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    notify: Notify = _noop,
    protective_enabled: bool = True,
    alert_only_from: time = time(15, 10),
    rate_per_second: float = 1.0,
) -> Callable[[], None]:
    """보유 종목의 트레일링 스탑(-10%/훈련 -7%)을 감시합니다(6.1절).

    15:10 이전: 스탑 도달 시 위험 축소 매도 1회(Bid1). `SAFE_HALT` 라면 보호성 손절 게이트만 통과해야 하며
    그 외 정지 원인이면 주문하지 않고 의무만 기록합니다. 15:10 이후: 주문 없이 의무 기록·경보만.
    `OPEN` 의무가 있는 종목은 의무 재주문이 담당하므로 건너뜁니다.
    """

    last_run: list[datetime] = []
    last_dd: list[datetime] = []
    last_recon: list[datetime] = []

    def handler() -> None:
        """스탑 감시 한 틱: 보유 종목을 점검하고 스탑 도달 시 매도 또는 의무 기록을 한다."""
        now = clock()
        connection = connection_factory()
        try:
            # 4.4절: 보유 종목 수 × 1콜이 초당 한도를 넘으면 폴링 주기를 자동 연장하고 /health 에 표시한다.
            held = connection.execute("SELECT COUNT(*) FROM positions WHERE tracking_status = 'MANAGED' "
                                      "AND quantity > 0").fetchone()[0]
            interval = guard_poll_interval(held, rate_per_second)
            if last_run and (now - last_run[0]).total_seconds() < interval - 0.5:
                return
            last_run[:] = [now]
            _set_guard_interval(connection, interval, now)
            trade_date = now.astimezone(KST).date().isoformat()
            alert_only = now.astimezone(KST).time() >= alert_only_from
            flags = load_risk_flags(connection, protective_exit_enabled=protective_enabled)
            rows = connection.execute(
                "SELECT ticker, quantity, peak_price_adj FROM positions "
                "WHERE tracking_status = 'MANAGED' AND quantity > 0").fetchall()
            if not last_recon or (now - last_recon[0]).total_seconds() >= 120:
                last_recon[:] = [now]
                try:  # 7.1절 게이트 2: 마지막 성공 대조 300초 TTL 을 유지한다(장중 2분 주기)
                    if reconcile_intraday(connection, broker, now) == "CONFIRMED":
                        _safe_notify(notify, "CRITICAL", "계좌 불일치 확정 → SAFE_HALT(ACCOUNT_MISMATCH)")
                except Exception as error:  # noqa: BLE001 - 조회 실패는 STALE(일시) 처리, 다음 주기에 재시도
                    _mark_stale(connection, now, f"intraday_recon:{type(error).__name__}")
            if not last_dd or (now - last_dd[0]).total_seconds() >= 60:
                last_dd[:] = [now]
                try:  # 6.2절: 장중 유량 보정 낙폭 감시 — CRITICAL(-15%) 도달 시 EMERGENCY 자동 전이
                    drawdown = intraday_drawdown(connection, broker.total_equity())
                    if drawdown is not None and auto_emergency_if_critical(connection, drawdown, now):
                        _safe_notify(notify, "CRITICAL", f"낙폭 {drawdown * 100:.1f}%: EMERGENCY 자동 발동(위험 축소 매도만 허용)")
                except Exception:  # noqa: BLE001 - 잔고 조회 실패는 STALE 처리 대상이 아니라 다음 주기에 재시도
                    pass
            if flags.emergency_mode or _emergency_by_drawdown(connection):
                cancel_active_buys(connection, broker, now)  # EMERGENCY: 미체결 매수 전량 취소(6.2절)
            failed = False
            for row in rows:
                ticker, quantity, peak = row["ticker"], row["quantity"], row["peak_price_adj"]
                if ticker_is_blocked(connection, ticker) and not _quote_recovered(connection, broker, ticker, now):
                    continue  # 4.2절: 격리(기업행위 미검증·호가 무효 등) 중에는 가격 기반 자동 매도를 보류한다
                if peak is None:
                    continue  # 고점 기록 없는 보유분은 대상 제외(스탑 감시 불가 → 별도 경보 대상)
                if connection.execute("SELECT 1 FROM exit_obligations WHERE ticker=? AND status='OPEN'",
                                      (ticker,)).fetchone():
                    continue
                factor = _adjust_factor(connection, ticker)
                if factor is None:
                    continue
                try:
                    price = broker.last_price(ticker)
                except Exception as error:  # noqa: BLE001 - 조회 실패는 STALE 처리
                    _mark_stale(connection, now, f"last_price:{type(error).__name__}")
                    failed = True
                    continue
                if not should_trailing_sell(price * factor, float(peak), training_mode=flags.training_mode):
                    continue
                event_id = stop_cycle_id(ticker, trade_date)
                if alert_only:
                    if open_exit_obligation(connection, ticker=ticker, quantity=quantity,
                                            reason="TRAILING_STOP_AFTER_CUTOFF", source_event_id=event_id, now=now):
                        msg = f"{ticker} 스탑 도달(15:10 이후): 주문 없이 매도 의무 기록, 익일 09:10 재매도"
                        _log(connection, "intraday_guard", "STOP_AFTER_CUTOFF", "CRITICAL", msg, now)
                        _safe_notify(notify, "CRITICAL", msg)
                    continue
                kind = (OrderKind.PROTECTIVE_EXIT_SELL if flags.is_safe_halt else OrderKind.REDUCE_ONLY_SELL)
                result = submit_sell(
                    connection, broker,
                    SellRequest(ticker, quantity, kind, risk_cycle_id(trade_date, "STOP"), "TRAILING_STOP"),
                    now=now, protective_enabled=protective_enabled)
                if result == SELL_ALREADY_ACTIVE or result in TRANSIENT_BLOCKS:
                    continue  # 진행 중이거나 조회 일시 실패: 의무·경보 없이 다음 틱에 다시 시도한다
                if result.startswith("BLOCKED") or result == "ANOMALY":
                    # 못 낸 매도는 사라지지 않게 의무로 남긴다(익일 09:10 재시도).
                    open_exit_obligation(connection, ticker=ticker, quantity=quantity,
                                         reason=f"STOP_{result}", source_event_id=event_id, now=now)
                if result != "DUPLICATE":
                    msg = f"{ticker} 트레일링 스탑 → {result}"
                    _log(connection, "intraday_guard", "STOP_TRIGGERED", "CRITICAL", msg, now)
                    _safe_notify(notify, "CRITICAL", msg)
            if rows and not failed:
                _clear_transient_stale(connection, now)
        finally:
            connection.close()

    return handler


# ───────────────────────── open_checkpoint (09:10) ─────────────────────────

def create_open_checkpoint_handler(
    connection_factory: Callable[[], sqlite3.Connection],
    broker: AccountBrokerPort,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    notify: Notify = _noop,
    protective_enabled: bool = True,
) -> Callable[[], None]:
    """전일 `OPEN` 매도 의무를 최우선으로 재매도합니다(7.3절).

    보유가 이미 0이면 `VOIDED`, 기존 주문이 아직 활성이면 건너뜁니다. 남은 수량(필요-체결)을
    Bid1 1회로 냅니다. 의무당 하루 1회(`X{의무}:{거래일}` 멱등키).
    """

    def handler() -> None:
        """09:10 점검: 열린 매도 의무를 재매도하거나 보유가 없으면 닫는다."""
        now = clock()
        connection = connection_factory()
        try:
            trade_date = now.astimezone(KST).date().isoformat()
            for ob in connection.execute(
                    "SELECT obligation_id, ticker, required_qty, filled_qty FROM exit_obligations "
                    "WHERE status = 'OPEN' ORDER BY created_at").fetchall():
                held = connection.execute("SELECT quantity FROM positions WHERE ticker = ?",
                                          (ob["ticker"],)).fetchone()
                if held is None or held[0] <= 0:
                    void_exit_obligation(connection, obligation_id=ob["obligation_id"], resolved_at=now)
                    continue
                if connection.execute(
                        "SELECT 1 FROM orders o JOIN order_intents i USING (parent_intent_id) "
                        "WHERE i.obligation_id = ? AND o.status IN ('CREATED','SUBMITTING','PENDING_CONFIRMATION',"
                        "'ACCEPTED','PARTIALLY_FILLED','CANCEL_PENDING') LIMIT 1", (ob["obligation_id"],)).fetchone():
                    continue
                remaining = min(ob["required_qty"] - ob["filled_qty"], held[0])
                if remaining <= 0:
                    continue
                result = submit_sell(
                    connection, broker,
                    SellRequest(ob["ticker"], remaining, OrderKind.EXIT_OBLIGATION_RETRY,
                                obligation_cycle_id(ob["obligation_id"], trade_date), "EXIT_OBLIGATION_RETRY",
                                obligation_id=ob["obligation_id"]),
                    now=now, protective_enabled=protective_enabled)
                if result != "DUPLICATE":
                    msg = f"{ob['ticker']} 매도 의무 재주문({remaining}주) → {result}"
                    _log(connection, "open_checkpoint", "OBLIGATION_RETRY", "WARN", msg, now)
                    _safe_notify(notify, "WARN", msg)
        finally:
            connection.close()

    return handler


# ───────────────────────── order_cancel_sweep (15:14:30) ─────────────────────────

def create_order_cancel_sweep_handler(
    connection_factory: Callable[[], sqlite3.Connection],
    broker: AccountBrokerPort,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    cancel_time: time = time(15, 14, 30),
) -> Callable[[], None]:
    """주문번호가 있는 모든 활성 주문의 취소를 요청하고 `CANCEL_PENDING` 으로 기록합니다.

    최종 `CANCELED`·잔량 확정은 15:40 마감 후 대조가 KIS 조회로 확정합니다(취소 응답만 믿지 않음).
    미체결로 남은 위험 축소 매도는 즉시 매도 의무로 넘깁니다.
    """

    def handler() -> None:
        """15:14:30 취소 정리: 활성 주문 취소를 요청하고 미체결 위험 축소 매도를 의무로 넘긴다."""
        now = clock()
        connection = connection_factory()
        try:
            deadline = datetime.combine(now.astimezone(KST).date(), cancel_time, tzinfo=KST)
            for order_id in orders_due_for_cancel(connection, now=now, cancel_deadline=deadline):
                row = connection.execute(
                    "SELECT o.kis_order_no, o.ticker, o.side, o.remaining_qty, o.attempt_id, i.intent_type "
                    "FROM orders o JOIN order_intents i USING (parent_intent_id) WHERE o.order_id = ?",
                    (order_id,)).fetchone()
                if not row["kis_order_no"]:
                    continue
                try:
                    accepted = broker.cancel_order(row["kis_order_no"], row["ticker"])
                except Exception:  # noqa: BLE001 - 취소 응답 유실: 상태 유지, 마감 후 대조가 확정
                    accepted = False
                if accepted:
                    persist_order_transition(
                        connection, order_id=order_id, new_status=OrderStatus.CANCEL_PENDING,
                        actor="cancel_sweep", reason_code="PRE_CLOSE_CANCEL", correlation_id=row["attempt_id"],
                        occurred_at=now)
                if row["side"] == "SELL" and row["intent_type"] in ("RISK_REDUCTION", "EXIT_OBLIGATION_RETRY"):
                    open_exit_obligation(
                        connection, ticker=row["ticker"], quantity=row["remaining_qty"],
                        reason="UNFILLED_AT_CLOSE", source_event_id=f"U{order_id}", now=now)
        finally:
            connection.close()

    return handler


# ───────────────────────── post_close_reconcile (15:40) ─────────────────────────

def _load_performance(connection: sqlite3.Connection) -> tuple[PerformanceState | None, DrawdownMode]:
    """마지막 `daily_portfolio` 행에서 성과 상태와 낙폭 단계를 복원한다."""
    row = connection.execute("SELECT * FROM daily_portfolio ORDER BY snapshot_date DESC LIMIT 1").fetchone()
    if row is None:
        return None, DrawdownMode.NORMAL
    nav = row["flow_adjusted_unit_nav"]
    if nav is None or row["units_outstanding"] <= 0:
        return None, DrawdownMode(row["drawdown_stage"])
    state = PerformanceState(
        unit_nav=nav, units=row["units_outstanding"], hwm=row["hwm_flow_adjusted"],
        twr_cumulative=row["twr_cumulative"], drawdown=min(0.0, nav / row["hwm_flow_adjusted"] - 1.0),
        as_of=datetime.fromisoformat(row["snapshot_date"] + "T00:00:00+09:00"), last_close_unit_nav=nav,
        epoch=row["performance_epoch"], twr_quality_status=TWRQualityStatus(row["twr_quality_status"]))
    return state, DrawdownMode(row["drawdown_stage"])


def save_daily_snapshot(connection: sqlite3.Connection, *, trade_date: str, total_equity: int, cash: int,
                        now: datetime) -> str | None:
    """TWR/HWM 일간 스냅샷을 저장합니다(6.2절). 성과 상태를 만들 수 없으면 None."""
    state, previous_stage = _load_performance(connection)
    if state is None:
        state = initialize_flow_adjusted_performance(starting_equity=total_equity, as_of=now)
        if state is None:
            return None
    frozen = hwm_frozen_for_cash_flow(connection)
    excluded = excluded_unclassified_amount(connection)
    new = update_flow_adjusted_performance(
        state, total_equity=total_equity, as_of=max(now, state.as_of), close_snapshot=True, freeze=frozen,
        unclassified_excluded_amount=excluded)
    rebase = connection.execute("SELECT value FROM system_state WHERE \"key\" = 'hwm_rebase_pending'").fetchone()
    if rebase is not None and rebase[0] == "true" and not frozen:
        new = rebase_hwm(new)  # EMERGENCY(낙폭 원인) 해제 후 첫 스냅샷: HWM 만 현재 단위가격으로
    floor_row = connection.execute(
        "SELECT value FROM system_state WHERE \"key\" = 'post_emergency_floor_until'").fetchone()
    floor = DrawdownMode.WARNING if floor_row and floor_row[0] and trade_date <= floor_row[0] else None
    stage = next_drawdown_stage(previous_stage, new.drawdown, frozen=frozen, floor_stage=floor)
    category = {r[0]: r[1] for r in connection.execute(
        "SELECT p.ticker, u.category FROM positions p LEFT JOIN universe_assets u USING (ticker)")}
    values = {"SAFE_PARKING": 0, "RISK_ASSET": 0, "UNMANAGED": 0}
    for r in connection.execute("SELECT ticker, market_value, tracking_status FROM positions"):
        key = "UNMANAGED" if r["tracking_status"] == "UNMANAGED" else category.get(r["ticker"], "UNMANAGED")
        values[key] += r["market_value"]
    with connection:
        connection.execute(
            """
            INSERT INTO daily_portfolio (
                snapshot_date, performance_epoch, total_equity, cash_balance, parking_value, risk_asset_value,
                unmanaged_value, unclassified_excluded_amount, units_outstanding, flow_adjusted_unit_nav,
                hwm_flow_adjusted, hwm_frozen, drawdown_stage, twr_cumulative, twr_quality_status,
                benchmark_values, data_quality_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'{}','VALID')
            ON CONFLICT(snapshot_date) DO UPDATE SET
                total_equity=excluded.total_equity, cash_balance=excluded.cash_balance,
                parking_value=excluded.parking_value, risk_asset_value=excluded.risk_asset_value,
                unmanaged_value=excluded.unmanaged_value,
                unclassified_excluded_amount=excluded.unclassified_excluded_amount,
                units_outstanding=excluded.units_outstanding, flow_adjusted_unit_nav=excluded.flow_adjusted_unit_nav,
                hwm_flow_adjusted=excluded.hwm_flow_adjusted, hwm_frozen=excluded.hwm_frozen,
                drawdown_stage=excluded.drawdown_stage, twr_cumulative=excluded.twr_cumulative
            """,
            (trade_date, new.epoch, total_equity, cash, values["SAFE_PARKING"], values["RISK_ASSET"],
             values["UNMANAGED"], int(excluded), new.units, new.unit_nav, new.hwm, int(frozen), stage.value,
             new.twr_cumulative, new.twr_quality_status.value))
        if new.hwm_rebased:
            connection.execute("UPDATE daily_portfolio SET hwm_rebased = 1 WHERE snapshot_date = ?", (trade_date,))
            connection.execute("UPDATE system_state SET value = 'false', updated_at = ? "
                               "WHERE \"key\" = 'hwm_rebase_pending'", (now.isoformat(),))
    return stage.value


def _update_peaks(connection: sqlite3.Connection, now: datetime, live_closes: dict[str, float] | None = None) -> None:
    """`PeakPrice` 일 1회 갱신: 보유 종목의 최신 수정 종가로 단조 증가시킨다(6.1절).

    DB 일봉은 다음 날 아침에야 적재되므로, 마감 후에는 당일 종가(= 마감 후 현재가 × 조정계수)를 함께 반영해
    고점이 하루 늦어지지 않게 한다(`live_closes`: 티커 → 마감 후 현재가).
    """
    for ticker, close in (live_closes or {}).items():
        factor = _adjust_factor(connection, ticker) or 1.0
        with connection:
            connection.execute("UPDATE positions SET peak_price_adj = MAX(peak_price_adj, ?) "
                               "WHERE ticker = ? AND peak_price_adj IS NOT NULL", (close * factor, ticker))
    with connection:
        connection.execute(
            """
            UPDATE positions SET peak_price_adj = (
                SELECT MAX(positions.peak_price_adj, c.adjusted_close) FROM daily_candles c
                WHERE c.ticker = positions.ticker ORDER BY c.trade_date DESC LIMIT 1),
                last_synced_at = ?
            WHERE tracking_status = 'MANAGED' AND quantity > 0 AND peak_price_adj IS NOT NULL
              AND EXISTS (SELECT 1 FROM daily_candles c WHERE c.ticker = positions.ticker)
            """, (now.isoformat(),))


def reconcile_cash(
    connection: sqlite3.Connection,
    *,
    current_cash: int,
    now: datetime,
    trade_day: date,
    advance_baseline: bool,
    first_run_date: date | None = None,
    tolerance_krw: int = 1000,
    confirm_interval_seconds: int = 60,
) -> DiscrepancyDecision | None:
    """D+2 정산 예수금 대사(명세 2.2·5.4절): 직전 기준 예수금 + 당일 체결 현금 변동 = 기대 예수금 과 실제를 비교한다.

    `advance_baseline=False`(15:00 파이프라인)는 판정만 하고 기준 예수금을 옮기지 않는다(마감 후 대사가 같은 날 체결을
    다시 합산하므로 옮기면 이중 계산된다). 첫 관측·대기 중에는 어느 경우든 기준을 옮기지 않는다.
    기준값이 없으면(첫 실행) 판정 없이 None 을 돌려주고, advance_baseline 이면 기준만 기록한다.
    """
    previous = connection.execute(
        "SELECT value, updated_at FROM system_state WHERE \"key\" = 'last_d2_cash'").fetchone()
    decision = None
    move = advance_baseline
    if previous is not None:
        # 기준 예수금을 기록한 시각 이후의 체결을 모두 더한다. "오늘 날짜 체결"만 더하면 기준을 못 옮긴 날의 체결이
        # 다음 날 빠져 차이가 영영 첫 관측과 같아지지 않는다(WAIT 고착). julianday 는 시간대 표기가 달라도 같은 순간을 비교한다.
        trade_cash = connection.execute(
            "SELECT COALESCE(SUM(CASE side WHEN 'SELL' THEN qty*price ELSE -qty*price END - fee - tax),0) "
            "FROM trade_fills WHERE julianday(executed_at) > julianday(?)", (previous["updated_at"],)).fetchone()[0]
        delta = unexplained_cash_delta(int(previous[0]), int(round(trade_cash)), current_cash,
                                       tolerance_krw=tolerance_krw)
        first_row = connection.execute("SELECT MIN(snapshot_date) FROM daily_portfolio").fetchone()[0]
        first = first_run_date or (date.fromisoformat(first_row) if first_row else trade_day)
        decision = record_discrepancy(connection, delta, trade_date=trade_day, now=now,
                                      confirm_interval_seconds=confirm_interval_seconds,
                                      tolerance_krw=tolerance_krw,
                                      learning_period=in_learning_period(first, trade_day))
        # 확정 전(첫 관측·대기)에는 기준 예수금을 옮기지 않는다: 옮기면 다음 대사가 차이를 0 으로 보고
        # 방금 관측한 변동을 스스로 무효화(DISMISSED)해 버린다.
        move = move and decision not in (DiscrepancyDecision.PROVISIONAL, DiscrepancyDecision.WAIT)
    if move:
        with connection:
            connection.execute(
                "INSERT INTO system_state (\"key\", value, updated_at) VALUES ('last_d2_cash', ?, ?) "
                "ON CONFLICT(\"key\") DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (str(current_cash), now.isoformat()))
    return decision


def _reconcile_with(connection, broker_positions, broker_orders, now, trade_day, confirm_interval_seconds):
    """브로커 보유·주문과 DB 를 대조하고 결과를 기록한다(마감 후·장중 공통). `(결과, 판정, 정지상태 여부)` 를 돌려준다."""
    active = connection.execute(
        "SELECT COUNT(*) FROM orders WHERE status IN ('CREATED','SUBMITTING','PENDING_CONFIRMATION',"
        "'ACCEPTED','PARTIALLY_FILLED','CANCEL_PENDING')").fetchone()[0]
    last_fill = connection.execute("SELECT MAX(executed_at) FROM trade_fills").fetchone()[0]
    quiescent = reconciliation_allowed(
        active_order_count=active, now=now,
        last_fill_at=datetime.fromisoformat(last_fill) if last_fill else None)
    sqlite_positions = [Position(r["ticker"], r["quantity"]) for r in connection.execute(
        "SELECT ticker, quantity FROM positions WHERE quantity > 0 AND tracking_status = 'MANAGED'")]
    sqlite_orders = [OrderSnapshot(r["kis_order_no"], r["ticker"], r["side"], r["requested_qty"],
                                   r["filled_qty"], r["remaining_qty"], r["status"])
                     for r in connection.execute(
        "SELECT * FROM orders WHERE kis_order_no IS NOT NULL AND submitted_at >= ?",
        (datetime.combine(trade_day, time(0), tzinfo=KST).isoformat(),))]
    unmanaged = {r[0] for r in connection.execute(
        "SELECT ticker FROM positions WHERE tracking_status = 'UNMANAGED'")}
    result = reconcile_account_state(
        broker_positions=[p for p in broker_positions if p.ticker not in unmanaged],
        sqlite_positions=sqlite_positions, broker_orders=broker_orders, sqlite_orders=sqlite_orders,
        account_data_fresh=True,
        unclassified_cash_flow_exists="UNCLASSIFIED_CASH_FLOW" in active_codes(connection, "BUY_BLOCK"))
    verdict = persist_reconciliation_result(
        connection, result, checked_at=now, confirm_interval_seconds=confirm_interval_seconds,
        quiescent=quiescent)
    return result, verdict, quiescent


def compare_account(connection: sqlite3.Connection, broker_positions, broker_orders, trade_day: date) -> ReconciliationResult:
    """브로커와 DB 를 비교만 한다(DB 에 아무것도 쓰지 않음 — `/reconcile` 읽기·대조 전용, 명세 6.4절)."""
    sqlite_positions = [Position(r["ticker"], r["quantity"]) for r in connection.execute(
        "SELECT ticker, quantity FROM positions WHERE quantity > 0 AND tracking_status = 'MANAGED'")]
    sqlite_orders = [OrderSnapshot(r["kis_order_no"], r["ticker"], r["side"], r["requested_qty"], r["filled_qty"],
                                   r["remaining_qty"], r["status"]) for r in connection.execute(
        "SELECT * FROM orders WHERE kis_order_no IS NOT NULL AND submitted_at >= ?",
        (datetime.combine(trade_day, time(0), tzinfo=KST).isoformat(),))]
    unmanaged = {r[0] for r in connection.execute("SELECT ticker FROM positions WHERE tracking_status = 'UNMANAGED'")}
    return reconcile_account_state(
        broker_positions=[p for p in broker_positions if p.ticker not in unmanaged],
        sqlite_positions=sqlite_positions, broker_orders=broker_orders, sqlite_orders=sqlite_orders,
        account_data_fresh=True, unclassified_cash_flow_exists=False)


def cancel_active_buys(connection: sqlite3.Connection, broker: AccountBrokerPort, now: datetime) -> int:
    """EMERGENCY 중 미체결 매수 주문을 모두 취소 요청한다(6.2절 CRITICAL·/emergency). 요청한 건수를 돌려준다."""
    cancelled = 0
    for row in connection.execute(
            "SELECT order_id, kis_order_no, ticker, attempt_id FROM orders WHERE side = 'BUY' AND kis_order_no IS NOT NULL "
            "AND status IN ('ACCEPTED', 'PARTIALLY_FILLED') AND remaining_qty > 0").fetchall():
        try:
            accepted = broker.cancel_order(row["kis_order_no"], row["ticker"])
        except Exception:  # noqa: BLE001 - 취소 응답 유실: 상태 유지, 다음 틱에 다시 요청(취소는 사실상 멱등)
            continue
        if accepted:
            persist_order_transition(connection, order_id=row["order_id"], new_status=OrderStatus.CANCEL_PENDING,
                                     actor="emergency", reason_code="EMERGENCY_CANCEL_BUYS",
                                     correlation_id=row["attempt_id"], occurred_at=now)
            cancelled += 1
    return cancelled


def reconcile_intraday(
    connection: sqlite3.Connection, broker: AccountBrokerPort, now: datetime, confirm_interval_seconds: int = 60
) -> str:
    """장중 대조(보유 수량·주문, 명세 9.1절 `intraday_guard`): 마지막 성공 대조가 300초 TTL 안에 있도록 갱신한다.

    먼저 주문·체결을 DB 에 반영(`sync_orders`)한 뒤 비교한다. 활성 주문이 있으면 불일치 판정은 건너뛴다(2.2절).
    """
    sync_orders(connection, broker, now=now, market_closed=False)
    trade_day = now.astimezone(KST).date()
    _, verdict, _ = _reconcile_with(connection, broker.positions(), broker.orders(trade_day), now, trade_day,
                                    confirm_interval_seconds)
    return verdict


def create_post_close_reconcile_handler(
    connection_factory: Callable[[], sqlite3.Connection],
    broker: AccountBrokerPort,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    notify: Notify = _noop,
    first_run_date: date | None = None,
    cash_tolerance_krw: int = 1000,
    confirm_interval_seconds: int = 60,
) -> Callable[[], None]:
    """장 마감 후 KIS 계좌·주문을 대조하고, 예수금 대사와 일간 TWR/HWM 스냅샷을 저장합니다(9.1절).

    정지 상태(활성 주문 0건, 마지막 체결 후 60초)가 아니면 판정을 건너뜁니다(가짜 불일치 방지).
    조회 실패는 `ACCOUNT_DATA_STALE`(신규 매수 차단)로만 처리합니다.
    """

    def handler() -> None:
        """마감 후 대조: 주문 동기화, 계좌 대조, 예수금 대사, 최고가 갱신, 일간 스냅샷을 순서대로 수행한다."""
        now = clock()
        connection = connection_factory()
        try:
            trade_day = now.astimezone(KST).date()
            try:
                sync_orders(connection, broker, now=now, market_closed=True)  # 대조 전에 DB 를 브로커 상태로 먼저 맞춘다
                broker_positions = broker.positions()
                broker_orders = broker.orders(trade_day)
                current_cash, equity = broker.cash_d2(), broker.total_equity()
            except Exception as error:  # noqa: BLE001
                _mark_stale(connection, now, f"{type(error).__name__}")
                _safe_notify(notify, "WARN", "마감 후 대조 조회 실패: 신규 매수 차단")
                return
            result, verdict, quiescent = _reconcile_with(
                connection, broker_positions, broker_orders, now, trade_day, confirm_interval_seconds)
            if verdict == "CONFIRMED":
                _safe_notify(notify, "CRITICAL", "계좌 불일치 확정 → SAFE_HALT(ACCOUNT_MISMATCH)")
            if result.reconciliation_ok and is_order_scheduler_locked(connection):
                try:  # 대조 성공 + SAFE_HALT 없음이면 잠금을 푼다(조건은 set_order_scheduler_lock 이 검증)
                    set_order_scheduler_lock(connection, locked=False, reason="RECONCILIATION_REQUIRED", occurred_at=now)
                except PermissionError:
                    pass
            if not quiescent:
                return  # 진행 중인 주문이 있으면 예수금 대사·스냅샷도 다음 주기로 미룬다.
            reconcile_cash(connection, current_cash=current_cash, now=now, trade_day=trade_day,
                           advance_baseline=True, first_run_date=first_run_date, tolerance_krw=cash_tolerance_krw,
                           confirm_interval_seconds=confirm_interval_seconds)
            live_closes: dict[str, float] = {}
            for r in connection.execute("SELECT ticker FROM positions WHERE tracking_status = 'MANAGED' "
                                        "AND quantity > 0").fetchall():
                try:
                    live_closes[r["ticker"]] = broker.last_price(r["ticker"])
                except Exception:  # noqa: BLE001 - 종가 조회 실패 종목은 DB 일봉 기준 갱신만
                    continue
            _update_peaks(connection, now, live_closes)
            try:
                stage = save_daily_snapshot(connection, trade_date=trade_day.isoformat(), total_equity=equity,
                                            cash=current_cash, now=now)
                if stage == DrawdownMode.CRITICAL.value:
                    row = connection.execute("SELECT hwm_flow_adjusted, flow_adjusted_unit_nav FROM daily_portfolio "
                                             "WHERE snapshot_date = ?", (trade_day.isoformat(),)).fetchone()
                    if row and auto_emergency_if_critical(connection, row[1] / row[0] - 1.0, now):
                        _safe_notify(notify, "CRITICAL", "낙폭 -15% 도달: EMERGENCY 자동 발동")
            except ValueError as error:
                raise_flag(connection, "BUY_BLOCK", "ACCOUNTING_INVALID", now=now, detail=str(error)[:200])
        finally:
            connection.close()

    return handler


# ───────────────────────── safety_flag_auto_clear / 24h 재경보 ─────────────────────────

def create_safety_maintenance_handler(
    connection_factory: Callable[[], sqlite3.Connection],
    *,
    run_dir: str | None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    notify: Notify = _noop,
    token_valid: Callable[[], bool] | None = None,
) -> Callable[[], None]:
    """10분마다: 호스트 상태 파일로 UPS·시각 플래그를 갱신하고, 24시간 이상 풀리지 않은 HALT 를 재경보합니다."""

    def handler() -> None:
        """활성 주문이 있을 때만 브로커 상태를 DB 에 반영한다."""
        now = clock()
        connection = connection_factory()
        try:
            if run_dir:
                sync_host_state_flags(connection, run_dir, now=now)
            until = connection.execute("SELECT value FROM system_state WHERE \"key\" = 'training_until'").fetchone()
            mode = connection.execute("SELECT value, updated_at FROM system_state WHERE \"key\" = 'training_mode'").fetchone()
            if mode and mode[0] == "true" and not (until and until[0]):
                started = datetime.fromisoformat(mode[1])
                today = now.astimezone(KST).date().isoformat()
                alerted = connection.execute("SELECT value FROM system_state WHERE \"key\" = 'training_alert_date'").fetchone()
                if (now - started).days >= 7 and (alerted is None or alerted[0] != today):
                    _set_state_kv(connection, "training_alert_date", today, now)
                    _safe_notify(notify, "WARN", f"훈련 모드가 {(now - started).days}일째 무기한으로 유지 중입니다(위험자산 매수 차단).")
            if until and until[0] and now.astimezone(KST).date().isoformat() > until[0]:
                with connection:  # 훈련 종료일 도래: 자동 해제
                    connection.execute("UPDATE system_state SET value = CASE \"key\" WHEN 'training_mode' THEN 'false' "
                                       "ELSE '' END, updated_at = ? WHERE \"key\" IN ('training_mode','training_until')",
                                       (now.isoformat(),))
                _safe_notify(notify, "INFO", "훈련 모드 종료일이 지나 자동 해제되었습니다.")
            deliver_previews(connection, notify, now)  # 발송 실패한 주간 프리뷰 재시도(10분 간격, 최대 6회)
            if token_valid is not None:  # 토큰 무효 → CREDENTIALS_UNAVAILABLE(SAFE_HALT), 유효해지면 자동 해제
                if token_valid():
                    clear_flag(connection, "HALT", "CREDENTIALS_UNAVAILABLE", now=now, cleared_by="SYSTEM")
                elif raise_flag(connection, "HALT", "CREDENTIALS_UNAVAILABLE", now=now, detail="TOKEN_INVALID"):
                    _safe_notify(notify, "CRITICAL", "KIS 토큰이 없거나 만료되었습니다: 주문 정지")
            for code in due_unresolved_halt_alerts(connection, now=now):
                _safe_notify(notify, "CRITICAL", f"{code} 미해결 24시간 이상: 운영자 조치 필요")
        finally:
            connection.close()

    return handler


# ───────────────────────── pending_confirmation_poller (15초) ─────────────────────────

def create_pending_confirmation_poller(
    connection_factory: Callable[[], sqlite3.Connection],
    broker: AccountBrokerPort,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    notify: Notify = _noop,
) -> Callable[[], None]:
    """활성 주문이 있을 때만 브로커 상태를 체결·접수·취소로 반영합니다(장중에는 미접수 확정 안 함)."""

    def handler() -> None:
        """10분 주기 안전 점검: 호스트 상태 플래그, 훈련 종료, 프리뷰 재발송, 미해결 정지 재경보."""
        now = clock()
        connection = connection_factory()
        try:
            if connection.execute("SELECT 1 FROM orders WHERE status IN ('ACCEPTED','PARTIALLY_FILLED',"
                                  "'CANCEL_PENDING','PENDING_CONFIRMATION') LIMIT 1").fetchone() is None:
                return
            try:
                result = sync_orders(connection, broker, now=now, market_closed=False)
            except Exception as error:  # noqa: BLE001 - 조회 실패는 STALE(매수 차단)로만 처리
                _mark_stale(connection, now, f"sync:{type(error).__name__}")
                return
            _clear_transient_stale(connection, now)
            if result["filled"]:
                _safe_notify(notify, "INFO", f"체결 {result['filled']}건 반영")
        finally:
            connection.close()

    return handler
