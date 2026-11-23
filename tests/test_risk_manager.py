import math
import unittest

from src.risk_manager import (
    DrawdownMode,
    OperatingMode,
    RiskAction,
    RiskFlags,
    allows_new_order,
    allows_parking_buy,
    allows_reduce_only_sell,
    allows_risk_asset_buy,
    allows_smart_dca_buy,
    calculate_drawdown,
    get_drawdown_mode,
    is_action_allowed,
    resolve_operating_mode,
    should_trailing_sell,
    update_adjusted_peak_price,
)


class RiskManagerTests(unittest.TestCase):
    """손절 경계, 운영 모드 우선순위와 단일 주문 권한 정책을 확인합니다."""

    def test_normal_trailing_stop_is_ten_percent_inclusive(self):
        self.assertTrue(should_trailing_sell(90.0, 100.0, training_mode=False))
        self.assertFalse(should_trailing_sell(90.01, 100.0, training_mode=False))

    def test_training_trailing_stop_is_seven_percent_inclusive(self):
        self.assertTrue(should_trailing_sell(93.0, 100.0, training_mode=True))
        self.assertFalse(should_trailing_sell(93.01, 100.0, training_mode=True))

    def test_adjusted_price_and_equity_inputs_must_be_finite(self):
        with self.assertRaisesRegex(ValueError, "finite positive"):
            should_trailing_sell(math.inf, 100.0)
        with self.assertRaisesRegex(ValueError, "finite"):
            calculate_drawdown(math.nan, 1_000_000)
        with self.assertRaisesRegex(ValueError, "between -1 and 0"):
            get_drawdown_mode(0.01)

    def test_adjusted_peak_price_only_moves_up(self):
        self.assertEqual(update_adjusted_peak_price(100.0, 95.0), 100.0)
        self.assertEqual(update_adjusted_peak_price(100.0, 105.0), 105.0)

    def test_drawdown_boundaries_use_inclusive_thresholds(self):
        self.assertEqual(get_drawdown_mode(-0.069), DrawdownMode.NORMAL)
        self.assertEqual(get_drawdown_mode(-0.07), DrawdownMode.CAUTION)
        self.assertEqual(get_drawdown_mode(-0.10), DrawdownMode.WARNING)
        self.assertEqual(get_drawdown_mode(-0.15), DrawdownMode.CRITICAL)
        self.assertEqual(
            get_drawdown_mode(
                -0.08,
                caution_threshold=-0.06,
                warning_threshold=-0.09,
                critical_threshold=-0.14,
            ),
            DrawdownMode.CAUTION,
        )
        self.assertAlmostEqual(
            calculate_drawdown(850_000, 1_000_000),
            -0.15,
        )

    def test_safe_halt_has_highest_priority_and_only_allows_recovery_actions(self):
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
        self.assertFalse(allows_parking_buy(flags))
        self.assertFalse(
            allows_reduce_only_sell(
                flags,
                hard_stop_triggered=True,
                safety_gates_passed=True,
            )
        )
        self.assertTrue(is_action_allowed(flags, RiskAction.QUERY))
        self.assertTrue(is_action_allowed(flags, RiskAction.RECONCILE))
        self.assertTrue(is_action_allowed(flags, RiskAction.CANCEL))

    def test_invalid_performance_data_automatically_resolves_to_safe_halt(self):
        flags = RiskFlags(data_invalid=True)

        self.assertEqual(resolve_operating_mode(flags), OperatingMode.SAFE_HALT)
        self.assertFalse(allows_new_order(flags))
        self.assertFalse(allows_reduce_only_sell(flags))

    def test_training_allows_parking_only(self):
        flags = RiskFlags(training_mode=True)

        self.assertEqual(resolve_operating_mode(flags), OperatingMode.TRAINING)
        self.assertFalse(allows_risk_asset_buy(flags))
        self.assertTrue(allows_parking_buy(flags))

    def test_drawdown_caution_warning_and_critical_control_buys(self):
        caution = RiskFlags(drawdown_mode=DrawdownMode.CAUTION)
        warning = RiskFlags(drawdown_mode=DrawdownMode.WARNING)
        critical = RiskFlags(drawdown_mode=DrawdownMode.CRITICAL)

        self.assertTrue(allows_risk_asset_buy(caution))
        self.assertFalse(allows_smart_dca_buy(caution))
        self.assertTrue(allows_parking_buy(caution))
        self.assertFalse(allows_risk_asset_buy(warning))
        self.assertFalse(allows_smart_dca_buy(warning))
        self.assertTrue(allows_parking_buy(warning))
        self.assertEqual(resolve_operating_mode(critical), OperatingMode.EMERGENCY)
        self.assertFalse(allows_parking_buy(critical))

    def test_pause_and_emergency_require_confirmed_hard_stop_gates_for_sell(self):
        for flags in (RiskFlags(is_paused=True), RiskFlags(emergency_mode=True)):
            self.assertFalse(allows_reduce_only_sell(flags))
            self.assertFalse(
                allows_reduce_only_sell(flags, hard_stop_triggered=True)
            )
            self.assertFalse(
                allows_reduce_only_sell(flags, safety_gates_passed=True)
            )
            self.assertTrue(
                allows_reduce_only_sell(
                    flags,
                    hard_stop_triggered=True,
                    safety_gates_passed=True,
                )
            )
            self.assertTrue(is_action_allowed(flags, RiskAction.CANCEL))
            self.assertFalse(allows_new_order(flags))

    def test_training_and_normal_reduce_only_sales_require_hard_stop(self):
        for flags in (RiskFlags(), RiskFlags(training_mode=True)):
            self.assertFalse(allows_reduce_only_sell(flags))
            self.assertTrue(
                allows_reduce_only_sell(
                    flags,
                    hard_stop_triggered=True,
                    safety_gates_passed=True,
                )
            )

    def test_emergency_overrides_pause_and_training(self):
        flags = RiskFlags(
            emergency_mode=True,
            is_paused=True,
            training_mode=True,
        )

        self.assertEqual(
            resolve_operating_mode(flags),
            OperatingMode.EMERGENCY,
        )


if __name__ == "__main__":
    unittest.main()
