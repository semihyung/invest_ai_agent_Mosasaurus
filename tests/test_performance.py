import unittest

from src.performance import (
    apply_external_cash_flow,
    initialize_flow_adjusted_performance,
    update_flow_adjusted_performance,
)


class FlowAdjustedPerformanceTests(unittest.TestCase):
    def test_deposit_does_not_change_unit_nav(self):
        state = initialize_flow_adjusted_performance(starting_equity=10_000_000)
        state = apply_external_cash_flow(state, cash_flow=1_000_000)

        self.assertEqual(state.unit_nav, 100.0)
        self.assertEqual(state.units, 110_000)

    def test_hwm_and_drawdown_are_flow_neutral(self):
        state = initialize_flow_adjusted_performance(starting_equity=10_000_000)
        state = apply_external_cash_flow(state, cash_flow=1_000_000)
        state = update_flow_adjusted_performance(state, total_equity=9_900_000)

        self.assertAlmostEqual(state.unit_nav, 90.0)
        self.assertAlmostEqual(state.twr_cumulative, -0.10)
        self.assertAlmostEqual(state.drawdown, -0.10)
        self.assertEqual(state.hwm, 100.0)


if __name__ == "__main__":
    unittest.main()
