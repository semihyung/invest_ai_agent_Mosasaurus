"""명세 잔여 항목 테스트: 달력 이중 검증(26)·호가 무효 가드(61)·마감 후 EXPIRED(45)·낙폭 자동 EMERGENCY(23·49)·단일 writer(29)·브리핑."""

import threading
import unittest
from datetime import date, datetime, timedelta, timezone

from src.calendar_check import verify_calendar
from src.job_handlers import (
    auto_emergency_if_critical, create_intraday_guard_handler, create_post_close_reconcile_handler,
    create_safety_maintenance_handler, save_daily_snapshot,
)
from src.morning_prep import create_morning_prep_handler
from src.ops_jobs import compose_briefing, performance_summary, xirr
from src.order_executor import QUOTE_INVALID, SellRequest, submit_order
from src.order_manager import Quote
from src.order_policy import OrderKind
from src.rate_limit import ORDER, PriorityRateLimiter
from src.reconciliation import OrderSnapshot
from src.safety_flags import active_codes, raise_flag
from tests.integration.test_job_handlers import CLOSE, MORNING, FakeBroker, HandlerTestCase
from tests.integration.test_morning_prep import series

KST = timezone(timedelta(hours=9))
DAY = date(2026, 11, 25)


class CalendarTests(HandlerTestCase):
    """시나리오 26: XKRX 와 KIS 휴장일이 다르면 CALENDAR_MISMATCH, 일치하면 자동 해제."""

    def test_agreement_returns_the_value_and_mismatch_raises_the_halt(self):
        self.assertIs(verify_calendar(self.conn, DAY, MORNING, kis_is_open=lambda d: True, xkrx_open=lambda d: True), True)
        self.assertIs(verify_calendar(self.conn, DAY, MORNING, kis_is_open=lambda d: False, xkrx_open=lambda d: False), False)
        self.assertIsNone(verify_calendar(self.conn, DAY, MORNING, kis_is_open=lambda d: True, xkrx_open=lambda d: False))
        self.assertIn("CALENDAR_MISMATCH", active_codes(self.conn, "HALT"))
        verify_calendar(self.conn, DAY, MORNING, kis_is_open=lambda d: True, xkrx_open=lambda d: True)
        self.assertNotIn("CALENDAR_MISMATCH", active_codes(self.conn, "HALT"))  # 일치하면 자동 해제

    def test_an_unavailable_source_fails_closed(self):
        def boom(d):
            raise ImportError("exchange_calendars")
        self.assertIsNone(verify_calendar(self.conn, DAY, MORNING, kis_is_open=lambda d: True, xkrx_open=boom))
        self.assertIn("CALENDAR_MISMATCH", active_codes(self.conn, "HALT"))

    def prep(self, **kwargs):
        create_morning_prep_handler(self.factory, lambda t: series(), clock=lambda: MORNING, **kwargs)()

    def test_morning_prep_skips_everything_on_a_closed_or_mismatched_day(self):
        before = self.count("SELECT COUNT(*) FROM daily_candles")
        self.prep(calendar_verify=lambda c, n: False)
        self.assertEqual(self.count("SELECT COUNT(*) FROM daily_candles"), before)
        self.prep(calendar_verify=lambda c, n: None)
        self.assertEqual(self.count("SELECT COUNT(*) FROM daily_candles"), before)

    def test_invalid_token_halts_credentials_and_valid_token_clears_it(self):
        self.prep(calendar_verify=lambda c, n: True, token_valid=lambda: False)
        self.assertIn("CREDENTIALS_UNAVAILABLE", active_codes(self.conn, "HALT"))
        self.prep(calendar_verify=lambda c, n: True, token_valid=lambda: True)
        self.assertNotIn("CREDENTIALS_UNAVAILABLE", active_codes(self.conn, "HALT"))

    def test_previous_days_unconfirmed_order_is_confirmed_by_morning_prep(self):
        broker = FakeBroker()
        broker.transport.response = {}
        create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING)()   # 타임아웃 → PENDING
        self.assertEqual(self.count("SELECT COUNT(*) FROM orders WHERE status = 'PENDING_CONFIRMATION'"), 1)
        broker._orders = []
        self.prep(calendar_verify=lambda c, n: True, sync_broker=broker)
        self.assertEqual(self.conn.execute("SELECT status FROM orders").fetchone()[0], "CONFIRMED_NOT_ACCEPTED")


class MaintenanceTokenTests(HandlerTestCase):
    def test_maintenance_raises_and_clears_credentials_unavailable(self):
        alerts = []
        state = {"ok": False}
        handler = create_safety_maintenance_handler(self.factory, run_dir=None, clock=lambda: MORNING,
                                                    notify=lambda s, m: alerts.append(s), token_valid=lambda: state["ok"])
        handler()
        self.assertIn("CREDENTIALS_UNAVAILABLE", active_codes(self.conn, "HALT"))
        self.assertEqual(alerts, ["CRITICAL"])
        state["ok"] = True
        handler()
        self.assertNotIn("CREDENTIALS_UNAVAILABLE", active_codes(self.conn, "HALT"))


class InvalidQuoteTests(HandlerTestCase):
    """시나리오 61: Bid1/Ask1 이 0 이하이면 예외 없이 계산을 생략하고 종목을 QUOTE_INVALID 로 격리한다."""

    def test_zero_bid_blocks_without_intent_and_flags_the_ticker(self):
        broker = FakeBroker(bid=0)
        result = submit_order(self.conn, broker, SellRequest("069500", 10, OrderKind.REDUCE_ONLY_SELL, "S1", "STOP"),
                              now=MORNING)
        self.assertEqual(result, QUOTE_INVALID)
        self.assertEqual(self.count("SELECT COUNT(*) FROM order_intents"), 0)
        self.assertEqual(active_codes(self.conn, "TICKER_BLOCK", ticker="069500"), ("QUOTE_INVALID",))
        self.assertEqual(broker.transport.calls, [])

    def test_guard_retries_after_the_quote_recovers_and_sells(self):
        broker = FakeBroker(bid=0)
        handler = create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING)
        handler()
        self.assertEqual(broker.transport.calls, [])
        broker.bid = 8500
        create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING + timedelta(seconds=30))()
        self.assertEqual(len(broker.transport.calls), 1)
        self.assertEqual(active_codes(self.conn, "TICKER_BLOCK", ticker="069500"), ())


class CommonGateTests(HandlerTestCase):
    """명세 7.1·7.2절 공통 게이트: 시각 창, 스프레드, 호가 단위, 대조 신선도, 게이트 번호 기록, cycle_id 포맷."""

    def sell(self, broker, when=MORNING, kind=OrderKind.REDUCE_ONLY_SELL):
        return submit_order(self.conn, broker, SellRequest("069500", 10, kind, "R2026-11-25:STOP", "STOP"),
                            now=when)

    def test_orders_are_only_allowed_between_open_and_the_new_order_cutoff(self):
        from src.order_executor import OUTSIDE_ORDER_WINDOW
        broker = FakeBroker()
        self.assertEqual(self.sell(broker, datetime(2026, 11, 25, 8, 59, tzinfo=KST)), OUTSIDE_ORDER_WINDOW)
        self.assertEqual(self.sell(broker, datetime(2026, 11, 25, 15, 10, tzinfo=KST)), OUTSIDE_ORDER_WINDOW)
        self.assertEqual(broker.transport.calls, [])
        self.assertEqual(self.sell(broker), "SUBMITTED")

    def test_risk_reduction_is_held_when_the_spread_exceeds_one_percent(self):
        from src.order_executor import SPREAD_WIDE
        broker = FakeBroker()
        broker.quote = lambda ticker: Quote(bid=100, ask=102, nav=None)   # 약 2%
        self.assertEqual(self.sell(broker), SPREAD_WIDE)
        self.assertEqual(self.count("SELECT COUNT(*) FROM order_intents"), 0)  # 의도 없이 다음 평가에서 재시도
        broker.quote = lambda ticker: Quote(bid=100, ask=101, nav=None)   # 약 1.0%
        self.assertEqual(self.sell(broker), "SUBMITTED")

    def test_locked_or_crossed_quote_is_invalid(self):
        """명세 7.2절: Ask1 > Bid1 이 아니면(동가·역전 포함) QUOTE_INVALID."""
        from src.order_manager import calculate_limit_price, validate_spread_and_nav
        from src.order_manager import OrderSide
        for quote in (Quote(bid=10000, ask=10000, nav=10000.0), Quote(bid=10005, ask=10000, nav=10000.0)):
            broker = FakeBroker()
            broker.quote = lambda ticker, q=quote: q
            self.assertEqual(self.sell(broker), QUOTE_INVALID)
            with self.assertRaises(ValueError):
                calculate_limit_price(quote, OrderSide.SELL)
            self.assertFalse(validate_spread_and_nav(quote, region="KR"))

    def test_quote_must_be_a_multiple_of_the_etf_tick(self):
        broker = FakeBroker()
        broker.quote = lambda ticker: Quote(bid=12003, ask=12005, nav=None)   # 2,000원 이상은 5원 단위
        self.assertEqual(self.sell(broker), QUOTE_INVALID)
        self.assertEqual(active_codes(self.conn, "TICKER_BLOCK", ticker="069500"), ("QUOTE_INVALID",))

    def test_a_stale_reconciliation_blocks_normal_orders_but_not_protective_exits(self):
        self.conn.execute("UPDATE system_state SET value = ? WHERE key = 'last_reconciliation_at'",
                          ((MORNING - timedelta(seconds=301)).isoformat(),))
        self.conn.commit()
        broker = FakeBroker()
        # 감사 3: 신선도 만료는 일시 차단 — 의도·의무를 남기지 않아 다음 틱에 같은 cycle 로 재시도된다.
        self.assertEqual(self.sell(broker), "BLOCKED:RECONCILIATION_STALE")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0], 0)
        # 대조가 신선하지만 불일치(reconciliation_ok=false)면 게이트 2 에서 막히고 번호가 기록된다.
        self.conn.execute("UPDATE system_state SET value = ? WHERE key = 'last_reconciliation_at'", (MORNING.isoformat(),))
        self.conn.execute("UPDATE system_state SET value = 'false' WHERE key = 'reconciliation_ok'")
        self.conn.commit()
        self.assertEqual(self.sell(broker), "BLOCKED:GATE")
        reason = self.conn.execute("SELECT reason_code FROM order_events WHERE new_status = 'REJECTED'").fetchone()[0]
        self.assertEqual(reason, "GATE_2_FAILED")          # 실패한 게이트 번호 기록
        self.conn.execute("UPDATE system_state SET value = ? WHERE key = 'last_reconciliation_at'",
                          ((MORNING - timedelta(seconds=301)).isoformat(),))
        self.conn.commit()
        raise_flag(self.conn, "HALT", "ACCOUNT_MISMATCH", now=MORNING)
        broker2 = FakeBroker()
        result = submit_order(self.conn, broker2, SellRequest("069500", 10, OrderKind.PROTECTIVE_EXIT_SELL, "R2026-11-25:STOP2", "STOP"),
                              now=MORNING)
        self.assertEqual(result, "SUBMITTED")

    def test_cycle_id_formats_follow_the_spec(self):
        from src.order_executor import parking_cycle_id, risk_cycle_id, weekly_cycle_id
        self.assertEqual(risk_cycle_id("2026-11-25", "STOP"), "R2026-11-25:STOP")
        self.assertEqual(weekly_cycle_id("2026-11-23"), "W2026-48")
        self.assertEqual(weekly_cycle_id("2026-11-25"), "W2026-48")     # 같은 주는 같은 값(승계 실행 포함)
        self.assertEqual(parking_cycle_id("2026-11-24"), "D2026-11-24")
        with self.assertRaises(ValueError):
            risk_cycle_id("2026-11-25", "OTHER")

    def test_guard_keeps_the_reconciliation_fresh_within_the_ttl(self):
        self.conn.execute("UPDATE system_state SET value = '2020-01-01T00:00:00+00:00' WHERE key = 'last_reconciliation_at'")
        self.conn.commit()
        broker = FakeBroker(price=95.0)
        create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING)()
        last = datetime.fromisoformat(self.conn.execute(
            "SELECT value FROM system_state WHERE key = 'last_reconciliation_at'").fetchone()[0])
        self.assertLessEqual((MORNING - last).total_seconds(), 1)


class ExpiryAfterCloseTests(HandlerTestCase):
    """시나리오 45: 장 마감 후 취소·체결 없이 남은 잔량은 EXPIRED 로 확정되고 위험 축소 매도는 의무로 넘어간다."""

    def test_unfilled_accepted_sell_expires_and_becomes_an_obligation(self):
        broker = FakeBroker()
        create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING)()
        broker._orders = [OrderSnapshot("0000001", "069500", "SELL", 10, 0, 10, "ACCEPTED")]
        create_post_close_reconcile_handler(self.factory, broker, clock=lambda: CLOSE)()
        self.assertEqual(self.conn.execute("SELECT status FROM orders").fetchone()[0], "EXPIRED")
        self.assertEqual(self.count("SELECT COUNT(*) FROM exit_obligations WHERE status = 'OPEN' AND required_qty = 10"), 1)


class AutoEmergencyTests(HandlerTestCase):
    """시나리오 23·49: 낙폭 -15% 도달 시 EMERGENCY(원인 DRAWDOWN) 자동 발동, 해제 직후 재발동 없음."""

    def state(self, key):
        row = self.conn.execute('SELECT value FROM system_state WHERE "key" = ?', (key,)).fetchone()
        return row[0] if row else None

    def test_marketclose_snapshot_at_minus_20_percent_triggers_emergency(self):
        create_post_close_reconcile_handler(self.factory, FakeBroker(equity=1_000_000), clock=lambda: CLOSE)()
        create_post_close_reconcile_handler(self.factory, FakeBroker(equity=800_000), clock=lambda: CLOSE + timedelta(days=1))()
        self.assertEqual((self.state("emergency_mode"), self.state("emergency_reason")), ("true", "DRAWDOWN"))

    def test_intraday_check_triggers_and_is_suppressed_while_a_rebase_is_pending(self):
        save_daily_snapshot(self.conn, trade_date="2026-11-24", total_equity=1_000_000, cash=0, now=MORNING)
        self.assertFalse(auto_emergency_if_critical(self.conn, -0.05, MORNING))   # 임계 미만
        self.conn.execute("INSERT INTO system_state (key, value, updated_at) VALUES ('hwm_rebase_pending','true','t')")
        self.conn.commit()
        self.assertFalse(auto_emergency_if_critical(self.conn, -0.30, MORNING))   # 해제 직후: 재발동 금지
        self.conn.execute("UPDATE system_state SET value = 'false' WHERE key = 'hwm_rebase_pending'")
        self.conn.commit()
        self.assertTrue(auto_emergency_if_critical(self.conn, -0.30, MORNING))
        self.assertFalse(auto_emergency_if_critical(self.conn, -0.30, MORNING))   # 이미 켜져 있으면 다시 켜지 않음

    def test_guard_watches_drawdown_every_minute(self):
        save_daily_snapshot(self.conn, trade_date="2026-11-24", total_equity=1_000_000, cash=0, now=MORNING)
        notes = []
        broker = FakeBroker(price=95.0, equity=800_000)
        create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING, notify=lambda s, m: notes.append(m))()
        self.assertEqual(self.state("emergency_mode"), "true")
        self.assertTrue(any("EMERGENCY" in m for m in notes))


class SingleWriterTests(HandlerTestCase):
    """시나리오 29: 쓰기 블록은 BEGIN IMMEDIATE 로 시작하고 여러 스레드의 쓰기가 오류 없이 직렬화된다."""

    def test_with_block_holds_an_immediate_write_transaction(self):
        connection = self.factory()
        try:
            with connection:
                self.assertTrue(connection.in_transaction)
        finally:
            connection.close()

    def test_concurrent_writers_do_not_fail_with_database_locked(self):
        errors = []

        def writer(tag):
            connection = self.factory()
            try:
                for i in range(40):
                    with connection:
                        connection.execute("INSERT INTO system_events (component, event_type, severity, message, created_at)"
                                           " VALUES ('t', ?, 'INFO', 'x', 't')", (f"{tag}{i}",))
            except Exception as error:  # noqa: BLE001
                errors.append(error)
            finally:
                connection.close()

        threads = [threading.Thread(target=writer, args=(tag,)) for tag in "abc"]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(self.count("SELECT COUNT(*) FROM system_events WHERE component = 't'"), 120)


class BriefingTests(HandlerTestCase):
    """9.1절 18:05 브리핑: MDD·XIRR·슬롯·토큰 만료·API 호출 수."""

    def test_xirr_matches_a_known_return(self):
        rate = xirr([(date(2026, 1, 1), -1000.0), (date(2027, 1, 1), 1100.0)])
        self.assertAlmostEqual(rate, 0.10, places=3)
        self.assertIsNone(xirr([(date(2026, 1, 1), 100.0), (date(2026, 2, 1), 100.0)]))

    def test_briefing_contains_all_spec_items(self):
        save_daily_snapshot(self.conn, trade_date="2026-11-20", total_equity=1_000_000, cash=0, now=MORNING - timedelta(days=5))
        save_daily_snapshot(self.conn, trade_date="2026-11-24", total_equity=900_000, cash=0, now=MORNING - timedelta(days=1))
        self.conn.execute("INSERT INTO portfolio_slots (slot_id, target_weight, ticker, assigned_at, assignment_reason)"
                          " VALUES ('S1', 0.45, '069500', 't', 'r')")
        self.conn.commit()
        text = compose_briefing(self.conn, MORNING, token_expiry=MORNING + timedelta(hours=5), api_calls_today=321)
        for expected in ("MDD -10.00%", "XIRR", "S1=069500", "토큰 만료", "API 호출 321회"):
            self.assertIn(expected, text)
        self.assertIn("MDD", performance_summary(self.conn))

    def test_limiter_counts_calls_for_the_quota_line(self):
        limiter = PriorityRateLimiter(1000)
        for _ in range(3):
            limiter.acquire(ORDER)
        self.assertEqual(limiter.calls_today(), 3)


if __name__ == "__main__":
    unittest.main()
