import unittest
from datetime import datetime, timedelta, timezone

from src.performance import (
    CashFlowClassificationStatus,
    CashFlowType,
    TWRQualityStatus,
    apply_cash_flow,
    apply_external_cash_flow,
    classify_cash_flow,
    initialize_flow_adjusted_performance,
    update_flow_adjusted_performance,
)


UTC = timezone.utc


class FlowAdjustedPerformanceTests(unittest.TestCase):
    """단위화 수익률과 입출금·분배금 분류를 검사합니다."""

    def setUp(self):
        """시간이 고정된 시작 상태를 만들기 위한 기준 시각을 둡니다."""
        self.started_at = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)

    def initialize(self, equity=10_000_000):
        """테스트용 최초 계좌 평가액으로 TWR 상태를 시작합니다."""
        return initialize_flow_adjusted_performance(
            starting_equity=equity,
            as_of=self.started_at,
        )

    def test_initial_zero_equity_defers_performance_initialization(self):
        self.assertIsNone(self.initialize(equity=0))

    def test_deposit_does_not_change_unit_nav_or_fake_a_return(self):
        state = self.initialize()
        state = apply_external_cash_flow(
            state,
            cash_flow=1_000_000,
            flow_type=CashFlowType.DEPOSIT,
            occurred_at=self.started_at + timedelta(hours=1),
        )
        self.assertEqual(state.unit_nav, 100.0)
        self.assertEqual(state.units, 110_000)

        updated = update_flow_adjusted_performance(
            state,
            total_equity=11_000_000,
            as_of=self.started_at + timedelta(hours=2),
        )

        self.assertEqual(updated.unit_nav, 100.0)
        self.assertEqual(updated.twr_cumulative, 0.0)
        self.assertEqual(updated.hwm, 100.0)

    def test_withdrawal_reduces_units_without_changing_unit_nav(self):
        state = apply_external_cash_flow(
            self.initialize(),
            cash_flow=-1_000_000,
            flow_type=CashFlowType.WITHDRAWAL,
            occurred_at=self.started_at + timedelta(hours=1),
        )
        updated = update_flow_adjusted_performance(
            state,
            total_equity=9_000_000,
            as_of=self.started_at + timedelta(hours=2),
        )

        self.assertEqual(state.units, 90_000)
        self.assertEqual(updated.unit_nav, 100.0)
        self.assertEqual(updated.twr_cumulative, 0.0)

    def test_hwm_and_drawdown_remain_neutral_to_external_deposits(self):
        state = update_flow_adjusted_performance(
            self.initialize(),
            total_equity=12_000_000,
            as_of=self.started_at + timedelta(hours=1),
        )
        self.assertEqual(state.hwm, 120.0)

        state = apply_external_cash_flow(
            state,
            cash_flow=1_000_000,
            flow_type=CashFlowType.DEPOSIT,
            occurred_at=self.started_at + timedelta(hours=2),
        )
        updated = update_flow_adjusted_performance(
            state,
            total_equity=13_000_000,
            as_of=self.started_at + timedelta(hours=3),
        )
        self.assertAlmostEqual(updated.unit_nav, 120.0)
        self.assertEqual(updated.hwm, 120.0)

        drawdown = update_flow_adjusted_performance(
            updated,
            total_equity=12_000_000,
            as_of=self.started_at + timedelta(hours=4),
        )
        self.assertAlmostEqual(drawdown.drawdown, -1 / 13)
        self.assertEqual(drawdown.hwm, 120.0)

    def test_distribution_is_return_not_an_external_unit_adjustment(self):
        state = apply_cash_flow(
            self.initialize(),
            cash_flow=1_000_000,
            flow_type=CashFlowType.DISTRIBUTION,
            occurred_at=self.started_at + timedelta(hours=1),
        )
        updated = update_flow_adjusted_performance(
            state,
            total_equity=11_000_000,
            as_of=self.started_at + timedelta(hours=2),
        )

        self.assertEqual(state.units, 100_000)
        self.assertEqual(updated.unit_nav, 110.0)
        self.assertAlmostEqual(updated.twr_cumulative, 0.10)

    def test_unclassified_cash_flow_marks_quality_invalid_and_preserves_hwm(self):
        initial = self.initialize()
        state = apply_cash_flow(
            initial,
            cash_flow=50_000,
            flow_type="UNKNOWN_BROKER_FLOW",
            occurred_at=self.started_at + timedelta(hours=1),
        )

        self.assertEqual(state.twr_quality_status, TWRQualityStatus.DATA_INVALID)
        self.assertEqual(state.hwm, initial.hwm)
        with self.assertRaisesRegex(ValueError, "Invalid cash-flow data"):
            update_flow_adjusted_performance(
                state,
                total_equity=10_050_000,
                as_of=self.started_at + timedelta(hours=2),
            )

    def test_missing_amount_or_occurrence_time_fails_closed(self):
        missing_amount = apply_cash_flow(
            self.initialize(),
            cash_flow=None,
            flow_type=CashFlowType.DEPOSIT,
            occurred_at=self.started_at + timedelta(hours=1),
        )
        missing_time = apply_cash_flow(
            self.initialize(),
            cash_flow=1_000_000,
            flow_type=CashFlowType.DEPOSIT,
            occurred_at=None,
        )

        self.assertEqual(
            missing_amount.twr_quality_status,
            TWRQualityStatus.DATA_INVALID,
        )
        self.assertEqual(
            missing_time.twr_quality_status,
            TWRQualityStatus.DATA_INVALID,
        )

    def test_out_of_order_cash_flow_invalidates_performance_quality(self):
        state = apply_cash_flow(
            self.initialize(),
            cash_flow=1_000_000,
            flow_type=CashFlowType.DEPOSIT,
            occurred_at=self.started_at - timedelta(seconds=1),
        )

        self.assertEqual(state.twr_quality_status, TWRQualityStatus.DATA_INVALID)

    def test_classifies_only_known_flow_types_as_confirmed(self):
        self.assertEqual(
            classify_cash_flow(CashFlowType.WITHDRAWAL).classification_status,
            CashFlowClassificationStatus.CONFIRMED,
        )
        self.assertEqual(
            classify_cash_flow("UNKNOWN").flow_type,
            CashFlowType.UNCLASSIFIED,
        )
        self.assertEqual(
            classify_cash_flow("UNKNOWN").classification_status,
            CashFlowClassificationStatus.UNCLASSIFIED_CASH_FLOW,
        )


if __name__ == "__main__":
    unittest.main()
