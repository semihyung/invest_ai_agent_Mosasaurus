"""rate_limit: 우선순위 순서·초당 한도·폴링 주기 자동 연장을 검증합니다."""

import threading
import time
import unittest

from src.rate_limit import GUARD, ORDER, OTHER, PriorityRateLimiter, RateLimitedBroker, guard_poll_interval


class LimiterTests(unittest.TestCase):
    def test_calls_are_spaced_by_the_minimum_interval(self):
        limiter = PriorityRateLimiter(50)  # 20ms 간격
        start = time.monotonic()
        for _ in range(6):
            limiter.acquire(OTHER)
        self.assertGreaterEqual(time.monotonic() - start, 0.09)

    def test_higher_priority_waiter_goes_first(self):
        limiter = PriorityRateLimiter(20)  # 50ms 간격
        limiter.acquire(ORDER)             # 슬롯 소진: 이후 대기자들이 줄을 선다
        order, gate = [], threading.Barrier(3)

        def worker(priority, name):
            gate.wait()
            limiter.acquire(priority)
            order.append(name)

        threads = [threading.Thread(target=worker, args=(OTHER, "other")),
                   threading.Thread(target=worker, args=(GUARD, "guard")),
                   threading.Thread(target=worker, args=(ORDER, "order"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertEqual(order, ["order", "guard", "other"])

    def test_invalid_arguments(self):
        with self.assertRaises(ValueError):
            PriorityRateLimiter(0)
        with self.assertRaises(ValueError):
            PriorityRateLimiter(1).acquire(9)


class FakeInner:
    environment = "PAPER"

    class transport:  # noqa: N801 - 가짜 전송기
        sent = []

        @staticmethod
        def submit_limit_order(**kwargs):
            FakeInner.transport.sent.append(kwargs)
            return {"accepted": True, "order_number": "1"}

    def last_price(self, ticker):
        return 1.0

    def candles(self, ticker, today):
        return []


class BrokerWrapperTests(unittest.TestCase):
    def test_every_call_consumes_a_slot_and_attributes_pass_through(self):
        calls = []

        class Recording(PriorityRateLimiter):
            def acquire(self, priority):
                calls.append(priority)

        broker = RateLimitedBroker(FakeInner(), Recording(1))
        broker.last_price("069500")
        broker.candles("069500", None)
        broker.transport.submit_limit_order(ticker="069500", quantity=1)
        self.assertEqual(calls, [GUARD, OTHER, ORDER])
        self.assertEqual(broker.environment, "PAPER")


class PollIntervalTests(unittest.TestCase):
    def test_interval_grows_only_when_calls_exceed_the_limit(self):
        self.assertEqual(guard_poll_interval(0, 1), 10)
        self.assertEqual(guard_poll_interval(5, 1), 10)
        self.assertEqual(guard_poll_interval(12, 1), 12)
        self.assertEqual(guard_poll_interval(12, 2), 10)


if __name__ == "__main__":
    unittest.main()
