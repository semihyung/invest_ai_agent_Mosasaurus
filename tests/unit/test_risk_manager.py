import math
import unittest

from src.risk_manager import (
    DrawdownMode,
    OperatingMode,
    RiskFlags,
    calculate_drawdown,
    get_drawdown_mode,
    resolve_operating_mode,
    should_trailing_sell,
)
from src.order_policy import OrderKind, is_allowed


def _allowed(flags: RiskFlags, kind: OrderKind) -> bool:
    """플래그에서 모드를 정한 뒤 0.6절 진리표(`order_policy.is_allowed`) 하나로 판정한다."""
    return is_allowed(
        resolve_operating_mode(flags), flags.drawdown_mode, kind,
        halt_reasons=flags.halt_reasons, protective_exit_enabled=flags.protective_exit_enabled)


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

    def test_safe_halt_has_highest_priority_and_only_allows_cancel(self):
        flags = RiskFlags(is_safe_halt=True, emergency_mode=True, is_paused=True, training_mode=True)

        self.assertEqual(resolve_operating_mode(flags), OperatingMode.SAFE_HALT)
        for kind in OrderKind:
            self.assertEqual(_allowed(flags, kind), kind is OrderKind.CANCEL, kind)

    def test_invalid_performance_data_automatically_resolves_to_safe_halt(self):
        flags = RiskFlags(data_invalid=True)

        self.assertEqual(resolve_operating_mode(flags), OperatingMode.SAFE_HALT)
        self.assertFalse(_allowed(flags, OrderKind.RISK_BUY))
        self.assertFalse(_allowed(flags, OrderKind.REDUCE_ONLY_SELL))

    def test_training_allows_parking_only(self):
        flags = RiskFlags(training_mode=True)

        self.assertEqual(resolve_operating_mode(flags), OperatingMode.TRAINING)
        self.assertFalse(_allowed(flags, OrderKind.RISK_BUY))
        self.assertTrue(_allowed(flags, OrderKind.PARKING_BUY))

    def test_drawdown_caution_warning_and_critical_control_buys(self):
        caution = RiskFlags(drawdown_mode=DrawdownMode.CAUTION)
        warning = RiskFlags(drawdown_mode=DrawdownMode.WARNING)
        critical = RiskFlags(drawdown_mode=DrawdownMode.CRITICAL)

        self.assertTrue(_allowed(caution, OrderKind.RISK_BUY))
        self.assertFalse(_allowed(caution, OrderKind.RISK_TOP_UP_BUY))  # Drift 보충 매수는 CAUTION 부터 금지
        self.assertTrue(_allowed(caution, OrderKind.PARKING_BUY))
        self.assertFalse(_allowed(warning, OrderKind.RISK_BUY))
        self.assertTrue(_allowed(warning, OrderKind.PARKING_BUY))
        self.assertEqual(resolve_operating_mode(critical), OperatingMode.EMERGENCY)
        self.assertFalse(_allowed(critical, OrderKind.PARKING_BUY))

    def test_pause_and_emergency_allow_only_reduce_only_sells_and_cancel(self):
        for flags in (RiskFlags(is_paused=True), RiskFlags(emergency_mode=True)):
            self.assertTrue(_allowed(flags, OrderKind.REDUCE_ONLY_SELL))
            self.assertTrue(_allowed(flags, OrderKind.CANCEL))
            self.assertFalse(_allowed(flags, OrderKind.RISK_BUY))
            self.assertFalse(_allowed(flags, OrderKind.PARKING_BUY))

    def test_training_and_normal_allow_reduce_only_sells(self):
        for flags in (RiskFlags(), RiskFlags(training_mode=True)):
            self.assertTrue(_allowed(flags, OrderKind.REDUCE_ONLY_SELL))

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
