import math
import unittest

from src.quant_engine import (
    CandidateProfile,
    RankedCandidate,
    assess_risk_candidate,
    calculate_momentum_score,
    is_above_sma120,
    passes_momentum_trend_filter,
    rank_candidates,
    validate_daily_history,
)


def make_prices(daily_returns):
    prices = [100.0]
    for daily_return in daily_returns:
        prices.append(prices[-1] * math.exp(daily_return))
    return prices


class CalculateMomentumScoreTests(unittest.TestCase):
    def test_equal_asset_and_parking_returns_have_zero_score(self):
        returns = [0.002 + (0.01 if day % 2 else -0.01) for day in range(120)]
        prices = make_prices(returns)

        score = calculate_momentum_score(prices, prices)

        self.assertAlmostEqual(score, 0.0, places=12)

    def test_positive_excess_returns_have_positive_score(self):
        asset_returns = [
            0.003 + (0.01 if day % 2 else -0.01) for day in range(120)
        ]
        parking_returns = [
            0.001 + (0.004 if day % 2 else -0.004) for day in range(120)
        ]

        score = calculate_momentum_score(
            make_prices(asset_returns), make_prices(parking_returns)
        )

        self.assertAlmostEqual(score, 2.675418217588054, places=10)

    def test_volatility_floor_is_used_instead_of_excluding_low_volatility(self):
        asset_prices = make_prices([0.001] * 120)
        parking_prices = make_prices([0.001] * 120)

        self.assertAlmostEqual(
            calculate_momentum_score(asset_prices, parking_prices),
            0.0,
            places=12,
        )

    def test_zero_excess_volatility_uses_configured_denominator_floor(self):
        asset_prices = make_prices([0.003] * 120)
        parking_prices = make_prices([0.001] * 120)

        score = calculate_momentum_score(
            asset_prices,
            parking_prices,
            min_daily_excess_volatility=0.01,
        )

        self.assertGreater(score, 0)
        self.assertTrue(math.isfinite(score))

    def test_rejects_mismatched_history_lengths(self):
        prices = make_prices([0.01 if day % 2 else -0.01 for day in range(120)])

        with self.assertRaises(ValueError):
            calculate_momentum_score(prices, prices[:-1])

    def test_rejects_insufficient_history(self):
        prices = make_prices([0.01 if day % 2 else -0.01 for day in range(119)])

        with self.assertRaises(ValueError):
            calculate_momentum_score(prices, prices)

    def test_rejects_non_positive_prices(self):
        prices = make_prices([0.01 if day % 2 else -0.01 for day in range(120)])
        prices[0] = 0

        with self.assertRaises(ValueError):
            calculate_momentum_score(prices, prices)


class IsAboveSma120Tests(unittest.TestCase):
    def test_returns_true_when_current_price_is_above_latest_120_day_average(self):
        closes = [1000.0] + [100.0] * 120

        self.assertTrue(is_above_sma120(101.0, closes))

    def test_returns_false_when_current_price_equals_average(self):
        closes = [100.0] * 120

        self.assertFalse(is_above_sma120(100.0, closes))

    def test_rejects_insufficient_history(self):
        with self.assertRaises(ValueError):
            is_above_sma120(101.0, [100.0] * 119)

    def test_rejects_invalid_current_price(self):
        with self.assertRaises(ValueError):
            is_above_sma120(0, [100.0] * 120)


class MomentumTrendFilterTests(unittest.TestCase):
    def test_passes_positive_score_above_sma120(self):
        self.assertTrue(passes_momentum_trend_filter(0.1, 101.0, [100.0] * 120))

    def test_rejects_non_positive_score(self):
        self.assertFalse(passes_momentum_trend_filter(0.0, 101.0, [100.0] * 120))
        self.assertFalse(passes_momentum_trend_filter(-0.1, 101.0, [100.0] * 120))

    def test_rejects_score_excluded_by_volatility_floor(self):
        self.assertFalse(passes_momentum_trend_filter(None, 101.0, [100.0] * 120))

    def test_rejects_price_at_or_below_sma120(self):
        self.assertFalse(passes_momentum_trend_filter(0.1, 100.0, [100.0] * 120))
        self.assertFalse(passes_momentum_trend_filter(0.1, 99.0, [100.0] * 120))

    def test_rejects_non_finite_score(self):
        with self.assertRaises(ValueError):
            passes_momentum_trend_filter(math.inf, 101.0, [100.0] * 120)


class RankCandidatesTests(unittest.TestCase):
    def test_ranks_by_descending_positive_score(self):
        candidates = [
            RankedCandidate("A", 0.2, 0.03),
            RankedCandidate("B", -0.1, 0.01),
            RankedCandidate("C", 0.8, 0.05),
        ]

        ranked = rank_candidates(candidates)

        self.assertEqual([candidate.ticker for candidate in ranked], ["C", "A"])

    def test_ties_use_er120_turnover_and_ticker(self):
        candidates = [
            RankedCandidate("C", 0.5, 0.0, er120=0.1, avg_turnover_20d=100),
            RankedCandidate("A", 0.5, 0.0, er120=0.1, avg_turnover_20d=200),
            RankedCandidate("B", 0.5, 0.0, er120=0.2, avg_turnover_20d=50),
        ]

        ranked = rank_candidates(candidates)

        self.assertEqual(
            [candidate.ticker for candidate in ranked],
            ["B", "A", "C"],
        )

    def test_rejects_non_finite_tie_breaker_values(self):
        with self.assertRaisesRegex(ValueError, "ER120"):
            rank_candidates([RankedCandidate("A", 0.1, 0, er120=math.nan)])

        with self.assertRaisesRegex(ValueError, "turnover"):
            rank_candidates(
                [RankedCandidate("A", 0.1, 0, avg_turnover_20d=math.inf)]
            )


class CandidateEligibilityTests(unittest.TestCase):
    """명세서의 6단계 후보 필터 결과를 검증합니다."""

    def make_profile(self, **overrides):
        """테스트 기본 조건을 만족하는 가짜 후보 정보를 만듭니다."""
        values = {
            "ticker": "423161",
            "category": "RISK_ASSET",
            "status": "ACTIVE",
            "data_status": "VALID",
            "product_type": "SPOT_ETF",
            "is_leveraged": False,
            "is_inverse": False,
            "is_futures": False,
            "is_etn": False,
            "adjusted_closes": make_prices([0.003] * 199),
            "parking_adjusted_closes": make_prices([0.001] * 199),
            "avg_turnover_20d": 6_000_000_000,
            "estimated_order_amount": 10_000_000,
            "currently_held": False,
            "blocked_until_next_rotation": False,
            "classification_verified": True,
            "leverage_multiplier": 1.0,
            "replication_method": "PHYSICAL",
        }
        values.update(overrides)
        return CandidateProfile(**values)

    def test_eligible_candidate_passes_all_six_filters(self):
        result = assess_risk_candidate(self.make_profile())

        self.assertIsNotNone(result.candidate)
        self.assertTrue(result.eligible_for_new_entry)
        self.assertIsNone(result.failed_filter)

    def test_rejects_inactive_or_derivative_products_at_first_filter(self):
        inactive = assess_risk_candidate(self.make_profile(status="REMOVED"))
        leveraged = assess_risk_candidate(self.make_profile(is_leveraged=True))

        self.assertEqual(inactive.failed_filter, 1)
        self.assertEqual(inactive.rejection_code, "INACTIVE")
        self.assertEqual(leveraged.rejection_code, "INELIGIBLE_PRODUCT")

    def test_rejects_unverified_classification_and_non_one_multiplier(self):
        unverified = assess_risk_candidate(
            self.make_profile(classification_verified=False)
        )
        leveraged = assess_risk_candidate(
            self.make_profile(leverage_multiplier=2.0)
        )

        self.assertEqual(unverified.rejection_code, "UNVERIFIED_CLASSIFICATION")
        self.assertEqual(unverified.failed_filter, 1)
        self.assertEqual(leveraged.rejection_code, "INELIGIBLE_PRODUCT")

    def test_rejects_insufficient_history_and_invalid_daily_returns(self):
        insufficient = assess_risk_candidate(
            self.make_profile(
                adjusted_closes=[100.0] * 199,
                parking_adjusted_closes=[100.0] * 199,
            )
        )
        self.assertEqual(insufficient.rejection_code, "INSUFFICIENT_HISTORY")
        with self.assertRaisesRegex(ValueError, "30% data-quality limit"):
            assess_risk_candidate(
                self.make_profile(
                    adjusted_closes=[100.0] * 199 + [131.0],
                    parking_adjusted_closes=[100.0] * 200,
                )
            )
        with self.assertRaisesRegex(ValueError, "30% data-quality limit"):
            assess_risk_candidate(
                self.make_profile(
                    parking_adjusted_closes=[100.0] * 199 + [131.0]
                )
            )

    def test_low_liquidity_blocks_entry_without_forcing_held_candidate_out(self):
        new_entry = assess_risk_candidate(
            self.make_profile(avg_turnover_20d=4_000_000_000)
        )
        held_position = assess_risk_candidate(
            self.make_profile(
                avg_turnover_20d=4_000_000_000,
                currently_held=True,
            )
        )

        self.assertIsNone(new_entry.candidate)
        self.assertEqual(new_entry.rejection_code, "LOW_LIQUIDITY")
        self.assertIsNotNone(held_position.candidate)
        self.assertFalse(held_position.eligible_for_new_entry)

    def test_order_participation_limit_is_inclusive_at_one_percent(self):
        at_limit = assess_risk_candidate(
            self.make_profile(estimated_order_amount=60_000_000)
        )
        over_limit = assess_risk_candidate(
            self.make_profile(estimated_order_amount=60_000_001)
        )

        self.assertTrue(at_limit.eligible_for_new_entry)
        self.assertEqual(over_limit.rejection_code, "LOW_LIQUIDITY")

    def test_blocked_position_fails_final_filter(self):
        result = assess_risk_candidate(
            self.make_profile(blocked_until_next_rotation=True)
        )

        self.assertEqual(result.failed_filter, 6)
        self.assertEqual(result.rejection_code, "BLOCKED_UNTIL_NEXT_ROTATION")


class ValidateDailyHistoryTests(unittest.TestCase):
    def test_accepts_200_valid_closes_and_exact_30_percent_change(self):
        closes = [100.0] * 199 + [130.0]

        self.assertIsNone(validate_daily_history(closes))

    def test_rejects_less_than_200_closes(self):
        with self.assertRaises(ValueError):
            validate_daily_history([100.0] * 199)

    def test_rejects_return_above_30_percent(self):
        closes = [100.0] * 199 + [130.01]

        with self.assertRaises(ValueError):
            validate_daily_history(closes)

    def test_rejects_return_below_negative_30_percent(self):
        closes = [100.0] * 199 + [69.99]

        with self.assertRaises(ValueError):
            validate_daily_history(closes)

    def test_rejects_non_finite_or_non_positive_prices(self):
        with self.assertRaises(ValueError):
            validate_daily_history([100.0] * 199 + [math.nan])
        with self.assertRaises(ValueError):
            validate_daily_history([100.0] * 199 + [0.0])


if __name__ == "__main__":
    unittest.main()
