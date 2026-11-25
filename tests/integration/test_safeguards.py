"""안전장치: 티커별 매도 상호배제와 DB busy_timeout 을 검증합니다."""

import unittest

from src.job_handlers import create_intraday_guard_handler
from src.order_executor import SELL_ALREADY_ACTIVE, SellRequest, submit_order
from src.order_policy import OrderKind
from tests.integration.test_job_handlers import MORNING, FakeBroker, HandlerTestCase


class SellMutualExclusionTests(HandlerTestCase):
    def test_second_intent_selling_same_ticker_is_blocked_while_first_is_active(self):
        broker = FakeBroker()
        create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING)()
        other = SellRequest("069500", 10, OrderKind.RISK_ROTATION_SELL, "R069500:2026-11-25", "WEEKLY_ROTATION")
        result = submit_order(self.conn, broker, other, now=MORNING)
        self.assertEqual(result, SELL_ALREADY_ACTIVE)
        self.assertEqual(len(broker.transport.calls), 1)

    def test_repeated_guard_ticks_do_not_open_obligations_for_an_active_sell(self):
        broker = FakeBroker()
        handler = create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING)
        handler()
        handler()
        self.assertEqual(self.count("SELECT COUNT(*) FROM exit_obligations"), 0)


class UncertainOrderScopeTests(HandlerTestCase):
    """v3.7.5 D3: 접수 불확실 주문의 영향 범위는 주문 방향별로 한정된다."""

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO universe_assets (ticker, name, category, region, underlying_index, product_type,"
            " replication_method, status, data_status, added_at, asset_class) VALUES ('360750','E','RISK_ASSET','US',"
            "'K2','SPOT_ETF','PHYSICAL','ACTIVE','VALID','t','EQUITY')")
        self.conn.execute(
            "INSERT INTO positions (ticker, tracking_status, quantity, avg_buy_price, market_value, peak_price_adj,"
            " last_synced_at) VALUES ('360750','MANAGED',10,100,1000,100,'t')")
        self.conn.commit()

    def broker(self):
        from src.order_manager import Quote

        class Broker(FakeBroker):
            def quote(self, ticker):
                return Quote(bid=10000, ask=10005, nav=10000.0)

            def sellable_qty(self, ticker):
                return 10
        return Broker()

    def pending_buy(self, broker):
        broker.transport.response = {}  # 타임아웃 → PENDING_CONFIRMATION
        result = submit_order(self.conn, broker, SellRequest("069500", 5, OrderKind.PARKING_BUY, "B1", "T"),
                              now=MORNING)
        broker.transport.response = {"accepted": True, "order_number": "9"}
        return result

    def test_pending_buy_blocks_other_buys_but_not_risk_reduction_sells_elsewhere(self):
        broker = self.broker()
        self.pending_buy(broker)
        self.assertEqual(self.count("SELECT COUNT(*) FROM orders WHERE status='PENDING_CONFIRMATION'"), 1)
        again = submit_order(self.conn, broker, SellRequest("360750", 5, OrderKind.PARKING_BUY, "B2", "T"),
                             now=MORNING)
        self.assertEqual(again, "BLOCKED:GATE")  # 다른 티커 매수도 계좌 신규 매수 대기
        sell = submit_order(self.conn, broker, SellRequest("360750", 10, OrderKind.REDUCE_ONLY_SELL, "S1", "STOP"),
                            now=MORNING)
        self.assertEqual(sell, "SUBMITTED")       # 다른 종목 스탑은 계속 동작

    def test_pending_sell_blocks_only_its_own_ticker(self):
        broker = self.broker()
        broker.transport.response = {}
        first = submit_order(self.conn, broker, SellRequest("069500", 10, OrderKind.REDUCE_ONLY_SELL, "S1", "STOP"),
                             now=MORNING)
        self.assertEqual(first, "SUBMITTED")
        broker.transport.response = {"accepted": True, "order_number": "9"}
        same = submit_order(self.conn, broker, SellRequest("069500", 10, OrderKind.RISK_ROTATION_SELL, "R1", "T"),
                            now=MORNING)
        self.assertEqual(same, SELL_ALREADY_ACTIVE)
        other = submit_order(self.conn, broker, SellRequest("360750", 10, OrderKind.REDUCE_ONLY_SELL, "S2", "STOP"),
                             now=MORNING)
        self.assertEqual(other, "SUBMITTED")

    def test_uncertain_orders_never_create_safe_halt(self):
        from src.safety_flags import active_codes
        broker = self.broker()
        self.pending_buy(broker)
        self.assertEqual(active_codes(self.conn, "HALT"), ())


class GuardPollIntervalTests(HandlerTestCase):
    def test_interval_is_extended_and_recorded_when_holdings_exceed_the_rate_limit(self):
        from datetime import timedelta
        for index in range(12):
            self.conn.execute(
                "INSERT INTO positions (ticker, tracking_status, quantity, avg_buy_price, market_value, peak_price_adj,"
                " last_synced_at) VALUES (?, 'MANAGED', 1, 1, 1, 1, 't')", (f"9000{index:02d}",))
        self.conn.commit()
        times = [MORNING]
        broker = FakeBroker(price=100.0)
        calls = []
        original = broker.last_price
        broker.last_price = lambda ticker: calls.append(ticker) or original(ticker)
        handler = create_intraday_guard_handler(self.factory, broker, clock=lambda: times[0], rate_per_second=1.0)
        handler()
        first = len(calls)
        times[0] = MORNING + timedelta(seconds=10)
        handler()                      # 13초 주기라 10초 뒤는 건너뜀
        self.assertEqual(len(calls), first)
        times[0] = MORNING + timedelta(seconds=13)
        handler()
        self.assertGreater(len(calls), first)
        self.assertEqual(self.conn.execute(
            "SELECT value FROM system_state WHERE \"key\" = 'guard_poll_interval_seconds'").fetchone()[0], "13")  # 기본 보유 1종목 + 12종목


class DatabaseSettingsTests(HandlerTestCase):
    def test_connections_use_the_specified_busy_timeout(self):
        connection = self.factory()
        try:
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
