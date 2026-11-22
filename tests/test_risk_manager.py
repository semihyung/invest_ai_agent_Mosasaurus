import unittest

from src.risk_manager import (
    DrawdownMode,
    OperatingMode,
    RiskFlags,
    allows_risk_asset_buy,
    calculate_drawdown,
    get_drawdown_mode,
    resolve_operating_mode,
    should_trailing_sell,
)


class RiskManagerTests(unittest.TestCase):
    def test_normal_trailing_stop_is_ten_percent(self):
        self.assertTrue(
            should_trailing_sell(89.99, 100.0, training_mode=False)
        )
        self.assertFalse(
            should_trailing_sell(90.01, 100.0, training_mode=False)
        )

    def test_training_trailing_stop_is_seven_percent(self):
        self.assertTrue(
            should_trailing_sell(92.99, 100.0, training_mode=True)
        )
        self.assertFalse(
            should_trailing_sell(93.01, 100.0, training_mode=True)
        )

    def test_drawdown_modes(self):
        self.assertEqual(get_drawdown_mode(-0.06), DrawdownMode.NORMAL)
        self.assertEqual(get_drawdown_mode(-0.08), DrawdownMode.CAUTION)
        self.assertEqual(get_drawdown_mode(-0.12), DrawdownMode.WARNING)
        self.assertEqual(get_drawdown_mode(-0.15), DrawdownMode.CRITICAL)
        self.assertAlmostEqual(
            calculate_drawdown(850_000, 1_000_000),
            -0.15,
        )

    def test_safe_halt_has_highest_operating_priority(self):
        flags = RiskFlags(
            is_safe_halt=True,
            emergency_mode=True,
            is_paused=True,
            training_mode=True,
        )

        self.assertEqual(
            resolve_operating_mode(flags),
            OperatingMode.SAFE_HALT,
        )
        self.assertFalse(allows_risk_asset_buy(flags))

    def test_training_allows_only_parking_by_risk_asset_buy_rule(self):
        flags = RiskFlags(training_mode=True)

        self.assertEqual(
            resolve_operating_mode(flags),
            OperatingMode.TRAINING,
        )
        self.assertFalse(allows_risk_asset_buy(flags))


if __name__ == "__main__":
    unittest.main()
