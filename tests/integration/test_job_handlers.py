"""job_handlers·order_executor 통합 테스트: 가짜 증권사로 스탑·검문소·취소·마감 후 대조를 검증합니다."""

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.db import initialize_database
from src.job_handlers import (
    create_intraday_guard_handler, create_open_checkpoint_handler, create_order_cancel_sweep_handler,
    create_pending_confirmation_poller, create_post_close_reconcile_handler, create_safety_maintenance_handler,
)
from src.order_manager import Quote
from src.reconciliation import OrderSnapshot, Position
from src.safety_flags import active_codes, raise_flag

KST = timezone(timedelta(hours=9))
MORNING = datetime(2026, 11, 25, 10, 0, tzinfo=KST)   # 수요일 10:00
AFTER_CUTOFF = datetime(2026, 11, 25, 15, 12, tzinfo=KST)
CLOSE = datetime(2026, 11, 25, 15, 40, tzinfo=KST)


class FakeTransport:
    """전송 횟수와 인자를 기록하는 가짜 KIS 주문 어댑터."""

    def __init__(self, response=None):
        self.calls = []
        self.response = response or {"accepted": True, "order_number": "0000001"}

    def submit_limit_order(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeBroker:
    """조회·주문 계약을 메모리로 구현한 가짜 증권사."""

    environment = "PAPER"

    def __init__(self, price=85.0, bid=85, positions=None, orders=None, cash=1_000_000, equity=2_000_000):
        self.transport = FakeTransport()
        self.price, self.bid = price, bid
        self._positions = positions if positions is not None else [Position("069500", 10)]
        self._orders = orders or []
        self.cash, self.equity = cash, equity
        self.cancelled, self.fail = [], False

    def quote(self, ticker):
        return Quote(bid=self.bid, ask=self.bid + 1, nav=None)

    def sellable_qty(self, ticker):
        return 10

    def last_price(self, ticker):
        if self.fail:
            raise TimeoutError("down")
        return self.price

    def positions(self):
        if self.fail:
            raise TimeoutError("down")
        return list(self._positions)

    def orders(self, trade_date):
        return list(self._orders)

    def cash_d2(self):
        return self.cash

    def total_equity(self):
        return self.equity

    def fill_prices(self, trade_date):
        return dict(getattr(self, "prices", {}))

    def cancel_order(self, kis_order_no, ticker):
        self.cancelled.append(kis_order_no)
        return True


class HandlerTestCase(unittest.TestCase):
    """공통 준비: DB·종목·보유·일봉·정상 대조 상태."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        self.conn = initialize_database(self.path)
        for ticker, cat in (("069500", "RISK_ASSET"), ("153130", "SAFE_PARKING")):
            self.conn.execute(
                "INSERT INTO universe_assets (ticker, name, category, region, underlying_index, product_type,"
                " replication_method, status, data_status, added_at, asset_class) VALUES (?,?,?,'KR','K',"
                "'SPOT_ETF','PHYSICAL','ACTIVE','VALID','t',?)",
                (ticker, ticker, cat, "EQUITY" if cat == "RISK_ASSET" else "MONEY_MARKET"))
        self.conn.execute(
            "INSERT INTO positions (ticker, tracking_status, quantity, avg_buy_price, market_value, peak_price_adj,"
            " last_synced_at) VALUES ('069500','MANAGED',10,100,1000,100,'t')")
        self.conn.execute(
            "INSERT INTO daily_candles (ticker, trade_date, raw_close, adjusted_close, volume, source,"
            " source_timestamp) VALUES ('069500','2026-11-24',100,100,1000,'KIS','t')")
        self.conn.execute(
            "INSERT INTO system_state (\"key\", value, updated_at) VALUES ('reconciliation_ok','true','t')")
        self.conn.commit()
        self.notes = []

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def factory(self):
        from src.db import connect_database
        return connect_database(self.path)

    def notify(self, severity, message):
        self.notes.append((severity, message))

    def count(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()[0]


class IntradayGuardTests(HandlerTestCase):
    """스탑 감시: 1회 전송·멱등·SAFE_HALT 원인별 허용·15:10 이후 경보 전용."""

    def run_guard(self, broker, at=MORNING):
        create_intraday_guard_handler(self.factory, broker, clock=lambda: at, notify=self.notify)()

    def test_stop_hit_sells_once_at_bid1_and_is_idempotent(self):
        broker = FakeBroker()
        self.run_guard(broker)
        self.run_guard(broker)  # 10초 뒤 다시 돌아도 같은 사유는 1회만
        self.assertEqual(len(broker.transport.calls), 1)
        self.assertEqual(broker.transport.calls[0]["price"], 85)
        row = self.conn.execute("SELECT status, kis_order_no FROM orders").fetchone()
        self.assertEqual((row[0], row[1]), ("ACCEPTED", "0000001"))

    def test_no_stop_no_order(self):
        broker = FakeBroker(price=95.0)
        self.run_guard(broker)
        self.assertEqual(broker.transport.calls, [])

    def test_safe_halt_allows_protective_exit_only_for_allowed_reasons(self):
        raise_flag(self.conn, "HALT", "ACCOUNT_MISMATCH", now=MORNING)
        broker = FakeBroker()
        self.run_guard(broker)
        self.assertEqual(len(broker.transport.calls), 1)

    def test_safe_halt_with_other_reason_blocks_and_records_obligation(self):
        raise_flag(self.conn, "HALT", "ACCOUNT_MISMATCH", now=MORNING)
        raise_flag(self.conn, "HALT", "CREDENTIALS_UNAVAILABLE", now=MORNING)
        broker = FakeBroker()
        self.run_guard(broker)
        self.assertEqual(broker.transport.calls, [])
        self.assertEqual(self.count("SELECT COUNT(*) FROM exit_obligations WHERE status='OPEN'"), 1)

    def test_after_cutoff_records_obligation_without_order(self):
        broker = FakeBroker()
        self.run_guard(broker, AFTER_CUTOFF)
        self.assertEqual(broker.transport.calls, [])
        self.assertEqual(self.count("SELECT COUNT(*) FROM exit_obligations WHERE status='OPEN'"), 1)
        self.assertTrue(any(s == "CRITICAL" for s, _ in self.notes))

    def test_open_obligation_skips_new_stop_orders(self):
        self.run_guard(FakeBroker(), AFTER_CUTOFF)
        broker = FakeBroker()
        self.run_guard(broker)
        self.assertEqual(broker.transport.calls, [])

    def test_quote_failure_marks_account_stale_not_mismatch(self):
        broker = FakeBroker()
        broker.fail = True
        self.run_guard(broker)
        self.assertIn("ACCOUNT_DATA_STALE", active_codes(self.conn, "BUY_BLOCK"))
        self.assertEqual(broker.transport.calls, [])

    def test_timeout_leaves_pending_confirmation_without_resend(self):
        broker = FakeBroker()
        broker.transport.response = {}  # accepted/rejected 모두 아님 → 확인 대기
        self.run_guard(broker)
        self.run_guard(broker)
        self.assertEqual(len(broker.transport.calls), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM orders WHERE status='PENDING_CONFIRMATION'"), 1)


class OpenCheckpointTests(HandlerTestCase):
    """09:10 검문소: 의무 재매도·보유 0 이면 VOIDED."""

    def make_obligation(self):
        self.conn.execute(
            "INSERT INTO exit_obligations (obligation_id, ticker, source_event_id, required_qty, status,"
            " reason_code, created_at) VALUES ('OB1','069500','E1',10,'OPEN','STOP','t')")
        self.conn.commit()

    def test_retries_open_obligation_once_per_day(self):
        self.make_obligation()
        broker = FakeBroker()
        handler = create_open_checkpoint_handler(self.factory, broker, clock=lambda: MORNING)
        handler()
        handler()
        self.assertEqual(len(broker.transport.calls), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM order_intents WHERE obligation_id='OB1'"), 1)

    def test_zero_holding_voids_obligation(self):
        self.make_obligation()
        self.conn.execute("UPDATE positions SET quantity = 0")
        self.conn.commit()
        broker = FakeBroker()
        create_open_checkpoint_handler(self.factory, broker, clock=lambda: MORNING)()
        self.assertEqual(broker.transport.calls, [])
        self.assertEqual(self.count("SELECT COUNT(*) FROM exit_obligations WHERE status='VOIDED'"), 1)


class CancelSweepTests(HandlerTestCase):
    """15:14:30: 활성 주문 취소 요청 + 미체결 위험 축소 매도는 의무로."""

    def test_cancel_requested_and_obligation_opened(self):
        broker = FakeBroker()
        create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING)()
        create_order_cancel_sweep_handler(
            self.factory, broker, clock=lambda: datetime(2026, 11, 25, 15, 14, 31, tzinfo=KST))()
        self.assertEqual(broker.cancelled, ["0000001"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM orders WHERE status='CANCEL_PENDING'"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM exit_obligations WHERE status='OPEN'"), 1)

    def test_before_deadline_does_nothing(self):
        broker = FakeBroker()
        create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING)()
        create_order_cancel_sweep_handler(self.factory, broker, clock=lambda: MORNING)()
        self.assertEqual(broker.cancelled, [])


class PostCloseTests(HandlerTestCase):
    """마감 후 대조·예수금 대사·일간 스냅샷."""

    def run_close(self, broker, at=CLOSE):
        create_post_close_reconcile_handler(
            self.factory, broker, clock=lambda: at, notify=self.notify, confirm_interval_seconds=60,
            first_run_date=date(2026, 1, 1))()

    def test_clean_reconcile_saves_snapshot_and_peak(self):
        self.conn.execute("INSERT INTO daily_candles (ticker, trade_date, raw_close, adjusted_close, volume,"
                          " source, source_timestamp) VALUES ('069500','2026-11-25',110,110,1000,'KIS','t')")
        self.conn.commit()
        self.run_close(FakeBroker(price=110.0))
        self.assertEqual(self.count("SELECT COUNT(*) FROM daily_portfolio"), 1)
        self.assertEqual(self.conn.execute("SELECT value FROM system_state WHERE \"key\"='reconciliation_ok'"
                                           ).fetchone()[0], "true")
        self.assertEqual(self.conn.execute("SELECT peak_price_adj FROM positions").fetchone()[0], 110)

    def test_mismatch_needs_two_observations_before_safe_halt(self):
        broker = FakeBroker(positions=[Position("069500", 7)])
        self.run_close(broker)
        self.assertNotIn("ACCOUNT_MISMATCH", active_codes(self.conn, "HALT"))
        self.run_close(broker, CLOSE + timedelta(seconds=61))
        self.assertIn("ACCOUNT_MISMATCH", active_codes(self.conn, "HALT"))

    def test_fetch_failure_is_stale_not_mismatch(self):
        broker = FakeBroker()
        broker.fail = True
        self.run_close(broker)
        self.assertIn("ACCOUNT_DATA_STALE", active_codes(self.conn, "BUY_BLOCK"))
        self.assertNotIn("ACCOUNT_MISMATCH", active_codes(self.conn, "HALT"))

    def test_active_order_skips_judgement(self):
        broker = FakeBroker(positions=[Position("069500", 7)])
        create_intraday_guard_handler(self.factory, FakeBroker(), clock=lambda: MORNING)()
        self.run_close(broker)
        self.run_close(broker, CLOSE + timedelta(seconds=61))
        self.assertNotIn("ACCOUNT_MISMATCH", active_codes(self.conn, "HALT"))
        self.assertEqual(self.count("SELECT COUNT(*) FROM daily_portfolio"), 0)

    def test_unexplained_cash_change_is_quarantined_after_two_observations(self):
        self.run_close(FakeBroker(cash=1_000_000))                       # 기준 예수금 기록
        day2 = CLOSE + timedelta(days=1)
        self.conn.execute("INSERT INTO daily_candles (ticker, trade_date, raw_close, adjusted_close, volume,"
                          " source, source_timestamp) VALUES ('069500','2026-11-26',100,100,1,'KIS','t')")
        self.conn.commit()
        self.run_close(FakeBroker(cash=3_000_000), day2)                 # 첫 관측: 대기
        self.assertEqual(self.count("SELECT COUNT(*) FROM cash_flows WHERE classification_status='PROVISIONAL'"), 1)
        self.run_close(FakeBroker(cash=3_000_000), day2 + timedelta(seconds=61))
        self.assertIn("UNCLASSIFIED_CASH_FLOW", active_codes(self.conn, "BUY_BLOCK"))


class OrderSyncTests(HandlerTestCase):
    """체결 반영·미접수 확정 규칙."""

    def stop_order(self, broker=None):
        broker = broker or FakeBroker()
        create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING)()
        return broker

    def poll(self, broker):
        create_pending_confirmation_poller(self.factory, broker, clock=lambda: MORNING + timedelta(seconds=15))()

    def test_full_fill_updates_order_fill_and_position(self):
        broker = self.stop_order()
        broker._orders = [OrderSnapshot("0000001", "069500", "SELL", 10, 10, 0, "FILLED")]
        broker.prices = {"0000001": 85.0}
        self.poll(broker)
        self.assertEqual(self.conn.execute("SELECT status FROM orders").fetchone()[0], "FILLED")
        self.assertEqual(self.conn.execute("SELECT quantity FROM positions").fetchone()[0], 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM trade_fills"), 1)
        self.assertEqual(self.conn.execute("SELECT status FROM order_intents").fetchone()[0], "COMPLETED")

    def test_stop_loss_fill_blocks_reentry_until_next_rotation(self):
        broker = self.stop_order()
        broker._orders = [OrderSnapshot("0000001", "069500", "SELL", 10, 10, 0, "FILLED")]
        broker.prices = {"0000001": 85.0}
        self.poll(broker)
        row = self.conn.execute("SELECT blocked_until_next_rotation, blocked_since FROM universe_assets "
                                "WHERE ticker = '069500'").fetchone()
        self.assertEqual((row[0], row[1]), (1, "2026-11-25"))

    def test_partial_fill_is_incremental_and_idempotent(self):
        broker = self.stop_order()
        broker._orders = [OrderSnapshot("0000001", "069500", "SELL", 10, 4, 6, "PARTIALLY_FILLED")]
        broker.prices = {"0000001": 85.0}
        self.poll(broker)
        self.poll(broker)
        self.assertEqual(self.count("SELECT COUNT(*) FROM trade_fills"), 1)
        self.assertEqual(self.conn.execute("SELECT quantity FROM positions").fetchone()[0], 6)

    def test_timeout_order_stays_pending_during_session_without_safe_halt(self):
        broker = FakeBroker()
        broker.transport.response = {}
        self.stop_order(broker)
        self.assertEqual(active_codes(self.conn, "HALT"), ())  # v3.7.5 D3: 계좌 전체를 멈추지 않는다
        self.poll(broker)
        self.assertEqual(self.count("SELECT COUNT(*) FROM orders WHERE status='PENDING_CONFIRMATION'"), 1)

    def test_pending_order_confirmed_not_accepted_only_after_close_then_obligation(self):
        broker = FakeBroker()
        broker.transport.response = {}
        self.stop_order(broker)
        create_post_close_reconcile_handler(
            self.factory, broker, clock=lambda: CLOSE, first_run_date=date(2026, 1, 1))()
        self.assertEqual(self.conn.execute("SELECT status FROM orders").fetchone()[0], "CONFIRMED_NOT_ACCEPTED")
        self.assertEqual(self.count("SELECT COUNT(*) FROM exit_obligations WHERE status='OPEN'"), 1)
        self.assertNotIn("PENDING_CONFIRMATION", active_codes(self.conn, "HALT"))

    def test_pending_order_adopts_unique_matching_broker_order(self):
        broker = FakeBroker()
        broker.transport.response = {}
        self.stop_order(broker)
        broker._orders = [OrderSnapshot("0000009", "069500", "SELL", 10, 0, 10, "ACCEPTED")]
        self.poll(broker)
        row = self.conn.execute("SELECT status, kis_order_no FROM orders").fetchone()
        self.assertEqual((row[0], row[1]), ("ACCEPTED", "0000009"))


class PipelineCashReconTests(HandlerTestCase):
    """15:00 파이프라인 시작 대사: 판정만 하고 기준 예수금을 옮기지 않는다."""

    def test_judges_without_moving_the_baseline(self):
        from src.job_handlers import reconcile_cash
        reconcile_cash(self.conn, current_cash=1_000_000, now=CLOSE, trade_day=date(2026, 11, 25),
                       advance_baseline=True, first_run_date=date(2026, 1, 1))
        decision = reconcile_cash(self.conn, current_cash=3_000_000, now=CLOSE + timedelta(days=1),
                                  trade_day=date(2026, 11, 26), advance_baseline=False,
                                  first_run_date=date(2026, 1, 1))
        self.assertEqual(decision.value, "PROVISIONAL")
        baseline = self.conn.execute("SELECT value FROM system_state WHERE \"key\" = 'last_d2_cash'").fetchone()[0]
        self.assertEqual(baseline, "1000000")
        self.assertEqual(self.count("SELECT COUNT(*) FROM cash_flows WHERE classification_status='PROVISIONAL'"), 1)


class SafetyMaintenanceTests(HandlerTestCase):
    """24시간 이상 풀리지 않은 HALT 재경보."""

    def test_unresolved_halt_alerts_after_24h(self):
        raise_flag(self.conn, "HALT", "ACCOUNT_MISMATCH", now=MORNING)
        handler = create_safety_maintenance_handler(
            self.factory, run_dir=None, clock=lambda: MORNING + timedelta(hours=25), notify=self.notify)
        handler()
        self.assertTrue(any("ACCOUNT_MISMATCH" in m for _, m in self.notes))
        self.notes.clear()
        handler()  # 같은 간격 안에서는 반복하지 않는다
        self.assertEqual(self.notes, [])


if __name__ == "__main__":
    unittest.main()
