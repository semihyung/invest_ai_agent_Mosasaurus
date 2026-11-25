"""저장소 감사(2026-11-25) 수정의 회귀 테스트: 부팅 대조·주문 대조·예수금 기준·쓰기 락·체결 원자성·취소 확정."""

import sqlite3
from datetime import datetime, timedelta

from src.job_handlers import compare_account, reconcile_cash
from src.order_manager import (
    OrderIntent, OrderIntentType, OrderSide, OrderStatus, persist_order_intent, persist_order_transition,
    record_fill_from_cumulative, record_order_attempt,
)
from src.order_sync import sync_orders
from src.reconciliation import OrderSnapshot, Position, reconcile_account_state
from tests.integration.test_job_handlers import CLOSE, KST, MORNING, FakeBroker, HandlerTestCase


class AuditFixTestCase(HandlerTestCase):
    """주문 한 건을 실제 기록 경로(의도 → 시도 → 접수)로 만드는 도우미를 더한다."""

    def make_order(self, *, status=OrderStatus.ACCEPTED, side=OrderSide.BUY, qty=10, when=MORNING, kis_no="0000001"):
        """접수된 주문 1건을 만들고 order_id 를 돌려준다."""
        result = persist_order_intent(
            self.conn, OrderIntent(f"D{when.date()}", OrderIntentType.PARKING_ALLOCATION, None, "069500", side, qty),
            policy_payload={}, created_at=when)
        order_id = "O-" + kis_no
        record_order_attempt(self.conn, order_id=order_id, parent_intent_id=result.parent_intent_id,
                             attempt_id="A-" + kis_no, order_price=8500, strategy_reason="TEST",
                             submitted_at=when, request_payload_hash="h")
        persist_order_transition(self.conn, order_id=order_id, new_status=OrderStatus.ACCEPTED, actor="t",
                                 reason_code="t", correlation_id="t", occurred_at=when)
        with self.conn:
            self.conn.execute("UPDATE orders SET kis_order_no = ? WHERE order_id = ?", (kis_no, order_id))
        if status is OrderStatus.CANCEL_PENDING:
            persist_order_transition(self.conn, order_id=order_id, new_status=status, actor="t",
                                     reason_code="t", correlation_id="t", occurred_at=when)
        return order_id


class BootReconciliationTests(AuditFixTestCase):
    """감사 1번: 부팅 대조는 오늘(KST) 주문끼리만 비교한다."""

    def test_yesterdays_orders_do_not_fail_todays_boot(self):
        yesterday = MORNING - timedelta(days=1)
        self.make_order(when=yesterday)
        # 증권사는 오늘 주문만 돌려준다(어제 주문은 목록에 없음). 보유 수량은 DB 와 같다.
        result = compare_account(self.conn, [Position("069500", 10)], [], MORNING.date())
        self.assertTrue(result.reconciliation_ok, result)


class OrderComparisonTests(AuditFixTestCase):
    """감사 2번: DB 내부 종결 상태(EXPIRED 등)와 KIS 정규화 상태의 이름 차이는 불일치가 아니다."""

    def test_expired_in_db_matches_unfilled_accepted_at_broker(self):
        result = reconcile_account_state(
            broker_positions=[], sqlite_positions=[], account_data_fresh=True,
            broker_orders=[OrderSnapshot("1", "069500", "BUY", 10, 0, 10, "ACCEPTED")],
            sqlite_orders=[OrderSnapshot("1", "069500", "BUY", 10, 0, 10, "EXPIRED")])
        self.assertTrue(result.reconciliation_ok)

    def test_different_filled_quantity_is_still_a_mismatch(self):
        result = reconcile_account_state(
            broker_positions=[], sqlite_positions=[], account_data_fresh=True,
            broker_orders=[OrderSnapshot("1", "069500", "BUY", 10, 4, 6, "PARTIALLY_FILLED")],
            sqlite_orders=[OrderSnapshot("1", "069500", "BUY", 10, 0, 10, "ACCEPTED")])
        self.assertEqual(result.order_mismatches, ("1",))


class CashBaselineTests(AuditFixTestCase):
    """감사 3번: 기준 예수금을 못 옮긴 날의 체결도 다음 대사에 포함돼, 같은 입금이 미분류로 확정된다."""

    def test_deposit_first_seen_at_close_confirms_next_day(self):
        day_before_close = CLOSE - timedelta(days=1)
        with self.conn:
            self.conn.execute(
                "INSERT INTO system_state (\"key\", value, updated_at) VALUES ('last_d2_cash', '1000000', ?)",
                (day_before_close.isoformat(),))
        order_id = self.make_order()
        # 당일 15:05 매수 체결 10주 × 1,000원 = 현금 -10,000
        record_fill_from_cumulative(self.conn, order_id=order_id, new_cumulative_qty=10, new_average_price=1000.0,
                                    executed_at=datetime(2026, 11, 25, 15, 5, tzinfo=KST))
        with_deposit = 1_000_000 - 10_000 + 500_000
        first = reconcile_cash(self.conn, current_cash=with_deposit, now=CLOSE, trade_day=CLOSE.date(),
                               advance_baseline=True)
        self.assertEqual(first.value, "PROVISIONAL")
        next_day = datetime(2026, 11, 26, 15, 0, tzinfo=KST)
        second = reconcile_cash(self.conn, current_cash=with_deposit, now=next_day, trade_day=next_day.date(),
                                advance_baseline=False)
        self.assertEqual(second.value, "UNCLASSIFIED")  # 예전 코드는 전날 체결이 빠져 WAIT 에 영구 고착


class WriteLockTests(AuditFixTestCase):
    """감사 7번: 의도 저장은 곧바로 커밋된다(쓰기 락 밖에 열린 트랜잭션이 남지 않음)."""

    def test_new_intent_is_committed_immediately(self):
        persist_order_intent(
            self.conn, OrderIntent("D2026-11-25", OrderIntentType.PARKING_ALLOCATION, None, "069500",
                                   OrderSide.BUY, 1), policy_payload={}, created_at=MORNING)
        self.assertFalse(self.conn.in_transaction)
        other = self.factory()
        try:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0], 1)
        finally:
            other.close()


class FillAtomicityTests(AuditFixTestCase):
    """감사 8번: 보유 수량 반영이 실패하면 체결 기록도 함께 취소된다(둘 중 하나만 남지 않음)."""

    def test_position_failure_rolls_back_the_fill(self):
        order_id = self.make_order()

        def crash(_connection, _qty, _price):
            raise sqlite3.OperationalError("power cut")

        with self.assertRaises(sqlite3.OperationalError):
            record_fill_from_cumulative(self.conn, order_id=order_id, new_cumulative_qty=4,
                                        new_average_price=8500.0, executed_at=MORNING, on_recorded=crash)
        self.assertEqual(self.count("SELECT COUNT(*) FROM trade_fills"), 0)
        self.assertEqual(self.count("SELECT filled_qty FROM orders WHERE order_id = ?", order_id), 0)

    def test_sync_updates_fill_and_position_together(self):
        order_id = self.make_order()
        broker = FakeBroker(orders=[OrderSnapshot("0000001", "069500", "BUY", 10, 4, 6, "PARTIALLY_FILLED")])
        sync_orders(self.conn, broker, now=MORNING, market_closed=False)
        self.assertEqual(self.count("SELECT SUM(qty) FROM trade_fills WHERE order_id = ?", order_id), 4)
        self.assertEqual(self.count("SELECT quantity FROM positions WHERE ticker = '069500'"), 14)


class CancelFinalizationTests(AuditFixTestCase):
    """감사 9번(명세 7.3절): 장중 취소 확정은 잔량·체결이 연속 2회 같은 조회에서만 한다."""

    def status(self, order_id):
        return self.conn.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,)).fetchone()[0]

    def test_intraday_cancel_needs_two_matching_reads(self):
        order_id = self.make_order(status=OrderStatus.CANCEL_PENDING)
        broker = FakeBroker(orders=[OrderSnapshot("0000001", "069500", "BUY", 10, 0, 0, "CANCELED")])
        sync_orders(self.conn, broker, now=MORNING, market_closed=False)
        self.assertEqual(self.status(order_id), "CANCEL_PENDING")
        sync_orders(self.conn, broker, now=MORNING + timedelta(seconds=15), market_closed=False)
        self.assertEqual(self.status(order_id), "CANCELED")

    def test_after_close_full_query_finalizes_at_once(self):
        order_id = self.make_order(status=OrderStatus.CANCEL_PENDING)
        broker = FakeBroker(orders=[OrderSnapshot("0000001", "069500", "BUY", 10, 0, 0, "CANCELED")])
        sync_orders(self.conn, broker, now=CLOSE, market_closed=True)
        self.assertEqual(self.status(order_id), "CANCELED")
