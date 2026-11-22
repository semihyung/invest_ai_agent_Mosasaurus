import tempfile
import unittest
from pathlib import Path

from backtest.data_loader import load_csv_bars
from backtest.run_backtest import (
    calculate_mdd,
    calculate_turnover,
    run_synthetic_backtest,
)
from backtest.walk_forward import walk_forward_windows


class BacktestTests(unittest.TestCase):
    def test_loads_valid_csv_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bars.csv"
            rows = ["trade_date,adjusted_close,volume"]
            rows.extend(
                f"2026-01-{day:02d},100,{day * 100000}"
                for day in range(1, 32)
            )
            # Add 200 valid sessions while preserving the first 31 rows.
            for day in range(32, 201):
                rows.append(f"2026-{day:03d},100,{day * 100000}")
            path.write_text("\n".join(rows), encoding="utf-8")

            bars = load_csv_bars(path)

            self.assertGreaterEqual(len(bars), 200)

    def test_calculates_cagr_mdd_and_turnover(self):
        result = run_synthetic_backtest(
            10_000_000,
            [0.01, -0.05, 0.02, 0.03],
        )

        self.assertLess(result.cagr, 1.0)
        self.assertLessEqual(result.mdd, 0.0)
        self.assertGreater(result.turnover_ratio, 0.0)
        self.assertEqual(result.trades, 4)
        self.assertAlmostEqual(calculate_mdd([100, 90, 99, 95]), -0.1)
        self.assertAlmostEqual(calculate_turnover(500, 1000), 0.5)

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
