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


class ClassifyRestatesUnitsTests(AuditFixTestCase):
    """감사 2번: 입출금을 분류하면 단위 수가 함께 조정돼 가짜 낙폭·가짜 수익이 생기지 않는다."""

    def snapshot(self, day, equity):
        """KST 15:40 마감 스냅샷을 저장하고 낙폭 단계를 돌려준다."""
        from src.job_handlers import save_daily_snapshot

        when = datetime(2026, 11, day, 15, 40, tzinfo=KST)
        return save_daily_snapshot(self.conn, trade_date=f"2026-11-{day}", total_equity=equity, cash=0, now=when)

    def flow(self, flow_date, amount):
        """미분류 현금흐름 한 건을 만들고 승인된 `/classify` 확인 이력을 준비한다."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO cash_flows (broker_transaction_id, flow_date, occurred_at, amount, flow_type,"
                " classification_status, source, observed_at, first_observed_at) VALUES ('RECON:1', ?, 't', ?,"
                " 'UNCLASSIFIED', 'UNCLASSIFIED_CASH_FLOW', 'CASH_RECON', 't', 't')", (flow_date, amount))
            self.conn.execute(
                "INSERT INTO pending_actions (id, action_type, code_hash, reason, approval_status, created_at,"
                " expires_at) VALUES ('act1', 'CLASSIFY_FLOW', 'h', 'r', 'APPROVED', 't', 't')")

    def classify(self, flow_type):
        from src.operator_actions import classify_cash_flow

        classify_cash_flow(self.conn, flow_id=1, flow_type=flow_type, action_id="act1",
                           now=datetime(2026, 11, 25, 9, 0, tzinfo=KST))

    def nav_and_units(self, day):
        row = self.conn.execute("SELECT flow_adjusted_unit_nav, units_outstanding FROM daily_portfolio "
                                "WHERE snapshot_date = ?", (f"2026-11-{day}",)).fetchone()
        return round(row[0], 6), round(row[1], 6)

    def test_withdrawal_after_snapshot_keeps_unit_price(self):
        self.snapshot(23, 4_000_000)
        self.flow("2026-11-24", -1_000_000)
        self.assertEqual(self.snapshot(24, 3_000_000), "NORMAL")  # 미분류 구간: 평가액에서 보정
        self.classify("WITHDRAWAL")
        self.assertEqual(self.nav_and_units(24), (100.0, 30_000.0))
        self.assertEqual(self.snapshot(25, 3_000_000), "NORMAL")  # 예전 코드: 단위가격 75 → CRITICAL
        self.assertEqual(self.nav_and_units(25), (100.0, 30_000.0))
        recorded = self.conn.execute("SELECT units_delta, unit_price_applied FROM cash_flows").fetchone()
        self.assertEqual((round(recorded[0], 6), round(recorded[1], 6)), (-10_000.0, 100.0))

    def test_deposit_does_not_become_fake_profit(self):
        self.snapshot(23, 4_000_000)
        self.flow("2026-11-24", 1_000_000)
        self.snapshot(24, 5_000_000)
        self.classify("DEPOSIT")
        self.snapshot(25, 5_000_000)
        self.assertEqual(self.nav_and_units(25), (100.0, 50_000.0))  # 예전 코드: 125 와 고점 오염

    def test_classifying_before_the_closing_snapshot_adjusts_the_latest_units(self):
        self.snapshot(23, 4_000_000)
        self.snapshot(24, 4_000_000)
        self.flow("2026-11-25", -1_000_000)  # 오늘 흐름: 마감 스냅샷이 아직 없다
        self.classify("WITHDRAWAL")
        self.assertEqual(self.snapshot(25, 3_000_000), "NORMAL")
        self.assertEqual(self.nav_and_units(25), (100.0, 30_000.0))


class HostJobAlertTests(AuditFixTestCase):
    """감사 3·6번: 토큰 갱신 실패와 오프사이트 백업 이상은 앱이 읽어 하루 한 번 알린다."""

    def setUp(self):
        super().setUp()
        import tempfile
        from pathlib import Path

        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.run_dir, ignore_errors=True))

    def write(self, name, **payload):
        import json

        payload.setdefault("updated_at", MORNING.isoformat())
        (self.run_dir / name).write_text(json.dumps(payload), encoding="utf-8")

    def run_check(self, now=MORNING):
        from src.job_handlers import check_host_job_alerts

        check_host_job_alerts(self.conn, str(self.run_dir), now, self.notify)

    def test_token_refresh_alert_is_sent_once_per_day(self):
        self.write("token_refresh_status.json", consecutive_failures=3, alert=True)
        self.write("offsite_status.json", ok=True, detail="COPIED_0")
        self.run_check()
        self.run_check(MORNING + timedelta(hours=1))
        self.assertEqual([m for _, m in self.notes], ["KIS 토큰 갱신 이상: 연속 3회 실패"])
        self.run_check(MORNING + timedelta(days=1))  # 다음 날에는 다시 알린다
        self.assertEqual(len(self.notes), 2)

    def test_missing_token_status_is_quiet_but_missing_offsite_status_is_not(self):
        self.run_check()
        self.assertEqual(len(self.notes), 1)
        self.assertIn("오프사이트 백업", self.notes[0][1])

    def test_failed_or_stale_offsite_backup_is_reported(self):
        self.write("offsite_status.json", ok=False, detail="TARGET_UNAVAILABLE")
        self.run_check()
        self.assertIn("실패(TARGET_UNAVAILABLE)", self.notes[0][1])
        self.write("offsite_status.json", ok=True, detail="COPIED_1", updated_at=(MORNING - timedelta(days=4)).isoformat())
        self.run_check(MORNING + timedelta(days=1))
        self.assertIn("72시간 넘게", self.notes[1][1])

    def test_healthy_statuses_stay_silent(self):
        self.write("token_refresh_status.json", consecutive_failures=0, alert=False)
        self.write("offsite_status.json", ok=True, detail="COPIED_2")
        self.run_check()
        self.assertEqual(self.notes, [])


class DeferredNoticeTests(AuditFixTestCase):
    """감사 7번: 손절 알림은 모든 종목의 주문을 낸 뒤에 보낸다(느린 텔레그램이 다음 종목 손절을 늦추지 않게)."""

    def test_notifications_wait_until_every_stop_order_is_sent(self):
        from src.job_handlers import create_intraday_guard_handler

        with self.conn:
            self.conn.execute(
                "INSERT INTO universe_assets (ticker, name, category, region, underlying_index, product_type,"
                " replication_method, status, data_status, added_at, asset_class) VALUES ('229200','x',"
                "'RISK_ASSET','KR','K2','SPOT_ETF','PHYSICAL','ACTIVE','VALID','t','EQUITY')")
            self.conn.execute(
                "INSERT INTO positions (ticker, tracking_status, quantity, avg_buy_price, market_value,"
                " peak_price_adj, last_synced_at) VALUES ('229200','MANAGED',10,100,1000,100,'t')")
            self.conn.execute(
                "INSERT INTO daily_candles (ticker, trade_date, raw_close, adjusted_close, volume, source,"
                " source_timestamp) VALUES ('229200','2026-11-24',100,100,1000,'KIS','t')")
        broker = FakeBroker(price=85.0, positions=[Position("069500", 10), Position("229200", 10)])  # 두 종목 모두 고점 100 대비 -15%: 손절선(-10%) 도달
        orders_sent_when_notified = []
        handler = create_intraday_guard_handler(
            self.factory, broker, clock=lambda: MORNING,
            notify=lambda severity, message: orders_sent_when_notified.append(len(broker.transport.calls)))
        handler()
        self.assertEqual(len(broker.transport.calls), 2)
        self.assertEqual(orders_sent_when_notified, [2, 2])  # 첫 알림도 두 주문이 모두 나간 뒤에 보낸다
