import unittest
from datetime import date, datetime, timedelta, timezone

from src.corporate_actions import (
    CorporateAction,
    adjustment_factor_for_verified_actions,
    apply_adjustment_once,
)
from src.market_data import (
    DataQualityGate,
    daily_candles_from_kis,
    DailyCandle,
    FreshnessStatus,
    IntradaySnapshot,
    freshness_status,
    parse_kis_nav_record,
    snapshot_from_kis_records,
    validate_nav_deviation,
    validate_quote,
)


UTC = timezone.utc


class MarketDataTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 22, 6, 0, tzinfo=UTC)
        self.snapshot = IntradaySnapshot(
            ticker="423160",
            bid=10000,
            ask=10010,
            current_price=10005,
            nav=10004.0,
            observed_at=self.now,
            received_at=self.now,
        )

    def test_quote_is_fresh_and_within_spread_limit(self):
        self.assertEqual(
            freshness_status(self.now, self.now, 15),
            FreshnessStatus.FRESH,
        )
        self.assertTrue(validate_quote(self.snapshot, self.now))

    def test_quote_expired_by_ttl_is_stale(self):
        expired = IntradaySnapshot(
            ticker="423160",
            bid=10000,
            ask=10010,
            current_price=10005,
            nav=10004.0,
            observed_at=self.now - timedelta(seconds=16),
            received_at=self.now - timedelta(seconds=16),
        )

        self.assertFalse(validate_quote(expired, self.now))

    def test_quote_with_stale_observation_is_rejected_when_received_recently(self):
        stale_observation = IntradaySnapshot(
            ticker="423160",
            bid=10000,
            ask=10010,
            current_price=10005,
            nav=10004.0,
            observed_at=self.now - timedelta(seconds=16),
            received_at=self.now,
        )

        self.assertFalse(validate_quote(stale_observation, self.now))

    def test_quote_above_spread_limit_is_rejected(self):
        expired = IntradaySnapshot(
            ticker="423160",
            bid=10000,
            ask=11000,
            current_price=10500,
            nav=10500.0,
            observed_at=self.now,
            received_at=self.now,
        )

        self.assertFalse(validate_quote(expired, self.now))

    def test_nav_limits_follow_region(self):
        self.assertTrue(
            validate_nav_deviation(
                self.snapshot,
                self.now,
                region="KR",
            )
        )

        us_snapshot = IntradaySnapshot(
            ticker="000001",
            bid=10000,
            ask=10010,
            current_price=10110,
            nav=10000.0,
            observed_at=self.now,
            received_at=self.now,
        )
        self.assertTrue(
            validate_nav_deviation(us_snapshot, self.now, region="US")
        )
        self.assertFalse(
            validate_nav_deviation(
                IntradaySnapshot(
                    ticker="000001",
                    bid=10000,
                    ask=10010,
                    current_price=10200,
                    nav=10000.0,
                    observed_at=self.now,
                    received_at=self.now,
                ),
                self.now,
                region="US",
            )
        )

    def test_snapshot_maps_kis_quote_and_best_orderbook_fields(self):
        snapshot = snapshot_from_kis_records(
            "423160",
            {"stck_prpr": "10005"},
            {"bidp1": "10000", "askp1": "10010"},
            nav=10004.0,
            observed_at=self.now,
            received_at=self.now,
        )

        self.assertEqual(
            (snapshot.bid, snapshot.ask, snapshot.current_price, snapshot.nav),
            (10000, 10010, 10005, 10004.0),
        )

    def test_snapshot_rejects_non_integer_or_missing_quote_fields(self):
        with self.assertRaisesRegex(ValueError, "positive integer"):
            snapshot_from_kis_records(
                "423160",
                {"stck_prpr": "10005.5"},
                {"bidp1": "10000", "askp1": "10010"},
                nav=10004.0,
                observed_at=self.now,
                received_at=self.now,
            )

    def test_received_timestamp_cannot_be_in_the_future(self):
        with self.assertRaisesRegex(ValueError, "future"):
            freshness_status(
                self.now + timedelta(seconds=1),
                self.now,
                15,
            )

    def test_nav_record_must_be_positive(self):
        self.assertEqual(parse_kis_nav_record({"nav": "10004.0"}), 10004.0)
        with self.assertRaisesRegex(ValueError, "positive"):
            parse_kis_nav_record({"nav": "0"})

    def test_raw_and_adjusted_kis_candles_are_joined_by_date(self):
        raw_rows = [
            {
                "stck_bsop_date": "20261120",
                "stck_oprc": "100",
                "stck_hgpr": "110",
                "stck_lwpr": "90",
                "stck_clpr": "105",
                "acml_vol": "50",
            }
        ]
        adjusted_rows = [
            {
                "stck_bsop_date": "20261120",
                "stck_clpr": "95",
                "acml_vol": "50",
            }
        ]

        candles = daily_candles_from_kis("423160", raw_rows, adjusted_rows)

        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].trade_date, "2026-11-20")
        self.assertEqual(candles[0].adjusted_close, 95)
        self.assertEqual(candles[0].raw_close, 105)

    def test_raw_and_adjusted_kis_candles_must_have_matching_dates(self):
        with self.assertRaisesRegex(ValueError, "different trading dates"):
            daily_candles_from_kis(
                "423160",
                [{"stck_bsop_date": "20261120"}],
                [{"stck_bsop_date": "20261121"}],
            )

    def test_data_quality_gate_passes_only_complete_valid_inputs(self):
        """모든 필수 데이터가 유효하면 게이트가 통과하는지 확인합니다."""
        trade_dates = self.make_trading_dates()
        result = self.evaluate_gate(expected_trading_dates=trade_dates)

        self.assertTrue(result.passed)
        self.assertEqual(result.failure_codes, ())

    def test_data_quality_gate_rejects_missing_latest_trading_day(self):
        """직전 거래일 종가가 빠진 시계열을 거부하는지 확인합니다."""
        trade_dates = self.make_trading_dates()
        result = self.evaluate_gate(
            expected_trading_dates=trade_dates,
            previous_trading_day=trade_dates[-1],
            candles=self.make_candles(trade_dates[:-1] + [trade_dates[-2]]),
        )

        self.assertFalse(result.passed)
        self.assertIn("INVALID_DAILY_HISTORY", result.failure_codes)

    def test_data_quality_gate_rejects_incomplete_expected_trading_dates(self):
        """예상 거래일 집합과 일봉 날짜가 다르면 거부하는지 확인합니다."""
        trade_dates = self.make_trading_dates()
        candles = self.make_candles(trade_dates)
        candles[100] = DailyCandle(
            ticker="423160",
            trade_date=trade_dates[99],
            adjusted_close=100.0,
            volume=100,
            raw_open=100.0,
            raw_high=101.0,
            raw_low=99.0,
            raw_close=100.0,
        )

        result = self.evaluate_gate(
            expected_trading_dates=trade_dates,
            candles=candles,
        )

        self.assertIn("INVALID_DAILY_HISTORY", result.failure_codes)

    def test_data_quality_gate_rejects_stale_market_and_account_data(self):
        """오래된 시세와 계좌 데이터가 차단되는지 확인합니다."""
        result = self.evaluate_gate(
            received_at=self.now - timedelta(seconds=61),
            account_data_fresh=False,
        )

        self.assertIn("STALE_OR_INVALID_QUOTE", result.failure_codes)
        self.assertIn("STALE_OR_INVALID_NAV", result.failure_codes)
        self.assertIn("STALE_ACCOUNT_DATA", result.failure_codes)

    def test_data_quality_gate_rejects_unverified_actions_and_raw_adjusted_mismatch(self):
        """가격 기준 불일치와 미검증 기업행위가 차단되는지 확인합니다."""
        result = self.evaluate_gate(
            raw_adjusted_history_matches=False,
            corporate_actions_verified=False,
        )

        self.assertIn("RAW_ADJUSTED_HISTORY_MISMATCH", result.failure_codes)
        self.assertIn("UNVERIFIED_CORPORATE_ACTION", result.failure_codes)

    def test_data_quality_gate_rejects_account_order_flow_and_calendar_failures(self):
        """계좌·주문·현금흐름·캘린더 실패가 차단되는지 확인합니다."""
        result = self.evaluate_gate(
            account_matched=False,
            order_statuses=["PENDING_CONFIRMATION"],
            cash_flow_classifications=["UNCLASSIFIED_CASH_FLOW"],
            calendar_matches=False,
        )

        self.assertIn("ACCOUNT_MISMATCH", result.failure_codes)
        self.assertIn("UNRESOLVED_ORDER", result.failure_codes)
        self.assertIn("UNCLASSIFIED_CASH_FLOW", result.failure_codes)
        self.assertIn("CALENDAR_MISMATCH", result.failure_codes)

    def test_data_quality_gate_rejects_unknown_order_and_cash_flow_states(self):
        """알 수 없는 상태값도 안전하게 거부하는지 확인합니다."""
        result = self.evaluate_gate(
            order_statuses=["UNKNOWN"],
            cash_flow_classifications=["UNKNOWN"],
        )

        self.assertIn("UNKNOWN_ORDER_STATUS", result.failure_codes)
        self.assertIn(
            "UNKNOWN_CASH_FLOW_CLASSIFICATION",
            result.failure_codes,
        )

    def make_trading_dates(self):
        """테스트용 200개 거래일 목록을 생성합니다."""
        trade_dates = []
        current_date = date(2026, 1, 1)
        while len(trade_dates) < 200:
            if current_date.weekday() < 5:
                trade_dates.append(current_date.isoformat())
            current_date += timedelta(days=1)
        return trade_dates

    def make_candles(self, trade_dates):
        """테스트용 유효 원주가·수정주가 일봉을 생성합니다."""
        return [
            DailyCandle(
                ticker="423160",
                trade_date=trade_date,
                adjusted_close=100.0,
                volume=100,
                raw_open=100.0,
                raw_high=101.0,
                raw_low=99.0,
                raw_close=100.0,
            )
            for trade_date in trade_dates
        ]

    def evaluate_gate(
        self,
        *,
        expected_trading_dates=None,
        candles=None,
        received_at=None,
        raw_adjusted_history_matches=True,
        corporate_actions_verified=True,
        account_data_fresh=True,
        account_matched=True,
        order_statuses=(),
        cash_flow_classifications=("CONFIRMED",),
        calendar_matches=True,
        previous_trading_day=None,
    ):
        """정상 기본값에 필요한 실패 조건만 덮어써 게이트를 평가합니다."""
        trade_dates = (
            self.make_trading_dates()
            if expected_trading_dates is None
            else expected_trading_dates
        )
        if previous_trading_day is None:
            previous_trading_day = trade_dates[-1]
        if candles is None:
            candles = self.make_candles(trade_dates)
        timestamp = received_at or self.now
        snapshot = IntradaySnapshot(
            ticker="423160",
            bid=10000,
            ask=10010,
            current_price=10005,
            nav=10005.0,
            observed_at=timestamp,
            received_at=timestamp,
        )
        return DataQualityGate().evaluate(
            now=self.now,
            snapshot=snapshot,
            region="KR",
            candles=candles,
            previous_trading_day=previous_trading_day,
            expected_trading_dates=trade_dates,
            raw_adjusted_history_matches=raw_adjusted_history_matches,
            corporate_actions_verified=corporate_actions_verified,
            account_data_fresh=account_data_fresh,
            account_matched=account_matched,
            order_statuses=order_statuses,
            cash_flow_classifications=cash_flow_classifications,
            calendar_matches=calendar_matches,
        )


class CorporateActionTests(unittest.TestCase):
    def test_verified_action_adjusts_price_once(self):
        action = CorporateAction(
            ticker="423160",
            action_type="DISTRIBUTION",
            ex_date=date(2026, 11, 20),
            adjustment_factor=0.9,
            verified=True,
        )

        self.assertAlmostEqual(apply_adjustment_once(100.0, action), 90.0)

    def test_unverified_action_is_rejected(self):
        action = CorporateAction(
            ticker="423160",
            action_type="SPLIT",
            ex_date=date(2026, 11, 20),
            adjustment_factor=0.5,
            verified=False,
        )

        with self.assertRaisesRegex(ValueError, "verified"):
            apply_adjustment_once(100.0, action)

    def test_combines_verified_actions_without_manual_double_adjustment(self):
        actions = [
            CorporateAction(
                ticker="423160",
                action_type="DISTRIBUTION",
                ex_date=date(2026, 11, 20),
                adjustment_factor=0.9,
                verified=True,
            ),
            CorporateAction(
                ticker="423160",
                action_type="SPLIT",
                ex_date=date(2026, 11, 21),
                adjustment_factor=0.5,
                verified=True,
            ),
        ]

        self.assertAlmostEqual(
            adjustment_factor_for_verified_actions(actions),
            0.45,
        )

    def test_reverse_split_factor_greater_than_one_is_supported(self):
        action = CorporateAction(
            ticker="423160",
            action_type="REVERSE_SPLIT",
            ex_date=date(2026, 11, 20),
            adjustment_factor=2.0,
            verified=True,
        )

        self.assertEqual(apply_adjustment_once(100.0, action), 200.0)

    def test_unverified_action_fails_closed_when_combining_factors(self):
        action = CorporateAction(
            ticker="423160",
            action_type="SPLIT",
            ex_date=date(2026, 11, 20),
            adjustment_factor=0.5,
            verified=False,
        )

        with self.assertRaisesRegex(ValueError, "must be verified"):
            adjustment_factor_for_verified_actions([action])


if __name__ == "__main__":
    unittest.main()
