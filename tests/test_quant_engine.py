import math
import unittest

from src.quant_engine import (
    RankedCandidate,
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

        self.assertIsNone(score)

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

    def test_low_volatility_excludes_candidate(self):
        asset_prices = make_prices([0.001] * 120)
        parking_prices = make_prices([0.001] * 120)

        self.assertIsNone(calculate_momentum_score(asset_prices, parking_prices))

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
