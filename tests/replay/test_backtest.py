import unittest

from backtest.engine import BacktestRun
from backtest.run_backtest import (
    calculate_cagr,
    calculate_mdd,
    calculate_turnover,
)
from backtest.walk_forward import walk_forward_windows


class BacktestTests(unittest.TestCase):
    def test_engine_metrics_are_the_shared_functions(self):
        """BacktestRun 의 CAGR·MDD·회전율은 run_backtest 의 단일 구현을 그대로 쓴다(감사 9번)."""
        equity = (100.0, 110.0, 99.0, 120.0)
        run = BacktestRun(dates=("a", "b", "c", "d"), equity=equity, trades=3, traded_notional=300.0,
                          fees_paid=0.0, slippage_cost=0.0, stops=0, rotations=0)
        years = 3 / 252
        self.assertAlmostEqual(run.cagr, calculate_cagr(100.0, 120.0, years))
        self.assertAlmostEqual(run.mdd, calculate_mdd(equity))
        self.assertAlmostEqual(run.mdd, 99.0 / 110.0 - 1)
        self.assertAlmostEqual(run.turnover, calculate_turnover(300.0, sum(equity) / 4) / years)

    def test_metric_edge_cases(self):
        self.assertAlmostEqual(calculate_mdd([100, 90, 99, 95]), -0.1)
        self.assertAlmostEqual(calculate_turnover(500, 1000), 0.5)
        self.assertEqual(calculate_cagr(100.0, 0.0, 1.0), -1.0)
        self.assertEqual(calculate_mdd([100.0, 0.0]), -1.0)  # 전액 손실 곡선도 계산된다
        with self.assertRaises(ValueError):
            calculate_mdd([100.0, -1.0])

    def test_walk_forward_creates_training_and_test_windows(self):
        dates = [str(index) for index in range(1008)]

        windows = walk_forward_windows(
            dates,
            training_sessions=756,
            test_sessions=252,
        )

        self.assertEqual(len(windows), 1)
        self.assertEqual(len(windows[0][0]), 756)
        self.assertEqual(len(windows[0][1]), 252)


if __name__ == "__main__":
    unittest.main()
