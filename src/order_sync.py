"""증권사 주문·체결 상태를 DB 의 주문·체결·보유 수량에 반영합니다(명세 4.4·7.3절, `pending_confirmation_poller`).

이 모듈이 없으면 전송한 주문이 영원히 `ACCEPTED` 로 남고 체결이 `positions` 에 반영되지 않습니다.
원칙: ① 브로커가 확인해 준 것만 반영한다(누적 체결 증분 기록, 감소는 `ACCOUNT_MISMATCH`) ② 접수 여부가 불확실한
주문은 장 마감 후 완전 조회로만 "미접수 확정"하고 그 전에는 절대 재전송하지 않는다
③ 장중에는 `PENDING_CONFIRMATION` 을 해소하지 않는다(체결통보 WebSocket 연동 전까지 가장 보수적).
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from src.order_manager import (
    OrderSide, OrderStatus, PendingConfirmationEvidence, cancel_can_finalize, persist_order_transition,
    record_fill_from_cumulative, resolve_pending_confirmation, transition_order_status,
)
from src.reconciliation import OrderSnapshot
from src.safety_flags import clear_flag, raise_flag

KST = timezone(timedelta(hours=9), name="KST")
_ACTIVE = ("ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING", "PENDING_CONFIRMATION")


class SyncBroker(Protocol):
    """동기화에 필요한 조회 계약."""

    def orders(self, trade_date: date) -> list[OrderSnapshot]:
        """지정 거래일의 주문 내역."""
    def fill_prices(self, trade_date: date) -> dict[str, float]:
        """주문번호 → 평균 체결가."""


def apply_fill_to_position(
    connection: sqlite3.Connection, *, ticker: str, side: str, qty: int, price: float, now: datetime
) -> None:
    """체결 1건(증분)을 `positions` 에 반영한다. 매수는 평균단가·고점 초기화, 매도는 수량 차감.

    트랜잭션은 호출자 책임이다: `record_fill_from_cumulative(on_recorded=...)` 가 체결 기록과 같은 커밋 안에서 부른다.
    """
    row = connection.execute("SELECT quantity, avg_buy_price, peak_price_adj, tracking_status FROM positions "
                             "WHERE ticker = ?", (ticker,)).fetchone()
    factor_row = connection.execute(
        "SELECT adjusted_close * 1.0 / raw_close FROM daily_candles WHERE ticker = ? "
        "ORDER BY trade_date DESC LIMIT 1", (ticker,)).fetchone()
    factor = factor_row[0] if factor_row and factor_row[0] else 1.0
    stamp = now.isoformat()
    if side == "BUY":
        old_qty, old_avg = (row["quantity"], row["avg_buy_price"]) if row else (0, 0.0)
        new_qty = old_qty + qty
        avg = (old_qty * old_avg + qty * price) / new_qty
        peak_now = price * factor
        peak = max(row["peak_price_adj"] or 0.0, peak_now) if row else peak_now
        managed = connection.execute("SELECT 1 FROM universe_assets WHERE ticker = ?", (ticker,)).fetchone()
        connection.execute(
            """
            INSERT INTO positions (ticker, tracking_status, quantity, avg_buy_price, market_value,
                                   peak_price_adj, last_synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET quantity = excluded.quantity, avg_buy_price = excluded.avg_buy_price,
                market_value = excluded.market_value, peak_price_adj = excluded.peak_price_adj,
                last_synced_at = excluded.last_synced_at
            """,
            (ticker, "MANAGED" if managed else "UNMANAGED", new_qty, avg, int(new_qty * price),
             peak if managed else None, stamp))
    elif row is not None:
        new_qty = max(0, row["quantity"] - qty)
        connection.execute("UPDATE positions SET quantity = ?, market_value = ?, last_synced_at = ? "
                           "WHERE ticker = ?", (new_qty, int(new_qty * price), stamp, ticker))


def _cancel_matches(connection: sqlite3.Connection, order_id: str, snap: OrderSnapshot, now: datetime) -> int:
    """취소 확인 조회에서 (누적 체결, 잔량)이 직전 조회와 같으면 연속 횟수를 1 늘리고, 다르면 1 로 되돌린다."""
    key, seen = f"cancel_match:{order_id}", f"{snap.filled_qty},{snap.remaining_qty}"
    row = connection.execute('SELECT value FROM system_state WHERE "key" = ?', (key,)).fetchone()
    last, _, count = (row[0] if row else "").rpartition(",")
    matches = int(count) + 1 if row and last == seen else 1
    with connection:
        connection.execute(
            'INSERT INTO system_state ("key", value, updated_at) VALUES (?, ?, ?) '
            'ON CONFLICT("key") DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at',
            (key, f"{seen},{matches}", now.isoformat()))
    return matches


def _close_intent(connection: sqlite3.Connection, order_id: str, status: str, now: datetime) -> None:
    """주문의 부모 의도 상태를 갱신한다."""
    with connection:
        connection.execute(
            "UPDATE order_intents SET status = ?, updated_at = ? WHERE parent_intent_id = "
            "(SELECT parent_intent_id FROM orders WHERE order_id = ?)", (status, now.isoformat(), order_id))


def _move(connection: sqlite3.Connection, order: sqlite3.Row, target: OrderStatus, now: datetime, reason: str) -> None:
    """허용된 전이만 수행한다(같은 상태·불가 전이는 건너뜀)."""
    current = OrderStatus(connection.execute("SELECT status FROM orders WHERE order_id = ?",
                                             (order["order_id"],)).fetchone()[0])
    if current is target:
        return
    try:
        transition_order_status(current, target)
    except ValueError:
        return
    persist_order_transition(connection, order_id=order["order_id"], new_status=target, actor="order_sync",
                             reason_code=reason, correlation_id=order["attempt_id"], occurred_at=now)
    if target is OrderStatus.FILLED:
        _close_intent(connection, order["order_id"], "COMPLETED", now)
    elif target in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.CONFIRMED_NOT_ACCEPTED,
                    OrderStatus.EXPIRED):
        _close_intent(connection, order["order_id"], "CLOSED", now)


def sync_orders(connection: sqlite3.Connection, broker: SyncBroker, *, now: datetime, market_closed: bool) -> dict:
    """활성 주문을 브로커 상태와 맞춘다. 반환: {'filled': n, 'pending': 남은 확인대기 수}."""
    from src.order_executor import open_exit_obligation  # 지연 import(순환 방지)

    orders = connection.execute(
        "SELECT o.*, i.intent_type FROM orders o JOIN order_intents i USING (parent_intent_id) "
        f"WHERE o.status IN ({','.join('?' * len(_ACTIVE))}) ORDER BY o.submitted_at", _ACTIVE).fetchall()
    by_date: dict[date, tuple[dict, dict]] = {}
    filled_count = 0
    for o in orders:
        day = datetime.fromisoformat(o["submitted_at"]).astimezone(KST).date()
        if day not in by_date:
            by_date[day] = ({s.order_key: s for s in broker.orders(day)}, broker.fill_prices(day))
        snaps, prices = by_date[day]
        snap = snaps.get(o["kis_order_no"]) if o["kis_order_no"] else None
        if snap is None and o["status"] == "PENDING_CONFIRMATION" and not o["kis_order_no"]:
            known = {r[0] for r in connection.execute("SELECT kis_order_no FROM orders WHERE kis_order_no IS NOT NULL")}
            matches = [s for s in snaps.values() if s.order_key not in known and s.ticker == o["ticker"]
                       and s.side == o["side"] and s.requested_qty == o["requested_qty"]]
            if len(matches) == 1:  # 유일하게 일치할 때만 같은 주문으로 본다(모호하면 확정하지 않음)
                snap = matches[0]
                with connection:
                    connection.execute("UPDATE orders SET kis_order_no = ? WHERE order_id = ?",
                                       (snap.order_key, o["order_id"]))
        if snap is None:
            if o["status"] == "PENDING_CONFIRMATION":
                resolved = resolve_pending_confirmation(PendingConfirmationEvidence(
                    authoritative_acceptance_query_complete=True, authoritative_order_not_found=True,
                    execution_query_complete=True, execution_not_found=True, market_closed=market_closed,
                    side=OrderSide(o["side"])))
                if resolved is OrderStatus.CONFIRMED_NOT_ACCEPTED:
                    _move(connection, o, resolved, now, "CONFIRMED_NOT_ACCEPTED_AFTER_CLOSE")
                    if o["side"] == "SELL" and o["intent_type"] in ("RISK_REDUCTION", "EXIT_OBLIGATION_RETRY"):
                        open_exit_obligation(connection, ticker=o["ticker"], quantity=o["requested_qty"],
                                             reason="NOT_ACCEPTED", source_event_id=f"N{o['order_id']}", now=now)
            continue
        if snap.filled_qty > o["filled_qty"]:
            try:
                recorded = record_fill_from_cumulative(
                    connection, order_id=o["order_id"], new_cumulative_qty=snap.filled_qty,
                    new_average_price=prices.get(snap.order_key) or float(o["order_price"]), executed_at=now,
                    # 보유 수량 반영은 체결 기록과 같은 커밋 안에서 한다(사이에 정전이 나도 둘이 함께 남거나 함께 사라짐).
                    on_recorded=lambda c, qty, price, o=o: apply_fill_to_position(
                        c, ticker=o["ticker"], side=o["side"], qty=qty, price=price, now=now))
            except ValueError:
                raise_flag(connection, "HALT", "ACCOUNT_MISMATCH", now=now, detail="FILL_RECORD_INVALID")
                continue
            if recorded:
                if o["side"] == "SELL" and o["intent_type"] == "RISK_REDUCTION":
                    with connection:  # 손절 체결: 다음 주간 교체까지 재진입 차단(5.2절 6번)
                        connection.execute(
                            "UPDATE universe_assets SET blocked_until_next_rotation = 1, blocked_since = ? "
                            "WHERE ticker = ?", (now.astimezone(KST).date().isoformat(), o["ticker"]))
                filled_count += 1
        if snap.remaining_qty == 0 and snap.filled_qty >= o["requested_qty"]:
            _move(connection, o, OrderStatus.FILLED, now, "BROKER_FILLED")
        elif snap.status == "CANCELED" and (market_closed or o["status"] == "CANCEL_PENDING"):
            if o["status"] == "ACCEPTED":
                _move(connection, o, OrderStatus.CANCEL_PENDING, now, "BROKER_CANCELED")
            # 7.3절: 장중에는 잔량·누적 체결이 연속 2회 같은 조회에서만 확정한다(취소 직후 체결 반영 지연 대비).
            if cancel_can_finalize(consecutive_matches=_cancel_matches(connection, o["order_id"], snap, now),
                                   market_closed_full_query=market_closed):
                _move(connection, o, OrderStatus.CANCELED, now, "BROKER_CANCELED_CONFIRMED")
                with connection:
                    connection.execute('DELETE FROM system_state WHERE "key" = ?', (f"cancel_match:{o['order_id']}",))
        elif market_closed and snap.remaining_qty > 0 and o["status"] in ("ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING"):
            # 장 마감 후 완전 조회: 취소·체결 없이 남은 잔량은 당일 만료(EXPIRED)로 확정한다(7.3절).
            _move(connection, o, OrderStatus.EXPIRED, now, "EXPIRED_AFTER_CLOSE")
            if o["side"] == "SELL" and o["intent_type"] in ("RISK_REDUCTION", "EXIT_OBLIGATION_RETRY"):
                open_exit_obligation(connection, ticker=o["ticker"], quantity=snap.remaining_qty,
                                     reason="EXPIRED_UNFILLED", source_event_id=f"E{o['order_id']}", now=now)
        elif snap.filled_qty > 0:
            _move(connection, o, OrderStatus.PARTIALLY_FILLED, now, "BROKER_PARTIAL")
        else:
            _move(connection, o, OrderStatus.ACCEPTED, now, "BROKER_ACCEPTED")
    pending = connection.execute("SELECT COUNT(*) FROM orders WHERE status = 'PENDING_CONFIRMATION'").fetchone()[0]
    if pending == 0:
        clear_flag(connection, "HALT", "PENDING_CONFIRMATION", now=now, cleared_by="SYSTEM")
    return {"filled": filled_count, "pending": pending}
