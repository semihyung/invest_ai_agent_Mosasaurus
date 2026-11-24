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
    KST, BrokerPort, SellRequest, obligation_cycle_id, open_exit_obligation, stop_cycle_id, submit_sell,
)
from src.order_manager import OrderStatus, orders_due_for_cancel, persist_order_transition, void_exit_obligation
from src.order_policy import OrderKind
from src.order_sync import sync_orders
from src.performance import (
    PerformanceState, TWRQualityStatus, initialize_flow_adjusted_performance, rebase_hwm,
    update_flow_adjusted_performance,
)
from src.reconciliation import OrderSnapshot, Position, persist_reconciliation_result, reconcile_account_state
from src.risk_manager import DrawdownMode, next_drawdown_stage, should_trailing_sell
from src.safety_flags import (
    active_codes, due_unresolved_halt_alerts, load_risk_flags, raise_flag,
    sync_host_state_flags,
)

Notify = Callable[[str, str], None]  # (severity, message) → Telegram 등. 실패해도 잡을 막지 않는다.


class AccountBrokerPort(BrokerPort, Protocol):
    """조회 계약 추가분: 잔고·주문·시세 조회."""

    def last_price(self, ticker: str) -> float: ...
    def positions(self) -> list[Position]: ...
    def orders(self, trade_date: date) -> list[OrderSnapshot]: ...
    def cash_d2(self) -> int: ...
    def total_equity(self) -> int: ...
    def cancel_order(self, kis_order_no: str, ticker: str) -> bool: ...


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


def _mark_stale(connection: sqlite3.Connection, now: datetime, why: str) -> None:
    """조회 실패는 불일치가 아니다: 신규 매수만 막고 경보한다."""
    raise_flag(connection, "BUY_BLOCK", "ACCOUNT_DATA_STALE", now=now, detail=why[:200])


# ───────────────────────── intraday_guard / intraday_guard_alert_only ─────────────────────────

def create_intraday_guard_handler(
    connection_factory: Callable[[], sqlite3.Connection],
    broker: AccountBrokerPort,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    notify: Notify = _noop,
    protective_enabled: bool = True,
    alert_only_from: time = time(15, 10),
) -> Callable[[], None]:
    """보유 종목의 트레일링 스탑(-10%/훈련 -7%)을 감시합니다(6.1절).

    15:10 이전: 스탑 도달 시 위험 축소 매도 1회(Bid1). `SAFE_HALT` 라면 보호성 손절 게이트만 통과해야 하며
    그 외 정지 원인이면 주문하지 않고 의무만 기록합니다. 15:10 이후: 주문 없이 의무 기록·경보만.
    `OPEN` 의무가 있는 종목은 의무 재주문이 담당하므로 건너뜁니다.
    """

    def handler() -> None:
        now = clock()
        connection = connection_factory()
        try:
            trade_date = now.astimezone(KST).date().isoformat()
            alert_only = now.astimezone(KST).time() >= alert_only_from
            flags = load_risk_flags(connection, protective_exit_enabled=protective_enabled)
            rows = connection.execute(
                "SELECT ticker, quantity, peak_price_adj FROM positions "
                "WHERE tracking_status = 'MANAGED' AND quantity > 0").fetchall()
            for row in rows:
                ticker, quantity, peak = row["ticker"], row["quantity"], row["peak_price_adj"]
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
                    SellRequest(ticker, quantity, kind, stop_cycle_id(ticker, trade_date), "TRAILING_STOP"),
                    now=now, account_fresh=True, protective_enabled=protective_enabled)
                if result.startswith("BLOCKED") or result == "ANOMALY":
                    # 못 낸 매도는 사라지지 않게 의무로 남긴다(익일 09:10 재시도).
                    open_exit_obligation(connection, ticker=ticker, quantity=quantity,
                                         reason=f"STOP_{result}", source_event_id=event_id, now=now)
                if result != "DUPLICATE":
                    msg = f"{ticker} 트레일링 스탑 → {result}"
                    _log(connection, "intraday_guard", "STOP_TRIGGERED", "CRITICAL", msg, now)
                    _safe_notify(notify, "CRITICAL", msg)
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
                    now=now, account_fresh=True, protective_enabled=protective_enabled)
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


def _update_peaks(connection: sqlite3.Connection, now: datetime) -> None:
    """`PeakPrice` 일 1회 갱신: 보유 종목의 최신 수정 종가로 단조 증가시킨다(6.1절)."""
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
            if verdict == "CONFIRMED":
                _safe_notify(notify, "CRITICAL", "계좌 불일치 확정 → SAFE_HALT(ACCOUNT_MISMATCH)")
            if not quiescent:
                return  # 진행 중인 주문이 있으면 예수금 대사·스냅샷도 다음 주기로 미룬다.
            previous = connection.execute(
                "SELECT value FROM system_state WHERE \"key\" = 'last_d2_cash'").fetchone()
            advance_baseline = True
            if previous is not None:
                trade_cash = connection.execute(
                    "SELECT COALESCE(SUM(CASE side WHEN 'SELL' THEN qty*price ELSE -qty*price END - fee - tax),0) "
                    "FROM trade_fills WHERE substr(executed_at,1,10) = ?", (trade_day.isoformat(),)).fetchone()[0]
                delta = unexplained_cash_delta(int(previous[0]), int(round(trade_cash)), current_cash,
                                               tolerance_krw=cash_tolerance_krw)
                first_row = connection.execute("SELECT MIN(snapshot_date) FROM daily_portfolio").fetchone()[0]
                first = first_run_date or (date.fromisoformat(first_row) if first_row else trade_day)
                decision = record_discrepancy(connection, delta, trade_date=trade_day, now=now,
                                              confirm_interval_seconds=confirm_interval_seconds,
                                              tolerance_krw=cash_tolerance_krw,
                                              learning_period=in_learning_period(first, trade_day))
                # 확정 전(첫 관측·대기)에는 기준 예수금을 옮기지 않는다: 옮기면 다음 대사가 차이를 0 으로 보고
                # 방금 관측한 변동을 스스로 무효화(DISMISSED)해 버린다.
                advance_baseline = decision not in (DiscrepancyDecision.PROVISIONAL, DiscrepancyDecision.WAIT)
            if advance_baseline:
                with connection:
                    connection.execute(
                        "INSERT INTO system_state (\"key\", value, updated_at) VALUES ('last_d2_cash', ?, ?) "
                        "ON CONFLICT(\"key\") DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                        (str(current_cash), now.isoformat()))
            _update_peaks(connection, now)
            try:
                save_daily_snapshot(connection, trade_date=trade_day.isoformat(), total_equity=equity,
                                    cash=current_cash, now=now)
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
) -> Callable[[], None]:
    """10분마다: 호스트 상태 파일로 UPS·시각 플래그를 갱신하고, 24시간 이상 풀리지 않은 HALT 를 재경보합니다."""

    def handler() -> None:
        now = clock()
        connection = connection_factory()
        try:
            if run_dir:
                sync_host_state_flags(connection, run_dir, now=now)
            until = connection.execute("SELECT value FROM system_state WHERE \"key\" = 'training_until'").fetchone()
            if until and until[0] and now.astimezone(KST).date().isoformat() > until[0]:
                with connection:  # 훈련 종료일 도래: 자동 해제
                    connection.execute("UPDATE system_state SET value = CASE \"key\" WHEN 'training_mode' THEN 'false' "
                                       "ELSE '' END, updated_at = ? WHERE \"key\" IN ('training_mode','training_until')",
                                       (now.isoformat(),))
                _safe_notify(notify, "INFO", "훈련 모드 종료일이 지나 자동 해제되었습니다.")
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
            if result["filled"]:
                _safe_notify(notify, "INFO", f"체결 {result['filled']}건 반영")
        finally:
            connection.close()

    return handler
