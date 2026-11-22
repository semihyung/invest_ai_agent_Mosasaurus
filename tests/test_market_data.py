import unittest
from datetime import date, datetime, timedelta, timezone

from src.corporate_actions import (
    CorporateAction,
    adjustment_factor_for_verified_actions,
    apply_adjustment_once,
)
from src.market_data import (
    FreshnessStatus,
    IntradaySnapshot,
    freshness_status,
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


if __name__ == "__main__":
    unittest.main()
