"""KIS 주문 어댑터·체결 delta·비상 재가격 테스트(녹화 응답 재생, 실서버 호출 없음)."""

import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src._kis_order_adapter import (
    BASE_URLS,
    KisOrderAdapter,
    OrderRequestError,
    TR_IDS,
    TokenBucket,
)
from src.db import initialize_database
from src.order_manager import (
    OrderSide,
    OrderStatus,
    Quote,
    RealOrderGateContext,
    RepriceDecision,
    apply_late_fill_correction,
    cancel_can_finalize,
    create_kis_order_transport,
    etf_tick_size,
    fill_delta,
    next_emergency_price,
    record_fill_from_cumulative,
    round_down_to_tick,
    submit_recorded_limit_order,
    tick_down,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 24, 6, 0, tzinfo=timezone.utc)
ACCEPTED = (200, {"rt_cd": "0", "msg_cd": "APBK0013", "output": {"ODNO": "0000123", "KRX_FWDG_ORD_ORGNO": "91252"}})
REJECTED = (200, {"rt_cd": "1", "msg_cd": "APBK0919", "msg1": "주문 가능 금액 초과"})


class RecordingHttp:
    """요청을 기록하고 미리 정해진 응답(또는 예외)을 한 번씩 돌려주는 가짜 전송 함수입니다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), dict(body), timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_adapter(http, environment="PAPER", limiter=None):
    return KisOrderAdapter(
        environment,
        app_key="dummy-app-key",
        app_secret="dummy-app-secret",
        account_number="1234567801",
        token_provider=lambda: "dummy-token",
        http_post=http,
        rate_limiter=limiter,
    )


class AdapterContractTests(unittest.TestCase):
    """TR ID 표·요청 필드·응답 해석 계약입니다."""

    def test_tr_ids_come_from_the_table_for_each_environment(self):
        for environment, buy, sell in (("PAPER", "VTTC0012U", "VTTC0011U"), ("REAL", "TTTC0012U", "TTTC0011U")):
            http = RecordingHttp(ACCEPTED, ACCEPTED)
            adapter = make_adapter(http, environment)
            adapter.submit_limit_order(ticker="423160", side=OrderSide.BUY, quantity=1, price=110000)
            adapter.submit_limit_order(ticker="423160", side=OrderSide.SELL, quantity=1, price=110000)
            self.assertEqual([c[1]["tr_id"] for c in http.calls], [buy, sell])
            self.assertTrue(http.calls[0][0].startswith(BASE_URLS[environment]))

    def test_body_is_limit_only_uppercase_strings_with_split_account(self):
        http = RecordingHttp(ACCEPTED)
        make_adapter(http).submit_limit_order(
            ticker="423160", side=OrderSide.SELL, quantity=3, price=110500)
        body = http.calls[0][2]
        self.assertEqual(body["CANO"], "12345678")
        self.assertEqual(body["ACNT_PRDT_CD"], "01")
        self.assertEqual(body["ORD_DVSN"], "00")
        self.assertEqual((body["ORD_QTY"], body["ORD_UNPR"]), ("3", "110500"))
        self.assertEqual(body["SLL_TYPE"], "01")
        self.assertEqual(body["EXCG_ID_DVSN_CD"], "KRX")
        self.assertTrue(all(key.isupper() and isinstance(value, str) for key, value in body.items()))

    def test_market_orders_and_empty_prices_cannot_be_built(self):
        adapter = make_adapter(RecordingHttp())
        for kwargs in (
            dict(price=0), dict(price=-1), dict(price=None), dict(price=True), dict(quantity=0),
            dict(ticker="42316"), dict(side="HOLD"),
        ):
            params = dict(side="BUY", ticker="423160", quantity=1, price=1000)
            params.update(kwargs)
            with self.assertRaises(OrderRequestError, msg=kwargs):
                adapter.build_order_request(**params)

    def test_accepted_and_explicit_rejection_are_distinguished(self):
        ok = make_adapter(RecordingHttp(ACCEPTED)).submit_limit_order(
            ticker="423160", side=OrderSide.BUY, quantity=1, price=1000)
        self.assertEqual(ok, {"accepted": True, "order_number": "0000123", "org_number": "91252"})
        no = make_adapter(RecordingHttp(REJECTED)).submit_limit_order(
            ticker="423160", side=OrderSide.BUY, quantity=1, price=1000)
        self.assertEqual(no, {"rejected": True, "message_code": "APBK0919"})

    def test_ambiguous_failures_raise_timeout_so_orders_become_pending_confirmation(self):
        for response in (
            TimeoutError("slow"), ConnectionError("reset"),
            (200, None), (200, {"unexpected": 1}), (502, {"rt_cd": "1"}),
            (200, {"rt_cd": "0", "output": {}}),
        ):
            adapter = make_adapter(RecordingHttp(response))
            with self.assertRaises(TimeoutError, msg=response):
                adapter.submit_limit_order(ticker="423160", side=OrderSide.BUY, quantity=1, price=1000)

    def test_no_automatic_retry_on_post(self):
        http = RecordingHttp(TimeoutError("t"), ACCEPTED)
        with self.assertRaises(TimeoutError):
            make_adapter(http).submit_limit_order(
                ticker="423160", side=OrderSide.BUY, quantity=1, price=1000)
        self.assertEqual(len(http.calls), 1)

    def test_pipeline_through_submit_recorded_limit_order_maps_timeout_to_pending(self):
        gate_context = RealOrderGateContext(
            environment="PAPER", operator_approval=False, reconciliation_ok=True,
            reconciliation_fresh=True, unresolved_order_exists=False,
            unclassified_cash_flow_exists=False, token_valid=True, calendar_valid=True,
            quote_valid=True, adjusted_data_valid=True, backtest_calibrated=False,
            mode_allows_order=True, idempotency_conflict=False)
        result = submit_recorded_limit_order(
            make_adapter(RecordingHttp(TimeoutError("t"))),
            gate_context=gate_context,
            ticker="423160", side=OrderSide.BUY, quantity=1, price=1000)
        self.assertEqual(result.status, OrderStatus.PENDING_CONFIRMATION)

    def test_cancel_and_modify_use_rvsecncl_tr_and_codes(self):
        http = RecordingHttp(ACCEPTED, ACCEPTED)
        adapter = make_adapter(http, "REAL")
        adapter.cancel_or_modify(mode="CANCEL", org_number="91252", order_number="0000123",
                                 quantity=5, price=1000)
        adapter.cancel_or_modify(mode="MODIFY", org_number="91252", order_number="0000123",
                                 quantity=5, price=995, all_remaining=False)
        self.assertEqual([c[1]["tr_id"] for c in http.calls], ["TTTC0013U", "TTTC0013U"])
        self.assertEqual([c[2]["RVSE_CNCL_DVSN_CD"] for c in http.calls], ["02", "01"])
        self.assertEqual([c[2]["QTY_ALL_ORD_YN"] for c in http.calls], ["Y", "N"])

    def test_credentials_never_appear_in_repr_or_errors(self):
        adapter = make_adapter(RecordingHttp())
        for secret in ("dummy-app-key", "dummy-app-secret", "12345678", "dummy-token"):
            self.assertNotIn(secret, repr(adapter))
        with self.assertRaises(OrderRequestError) as caught:
            KisOrderAdapter("PAPER", app_key="k", app_secret="s", account_number="123",
                            token_provider=lambda: "t", http_post=RecordingHttp())
        self.assertNotIn("123", str(caught.exception).replace("10 digits", ""))

    def test_invalid_environment_is_rejected_and_no_prefix_conversion_exists(self):
        with self.assertRaises(OrderRequestError):
            make_adapter(RecordingHttp(), "STAGING")
        self.assertEqual(set(TR_IDS), {"PAPER", "REAL"})
        for name, tr_id in TR_IDS["PAPER"].items():
            if name != "CCNL_NOTICE":
                self.assertTrue(tr_id.startswith("V"), name)

    def test_factory_requires_an_explicit_transport(self):
        with self.assertRaises(TypeError):
            create_kis_order_transport(
                "PAPER", app_key="k", app_secret="s", account_number="1234567801",
                token_provider=lambda: "t")

    def test_rate_limiter_spaces_calls(self):
        clock = [0.0]
        sleeps = []
        limiter = TokenBucket(2, clock=lambda: clock[0],
                              sleeper=lambda s: (sleeps.append(s), clock.__setitem__(0, clock[0] + s)))
        limiter.acquire()
        limiter.acquire()
        self.assertEqual(sleeps, [0.5])

    def test_only_order_manager_imports_the_private_adapter(self):
        offenders = []
        for path in (ROOT / "src").glob("*.py"):
            if path.name in ("order_manager.py", "_kis_order_adapter.py"):
                continue
            if re.search(r"_kis_order_adapter|KisOrderAdapter", path.read_text(encoding="utf-8")):
                offenders.append(path.name)
        self.assertEqual(offenders, [])


class TickAndRepriceTests(unittest.TestCase):
    """ETF 호가표와 비상 매도 재가격 의사코드입니다."""

    def test_etf_tick_table_and_boundaries(self):
        self.assertEqual(etf_tick_size(1999), 1)
        self.assertEqual(etf_tick_size(2000), 5)
        self.assertEqual(round_down_to_tick(110503), 110500)
        self.assertEqual(round_down_to_tick(1999), 1999)
        self.assertEqual(tick_down(2000), 1999)
        self.assertEqual(tick_down(2005), 2000)
        self.assertEqual(tick_down(110500), 110495)

    def test_first_price_is_bid1_and_each_step_is_at_least_one_tick_lower(self):
        quote = Quote(bid=100000, ask=100100, nav=100500.0)
        first = next_emergency_price(quote, previous_price=None, reprice_count=0)
        self.assertEqual(first, RepriceDecision(100000, "OK"))
        same_bid = next_emergency_price(quote, previous_price=100000, reprice_count=1)
        self.assertEqual(same_bid.price, 99995)
        lower_bid = next_emergency_price(Quote(99000, 99100, 100500.0), previous_price=100000, reprice_count=1)
        self.assertEqual(lower_bid.price, 99000)

    def test_price_never_exceeds_bid1(self):
        for previous in (None, 100000, 99990):
            decision = next_emergency_price(
                Quote(99900, 100000, 100000.0), previous_price=previous, reprice_count=0)
            self.assertLessEqual(decision.price, 99900)

    def test_stops_when_quote_stale_or_nav_floor_breached_or_max_reached(self):
        quote = Quote(100000, 100100, 100500.0)
        self.assertEqual(next_emergency_price(quote, previous_price=None, reprice_count=0,
                                              quote_fresh=False).reason, "QUOTE_INVALID")
        self.assertEqual(next_emergency_price(Quote(0, 0, None), previous_price=None,
                                              reprice_count=0).reason, "QUOTE_INVALID")
        far = Quote(90000, 90100, 100000.0)  # iNAV 하한 98,000 미만
        self.assertEqual(next_emergency_price(far, previous_price=None, reprice_count=0).reason,
                         "MARKET_ANOMALY")
        self.assertEqual(next_emergency_price(quote, previous_price=100000, reprice_count=6).reason,
                         "MAX_REPRICES")

    def test_cancel_finalization_needs_consecutive_matches_or_close(self):
        self.assertFalse(cancel_can_finalize(consecutive_matches=1, market_closed_full_query=False))
        self.assertTrue(cancel_can_finalize(consecutive_matches=2, market_closed_full_query=False))
        self.assertTrue(cancel_can_finalize(consecutive_matches=0, market_closed_full_query=True))


class FillDeltaTests(unittest.TestCase):
    """누적 체결 증분 기록·WS 병합·지연 체결 보정입니다."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = initialize_database(Path(self.temp.name) / "t.db")
        self.addCleanup(self.connection.close)
        c = self.connection
        c.execute(
            "INSERT INTO universe_assets (ticker, name, category, region, underlying_index,"
            " product_type, replication_method, status, data_status, added_at)"
            " VALUES ('000001','E','RISK_ASSET','KR','K','SPOT_ETF','PHYSICAL','ACTIVE','VALID','t')")
        c.execute(
            "INSERT INTO order_intents (parent_intent_id, idempotency_key, cycle_id, intent_type,"
            " ticker, side, requested_qty, intent_payload_hash, status, created_at, updated_at)"
            " VALUES ('p','k','c','RISK_REDUCTION','000001','SELL',10,'h','IN_PROGRESS','t','t')")
        c.execute(
            "INSERT INTO orders (order_id, parent_intent_id, attempt_id, kis_order_no, ticker, side,"
            " requested_qty, remaining_qty, order_type, order_price, status, strategy_reason, updated_at)"
            " VALUES ('o1','p','a1','0000123','000001','SELL',10,10,'LIMIT',1000,'ACCEPTED','r','t')")

    def _fills(self):
        return [tuple(r) for r in self.connection.execute(
            "SELECT kis_execution_id, qty, cumulative_qty, round(price,4), price_source FROM trade_fills ORDER BY fill_id")]

    def test_fill_delta_math(self):
        self.assertEqual(fill_delta(previous_cumulative_qty=0, previous_average_price=0.0,
                                    new_cumulative_qty=4, new_average_price=1000.0), (4, 1000.0))
        qty, price = fill_delta(previous_cumulative_qty=4, previous_average_price=1000.0,
                                new_cumulative_qty=10, new_average_price=1010.0)
        self.assertEqual(qty, 6)
        self.assertAlmostEqual(price, 6100 / 6)
        with self.assertRaises(ValueError):
            fill_delta(previous_cumulative_qty=5, previous_average_price=1000.0,
                       new_cumulative_qty=4, new_average_price=1000.0)

    def test_partial_then_full_fill_records_deltas_and_quantities(self):
        record_fill_from_cumulative(self.connection, order_id="o1", new_cumulative_qty=6,
                                    new_average_price=1000.0, executed_at=NOW)
        record_fill_from_cumulative(self.connection, order_id="o1", new_cumulative_qty=10,
                                    new_average_price=1010.0, executed_at=NOW)
        self.assertEqual(self._fills(), [
            ("0000123:6", 6, 6, 1000.0, "REST_DELTA"),
            ("0000123:10", 4, 10, 1025.0, "REST_DELTA"),
        ])
        row = self.connection.execute("SELECT filled_qty, remaining_qty FROM orders").fetchone()
        self.assertEqual(tuple(row), (10, 0))

    def test_same_cumulative_quantity_is_merged_not_duplicated(self):
        for source in ("WS_NOTICE", "REST_DELTA"):
            record_fill_from_cumulative(self.connection, order_id="o1", new_cumulative_qty=6,
                                        new_average_price=1000.0, executed_at=NOW, source=source)
        self.assertEqual(len(self._fills()), 1)
        self.assertEqual(self._fills()[0][4], "WS_NOTICE")
        self.assertEqual(self._fills()[0][0], "0000123:WS:6")

    def test_decreasing_cumulative_quantity_is_rejected(self):
        record_fill_from_cumulative(self.connection, order_id="o1", new_cumulative_qty=6,
                                    new_average_price=1000.0, executed_at=NOW)
        with self.assertRaises(ValueError):
            record_fill_from_cumulative(self.connection, order_id="o1", new_cumulative_qty=4,
                                        new_average_price=1000.0, executed_at=NOW)

    def test_fill_cannot_exceed_requested_quantity(self):
        with self.assertRaises(ValueError):
            record_fill_from_cumulative(self.connection, order_id="o1", new_cumulative_qty=11,
                                        new_average_price=1000.0, executed_at=NOW)

    def test_late_fill_after_cancel_updates_quantity_without_changing_status(self):
        c = self.connection
        c.execute("UPDATE orders SET status = 'CANCEL_PENDING' WHERE order_id = 'o1'")
        c.execute("UPDATE orders SET status = 'CANCELED' WHERE order_id = 'o1'")
        self.assertTrue(apply_late_fill_correction(
            c, order_id="o1", new_cumulative_qty=3, new_average_price=1000.0, occurred_at=NOW))
        row = c.execute("SELECT status, filled_qty, remaining_qty FROM orders").fetchone()
        self.assertEqual(tuple(row), ("CANCELED", 3, 7))
        event = c.execute("SELECT event_type, old_status, new_status FROM order_events").fetchone()
        self.assertEqual(tuple(event), ("LATE_FILL_CORRECTION", "CANCELED", "CANCELED"))
        self.assertFalse(apply_late_fill_correction(
            c, order_id="o1", new_cumulative_qty=3, new_average_price=1000.0, occurred_at=NOW))

    def test_late_fill_correction_rejects_active_orders(self):
        with self.assertRaises(ValueError):
            apply_late_fill_correction(self.connection, order_id="o1", new_cumulative_qty=3,
                                       new_average_price=1000.0, occurred_at=NOW)


if __name__ == "__main__":
    unittest.main()
