import unittest
from datetime import date, datetime, timedelta, timezone

from src.corporate_actions import (
    CorporateAction,
    adjustment_factor_for_verified_actions,
    apply_adjustment_once,
)
from src.market_data import (
    daily_candles_from_kis,
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
