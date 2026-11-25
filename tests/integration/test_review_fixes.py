"""코드 리뷰 10건 수정의 회귀 테스트(잠금 교착·조회 예외·GLOBAL·격리·최고가·교체 재평가·승계·STALE·프리뷰 발송)."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from src.daily_pipeline import PipelineConfig, create_daily_pipeline_handler
from src.config import load_config
from src.db import connect_database
from src.job_handlers import (
    create_intraday_guard_handler, create_pending_confirmation_poller, create_post_close_reconcile_handler,
    create_safety_maintenance_handler,
)
from src.order_executor import (
    BROKER_UNAVAILABLE, QUOTE_UNAVAILABLE, SellRequest, submit_order,
)
from src.order_manager import Quote
from src.order_policy import OrderKind
from src.reconciliation import Position
from src.safety_flags import active_codes, raise_flag
from src.scheduler import (
    LOCK_EXEMPT_JOBS, ScheduleJob, _guarded_job, is_order_scheduler_locked,
)
from src.scheduler_jobs import deliver_previews
from tests.integration.test_daily_pipeline import PipelineBroker, PipelineTestCase, T0
from tests.integration.test_job_handlers import CLOSE, MORNING, FakeBroker, HandlerTestCase

KST = timezone(timedelta(hours=9))


class LockDeadlockTests(HandlerTestCase):
    """리뷰 1: 대조·동기화·취소 정리는 주문 잠금 중에도 돌고, 대조 성공 시 잠금이 풀린다."""

    def test_recovery_jobs_are_exempt_from_the_order_lock(self):
        self.assertEqual(LOCK_EXEMPT_JOBS, {ScheduleJob.ORDER_CANCEL_SWEEP, ScheduleJob.POST_CLOSE_RECONCILE,
                                            ScheduleJob.PENDING_CONFIRMATION_POLLER})
        ran = []
        self.conn.execute("INSERT INTO system_state (key, value, updated_at) VALUES ('scheduler_order_lock','true','t')")
        self.conn.commit()
        _guarded_job(ScheduleJob.POST_CLOSE_RECONCILE, lambda: ran.append("close"), self.factory, lambda: True)
        _guarded_job(ScheduleJob.INTRADAY_GUARD, lambda: ran.append("guard"), self.factory, lambda: True)
        self.assertEqual(ran, ["close"])  # 스탑 감시는 여전히 잠금, 대조는 실행

    def test_successful_reconcile_releases_the_lock_without_restart(self):
        self.conn.execute("INSERT INTO system_state (key, value, updated_at) VALUES ('scheduler_order_lock','true','t')")
        self.conn.commit()
        create_post_close_reconcile_handler(self.factory, FakeBroker(), clock=lambda: CLOSE)()
        self.assertFalse(is_order_scheduler_locked(self.conn))


class UnavailableQuoteTests(HandlerTestCase):
    """리뷰 2: 호가·잔고 조회 예외는 의도를 남기지 않고, 다음 틱에 그대로 재시도된다."""

    def test_quote_failure_leaves_no_intent_and_next_tick_retries(self):
        broker = FakeBroker()
        original = broker.quote
        broker.quote = lambda ticker: (_ for _ in ()).throw(TimeoutError("down"))
        handler = create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING)
        handler()
        self.assertEqual(self.count("SELECT COUNT(*) FROM order_intents"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM exit_obligations"), 0)
        broker.quote = original
        handler2 = create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING + timedelta(seconds=20))
        handler2()
        self.assertEqual(len(broker.transport.calls), 1)

    def test_submit_order_returns_codes_instead_of_raising(self):
        broker = FakeBroker()
        broker.quote = lambda ticker: (_ for _ in ()).throw(TimeoutError("x"))
        request = SellRequest("069500", 10, OrderKind.REDUCE_ONLY_SELL, "S1", "STOP")
        self.assertEqual(submit_order(self.conn, broker, request, now=MORNING), QUOTE_UNAVAILABLE)
        broker2 = FakeBroker()
        broker2.sellable_qty = lambda ticker: (_ for _ in ()).throw(TimeoutError("x"))
        self.assertEqual(submit_order(self.conn, broker2, request, now=MORNING), BROKER_UNAVAILABLE)

    def test_existing_intent_short_circuits_before_any_broker_call(self):
        broker = FakeBroker()
        request = SellRequest("069500", 10, OrderKind.REDUCE_ONLY_SELL, "S1", "STOP")
        submit_order(self.conn, broker, request, now=MORNING)
        broker.quote = lambda ticker: (_ for _ in ()).throw(AssertionError("must not be called"))
        self.assertEqual(submit_order(self.conn, broker, request, now=MORNING), "DUPLICATE")


class GlobalRegionTests(HandlerTestCase):
    """리뷰 3: GLOBAL 지역 전략 주문이 예외 없이 검증된다."""

    def test_spread_check_accepts_global_region(self):
        from src.order_manager import validate_spread_and_nav
        self.assertTrue(validate_spread_and_nav(Quote(bid=10000, ask=10005, nav=10000.0), region="GLOBAL"))
        self.assertFalse(validate_spread_and_nav(Quote(bid=10000, ask=10005, nav=9000.0), region="GLOBAL"))  # 괴리 > 1.2%


class QuarantineTests(HandlerTestCase):
    """리뷰 4: 격리 종목은 스탑 감시가 건너뛴다(명세 4.2절)."""

    def test_guard_skips_a_quarantined_ticker(self):
        raise_flag(self.conn, "TICKER_BLOCK", "CORPORATE_ACTION_UNVERIFIED", now=MORNING, ticker="069500")
        broker = FakeBroker()
        create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING)()
        self.assertEqual(broker.transport.calls, [])


class PeakFreshnessTests(HandlerTestCase):
    """리뷰 5: 마감 후 당일 종가(현재가)가 최고가에 즉시 반영된다."""

    def test_peak_uses_the_closing_price_not_yesterdays_candle(self):
        create_post_close_reconcile_handler(self.factory, FakeBroker(price=130.0), clock=lambda: CLOSE)()
        self.assertEqual(self.conn.execute("SELECT peak_price_adj FROM positions").fetchone()[0], 130.0)


class TransientStaleTests(HandlerTestCase):
    """리뷰 8: 일시 조회 실패로 걸린 매수 차단은 다음 성공 조회에서 풀리고, 대조 불일치 대기는 건드리지 않는다."""

    def test_stale_flag_from_a_failed_quote_clears_after_a_good_tick(self):
        broker = FakeBroker(price=95.0)
        broker.fail = True
        create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING)()
        self.assertIn("ACCOUNT_DATA_STALE", active_codes(self.conn, "BUY_BLOCK"))
        broker.fail = False
        create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING + timedelta(seconds=30))()
        self.assertNotIn("ACCOUNT_DATA_STALE", active_codes(self.conn, "BUY_BLOCK"))

    def test_reconciliation_pending_flag_is_not_cleared_by_a_good_tick(self):
        from src.job_handlers import _clear_transient_stale
        raise_flag(self.conn, "BUY_BLOCK", "ACCOUNT_DATA_STALE", now=MORNING, detail="PROVISIONAL_ACCOUNT_MISMATCH")
        _clear_transient_stale(self.conn, MORNING)  # 일시 조회 실패 표식이 없으므로 건드리지 않는다
        self.assertIn("ACCOUNT_DATA_STALE", active_codes(self.conn, "BUY_BLOCK"))

    def test_poller_success_clears_transient_stale(self):
        self.conn.execute("INSERT INTO safety_flags (flag_type, code, auto_clearable, detail_sanitized, created_at,"
                          " last_seen_at) VALUES ('BUY_BLOCK','ACCOUNT_DATA_STALE',1,'TRANSIENT:x','t','t')")
        self.conn.commit()
        broker = FakeBroker()
        create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING)()  # 활성 주문 만들기
        create_pending_confirmation_poller(self.factory, broker, clock=lambda: MORNING + timedelta(seconds=15))()
        self.assertNotIn("ACCOUNT_DATA_STALE", active_codes(self.conn, "BUY_BLOCK"))


class RotationRetryTests(PipelineTestCase):
    """리뷰 6·7: 스프레드에 막힌 교체 매도의 재평가와 주간 교체 승계 1회 제한."""

    def run_at(self, broker, when):
        self.now = when
        create_daily_pipeline_handler(lambda: connect_database(self.path), broker, CFG,
                                      clock=lambda: self.now, sleep=self.sleep)()

    def test_spread_blocked_rotation_sell_is_retried_every_30_seconds_until_it_clears(self):
        self.add_preview(["360750"])
        broker = PipelineBroker({"069500": 110, "360750": 110, "423160": 100}, holdings={"069500": 10})
        calls = {"n": 0}
        good = broker.quote

        def quote(ticker):
            calls["n"] += 1
            return Quote(bid=100, ask=110, nav=105.0) if ticker == "069500" and calls["n"] < 8 else good(ticker)
        broker.quote = quote
        self.run_pipeline(broker)
        sells = [c for c in self.calls(broker) if c[1] == "SELL"]
        self.assertEqual(sells, [("069500", "SELL", 10)])  # 막혔다가 풀린 뒤 제출됨

    def test_unsubmitted_rotation_sell_blocks_risk_buys_and_logs(self):
        self.add_preview(["360750"])
        broker = PipelineBroker({"069500": 110, "360750": 110, "423160": 100}, holdings={"069500": 10})
        wide = lambda ticker: Quote(bid=90, ask=110, nav=100.0) if ticker == "069500" else Quote(bid=11000, ask=11005, nav=11000.0)
        broker.quote = wide
        self.run_pipeline(broker)
        self.assertEqual([c for c in self.calls(broker) if c[0] == "360750"], [])  # 위험자산 매수 생략
        self.assertEqual(self.count("SELECT COUNT(*) FROM system_events WHERE event_type='ROTATION_SELL_UNSUBMITTED'"), 1)

    def test_rotation_sell_failing_for_a_non_spread_reason_also_blocks_risk_buys(self):
        """감사 2: 호가 조회 실패로 못 낸 교체 매도도 재평가 대상이며, 끝내 못 내면 위험자산 매수를 생략한다."""
        self.add_preview(["360750"])
        broker = PipelineBroker({"069500": 110, "360750": 110, "423160": 100}, holdings={"069500": 10})
        good = broker.quote

        def quote(ticker):
            """069500 호가만 계속 실패한다."""
            if ticker == "069500":
                raise TimeoutError("down")
            return good(ticker)
        broker.quote = quote
        self.run_pipeline(broker)
        self.assertEqual([c for c in self.calls(broker) if c[0] == "360750"], [])
        self.assertEqual(self.count("SELECT COUNT(*) FROM system_events WHERE event_type='ROTATION_SELL_UNSUBMITTED'"), 1)

    def test_preview_expires_after_the_first_day_plus_one_carryover(self):
        self.add_preview(["069500"])
        raise_flag(self.conn, "BUY_BLOCK", "UNCLASSIFIED_CASH_FLOW", now=T0)  # 매수를 막아 소비되지 않게 함
        for offset in (0, 1, 2):
            self.run_at(PipelineBroker({"069500": 110, "360750": 110, "423160": 100}), T0 + timedelta(days=offset))
        self.assertEqual(self.conn.execute("SELECT status FROM weekly_previews").fetchone()[0], "EXPIRED")

    def test_same_day_rerun_does_not_burn_a_carryover_day(self):
        self.add_preview(["069500"])
        raise_flag(self.conn, "BUY_BLOCK", "UNCLASSIFIED_CASH_FLOW", now=T0)
        for _ in range(3):
            self.run_at(PipelineBroker({"069500": 110, "360750": 110, "423160": 100}), T0)
        self.assertEqual(self.conn.execute("SELECT status FROM weekly_previews").fetchone()[0], "SENT")


CFG = PipelineConfig.from_rules(load_config())


class PreviewDeliveryTests(HandlerTestCase):
    """리뷰 9: 주간 프리뷰 발송 결과가 상태에 반영되고 실패는 10분 간격 6회까지 재시도된다."""

    def add_preview(self):
        self.conn.execute(
            "INSERT INTO weekly_previews (preview_id, base_candle_date, ranked_candidates_json, projected_slots_json,"
            " projected_trades_json, status, created_at) VALUES ('w1','2026-11-20',?,?,'[]','CREATED','t')",
            (json.dumps([{"ticker": "069500"}]), json.dumps([{"slot_id": "S1", "ticker": "069500"}])))
        self.conn.commit()

    def status(self):
        return self.conn.execute("SELECT status FROM weekly_previews").fetchone()[0]

    def test_success_marks_sent_and_message_contains_ranking(self):
        self.add_preview()
        sent = []
        deliver_previews(self.conn, lambda sev, msg: sent.append(msg) or True, MORNING)
        self.assertEqual(self.status(), "SENT")
        self.assertIn("069500", sent[0])

    def test_failure_is_retried_every_10_minutes_at_most_6_times(self):
        self.add_preview()
        attempts = []
        notify = lambda sev, msg: attempts.append(1) and False
        now = MORNING
        for _ in range(10):
            deliver_previews(self.conn, notify, now)
            now += timedelta(minutes=11)
        self.assertEqual(self.status(), "DELIVERY_FAILED")
        self.assertEqual(len(attempts), 6)

    def test_maintenance_handler_resends_failed_previews(self):
        self.add_preview()
        deliver_previews(self.conn, lambda sev, msg: False, MORNING)
        create_safety_maintenance_handler(self.factory, run_dir=None, clock=lambda: MORNING + timedelta(minutes=11),
                                          notify=lambda sev, msg: True)()
        self.assertEqual(self.status(), "SENT")

    def test_a_failed_delivery_does_not_stop_the_rotation_from_using_the_preview(self):
        from src.daily_pipeline import _load_preview
        self.add_preview()
        deliver_previews(self.conn, lambda sev, msg: False, MORNING)
        self.conn.execute("UPDATE weekly_previews SET created_at = ?", ((MORNING - timedelta(days=4)).isoformat(),))
        self.conn.commit()
        self.assertIsNotNone(_load_preview(self.conn, MORNING))  # DELIVERY_FAILED 여도 집행 대상


if __name__ == "__main__":
    unittest.main()

