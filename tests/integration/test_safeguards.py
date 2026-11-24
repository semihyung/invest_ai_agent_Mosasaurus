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
        result = submit_order(self.conn, broker, other, now=MORNING, account_fresh=True)
        self.assertEqual(result, SELL_ALREADY_ACTIVE)
        self.assertEqual(len(broker.transport.calls), 1)

    def test_repeated_guard_ticks_do_not_open_obligations_for_an_active_sell(self):
        broker = FakeBroker()
        handler = create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING)
        handler()
        handler()
        self.assertEqual(self.count("SELECT COUNT(*) FROM exit_obligations"), 0)


class DatabaseSettingsTests(HandlerTestCase):
    def test_connections_use_the_specified_busy_timeout(self):
        connection = self.factory()
        try:
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
