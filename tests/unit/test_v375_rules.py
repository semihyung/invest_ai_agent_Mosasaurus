"""명세 v3.7.5 의 순수 규칙(보호성 손절 게이트·오염 제거 단위가격·늦은 분류 재계산·대사 2단계)을 검증합니다."""

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.cash_recon import (
    DiscrepancyDecision,
    buy_wait_for_cash_flow,
    excluded_unclassified_amount,
    hwm_frozen_for_cash_flow,
    judge_discrepancy,
    reconciliation_allowed,
    record_discrepancy,
    unexplained_cash_delta,
)
from src.config import ConfigError, load_config, validate_config
from src.db import initialize_database
from src.order_manager import (
    OrderIntentType,
    RealOrderGateContext,
    validate_order_submission_gate,
)
from src.performance import (
    SnapshotRow,
    initialize_flow_adjusted_performance,
    restate_after_late_flow,
    update_flow_adjusted_performance,
)
from src.risk_manager import (
    RiskFlags,
    resolve_operating_mode,
    protective_exit_permitted,
)
from src.order_policy import OrderKind, is_allowed
from src.safety_flags import active_codes, load_risk_flags, raise_flag

UTC = timezone.utc
T0 = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)


class ProtectiveExitGateTests(unittest.TestCase):
    """SAFE_HALT 중 보호성 손절(D1)이 정확히 허용 원인에서만, 스탑 사유로만 열리는지 봅니다."""

    def flags(self, reasons, **kwargs):
        return RiskFlags(is_safe_halt=True, halt_reasons=frozenset(reasons), **kwargs)

    @staticmethod
    def allowed(flags, kind):
        return is_allowed(resolve_operating_mode(flags), flags.drawdown_mode, kind,
                          halt_reasons=flags.halt_reasons, protective_exit_enabled=flags.protective_exit_enabled)

    def test_only_trailing_stop_is_allowed_under_safe_halt(self):
        flags = self.flags({"ACCOUNT_MISMATCH"})
        self.assertTrue(self.allowed(flags, OrderKind.PROTECTIVE_EXIT_SELL))
        # 일반 위험 축소 매도·신규 매수는 여전히 전면 차단.
        self.assertFalse(self.allowed(flags, OrderKind.REDUCE_ONLY_SELL))
        self.assertFalse(self.allowed(flags, OrderKind.RISK_BUY))

    def test_forbidden_reason_or_disabled_switch_or_invalid_data_blocks_everything(self):
        for reasons in ({"PENDING_CONFIRMATION"}, {"UPS_LOW", "CREDENTIALS_UNAVAILABLE"}, {"DB_INTEGRITY"}):
            self.assertFalse(self.allowed(self.flags(reasons), OrderKind.PROTECTIVE_EXIT_SELL), reasons)
        self.assertFalse(self.allowed(self.flags({"UPS_LOW"}, protective_exit_enabled=False),
                                      OrderKind.PROTECTIVE_EXIT_SELL))
        self.assertFalse(self.allowed(RiskFlags(is_safe_halt=False, data_invalid=True),
                                      OrderKind.PROTECTIVE_EXIT_SELL))
        self.assertFalse(protective_exit_permitted(set()))

    def test_flags_loaded_from_db_carry_active_halt_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = initialize_database(Path(directory) / "t.db")
            try:
                raise_flag(connection, "HALT", "DEPLOY_ROLLBACK", now=T0)
                flags = load_risk_flags(connection)
                self.assertEqual(flags.halt_reasons, frozenset({"DEPLOY_ROLLBACK"}))
                self.assertTrue(self.allowed(flags, OrderKind.PROTECTIVE_EXIT_SELL))
            finally:
                connection.close()

    def test_order_gate_relaxes_only_reconciliation_and_cash_flow_for_protective_exit(self):
        base = dict(
            environment="PAPER", operator_approval=False, reconciliation_ok=False,
            reconciliation_fresh=True, unresolved_order_exists=False,
            unclassified_cash_flow_exists=True, token_valid=True, calendar_valid=True,
            quote_valid=True, adjusted_data_valid=True, backtest_calibrated=False,
            mode_allows_order=True, idempotency_conflict=False,
            intent_type=OrderIntentType.RISK_REDUCTION,
        )
        self.assertFalse(validate_order_submission_gate(RealOrderGateContext(**base)))
        self.assertTrue(validate_order_submission_gate(
            RealOrderGateContext(**base, protective_exit=True)))
        # 보호성 손절이어도 호가 무효·주문 미확정·토큰 무효는 통과시키지 않는다.
        for override in ({"quote_valid": False}, {"unresolved_order_exists": True},
                         {"token_valid": False}, {"reconciliation_fresh": False}):
            self.assertFalse(validate_order_submission_gate(
                RealOrderGateContext(**{**base, **override}, protective_exit=True)), override)


class UnclassifiedUnitPriceTests(unittest.TestCase):
    """미분류 입금이 단위가격·HWM·낙폭을 오염시키지 않고, 늦은 분류가 발생일 기준으로 반영되는지 봅니다."""

    def test_unclassified_deposit_is_excluded_so_no_false_critical(self):
        """감사 보고서 예시: V=3,000,000·U=100 에서 2,000,000 입금이 미분류여도 U 는 100 으로 유지됩니다."""
        state = initialize_flow_adjusted_performance(starting_equity=3_000_000, as_of=T0)
        later = update_flow_adjusted_performance(
            state, total_equity=5_000_000, as_of=T0 + timedelta(days=5),
            freeze=True, unclassified_excluded_amount=2_000_000)
        self.assertAlmostEqual(later.unit_nav, 100.0)
        self.assertAlmostEqual(later.hwm, 100.0)  # HWM 상향 동결 + 오염 제거
        self.assertAlmostEqual(later.drawdown, 0.0)
        # v3.7.4 방식(제외 없음)이면 U=166.67 이 되어 HWM 이 오염된다.
        polluted = update_flow_adjusted_performance(
            state, total_equity=5_000_000, as_of=T0 + timedelta(days=5))
        self.assertAlmostEqual(polluted.unit_nav, 5_000_000 / 30_000)

    def test_unclassified_withdrawal_does_not_hide_a_real_drawdown(self):
        state = initialize_flow_adjusted_performance(starting_equity=5_000_000, as_of=T0)
        later = update_flow_adjusted_performance(
            state, total_equity=3_000_000, as_of=T0 + timedelta(days=1),
            freeze=True, unclassified_excluded_amount=-1_000_000)
        # 출금 100만 원을 되돌려 계산하면 평가액 400만 원 → 낙폭 -20%.
        self.assertAlmostEqual(later.drawdown, -0.2)

    def test_late_deposit_uses_unit_price_before_the_flow_date_and_restates_hwm(self):
        snapshots = [
            SnapshotRow("2026-01-05", 3_000_000, 0, 30_000, 100.0),   # 발생일 직전
            SnapshotRow("2026-01-06", 5_000_000, 2_000_000, 30_000, 100.0),  # 입금일(미분류로 제외)
            SnapshotRow("2026-01-07", 5_050_000, 2_000_000, 30_000, 100.0),
        ]
        reference, delta_units, rows = restate_after_late_flow(
            snapshots, flow_date="2026-01-06", amount=2_000_000, flow_type="DEPOSIT",
            hwm_before_flow=100.0)
        self.assertAlmostEqual(reference, 100.0)
        self.assertAlmostEqual(delta_units, 20_000.0)
        self.assertEqual([row.snapshot_date for row in rows], ["2026-01-06", "2026-01-07"])
        self.assertTrue(all(row.restated for row in rows))
        self.assertAlmostEqual(rows[0].unit_nav, 100.0)
        self.assertAlmostEqual(rows[1].unit_nav, 5_050_000 / 50_000)
        self.assertAlmostEqual(rows[1].hwm, 101.0)

    def test_late_distribution_and_adjustment_do_not_change_units(self):
        snapshots = [SnapshotRow("2026-01-05", 1_000_000, 0, 10_000, 100.0),
                     SnapshotRow("2026-01-06", 1_022_000, 22_000, 10_000, 100.0)]
        for flow_type in ("DISTRIBUTION", "ADJUSTMENT"):
            reference, delta_units, rows = restate_after_late_flow(
                snapshots, flow_date="2026-01-06", amount=22_000, flow_type=flow_type,
                hwm_before_flow=100.0)
            self.assertIsNone(reference)
            self.assertEqual(delta_units, 0.0)
            self.assertAlmostEqual(rows[0].unit_nav, 102.2)  # 분배금은 성과에 포함된다
        with self.assertRaises(ValueError):
            restate_after_late_flow(snapshots, flow_date="2026-01-06", amount=1, flow_type="UNCLASSIFIED",
                                    hwm_before_flow=100.0)

    def test_late_deposit_without_prior_snapshot_cannot_be_restated_automatically(self):
        snapshots = [SnapshotRow("2026-01-06", 5_000_000, 2_000_000, 30_000, 100.0)]
        self.assertEqual(
            restate_after_late_flow(snapshots, flow_date="2026-01-06", amount=2_000_000,
                                    flow_type="DEPOSIT", hwm_before_flow=100.0),
            (None, None, []))


class QuiescentReconciliationTests(unittest.TestCase):
    """체결 진행 중에는 대사하지 않고, 불일치는 2회 재현돼야 미분류로 확정되는지 봅니다."""

    def test_reconciliation_waits_for_no_active_orders_and_fill_grace(self):
        self.assertFalse(reconciliation_allowed(active_order_count=1, last_fill_at=None, now=T0))
        self.assertFalse(reconciliation_allowed(
            active_order_count=0, last_fill_at=T0 - timedelta(seconds=59), now=T0))
        self.assertTrue(reconciliation_allowed(
            active_order_count=0, last_fill_at=T0 - timedelta(seconds=60), now=T0))
        self.assertTrue(reconciliation_allowed(active_order_count=0, last_fill_at=None, now=T0))

    def test_default_tolerance_is_1000_won_so_rounding_noise_is_not_a_flow(self):
        self.assertEqual(unexplained_cash_delta(1_000_000, -200_000, 800_015), 0)
        self.assertEqual(unexplained_cash_delta(1_000_000, -200_000, 802_000), 2_000)

    def test_judgement_state_machine(self):
        kwargs = dict(now=T0 + timedelta(seconds=61))
        self.assertIs(judge_discrepancy(0, provisional_amount=None, first_observed_at=None, now=T0),
                      DiscrepancyDecision.NONE)
        self.assertIs(judge_discrepancy(5_000, provisional_amount=None, first_observed_at=None, now=T0),
                      DiscrepancyDecision.PROVISIONAL)
        self.assertIs(judge_discrepancy(0, provisional_amount=5_000, first_observed_at=T0, **kwargs),
                      DiscrepancyDecision.DISMISSED)
        self.assertIs(judge_discrepancy(5_000, provisional_amount=5_000, first_observed_at=T0,
                                        now=T0 + timedelta(seconds=30)), DiscrepancyDecision.WAIT)
        self.assertIs(judge_discrepancy(5_000, provisional_amount=5_000, first_observed_at=T0, **kwargs),
                      DiscrepancyDecision.UNCLASSIFIED)
        self.assertIs(judge_discrepancy(-5_000, provisional_amount=5_000, first_observed_at=T0, **kwargs),
                      DiscrepancyDecision.WAIT)  # 부호가 다르면 같은 불일치가 아니다

    def test_provisional_waits_buys_then_unclassified_freezes_hwm_and_dismiss_is_automatic(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = initialize_database(Path(directory) / "t.db")
            try:
                day = date(2026, 1, 5)
                self.assertIs(record_discrepancy(connection, 5_000, trade_date=day, now=T0),
                              DiscrepancyDecision.PROVISIONAL)
                self.assertTrue(buy_wait_for_cash_flow(connection))
                self.assertFalse(hwm_frozen_for_cash_flow(connection))  # PROVISIONAL 은 HWM 에 영향 없음
                self.assertEqual(active_codes(connection, "BUY_BLOCK"), ())
                later = T0 + timedelta(seconds=61)
                self.assertIs(record_discrepancy(connection, 5_020, trade_date=day, now=later),
                              DiscrepancyDecision.UNCLASSIFIED)
                self.assertTrue(hwm_frozen_for_cash_flow(connection))
                self.assertEqual(excluded_unclassified_amount(connection), 5_020)
                self.assertEqual(active_codes(connection, "BUY_BLOCK"), ("UNCLASSIFIED_CASH_FLOW",))

                # 두 번째 건: 다음 대사가 차이를 설명하면 자동 무효화(DISMISSED).
                self.assertIs(record_discrepancy(connection, 7_000, trade_date=day, now=later),
                              DiscrepancyDecision.PROVISIONAL)
                self.assertIs(record_discrepancy(connection, 0, trade_date=day,
                                                 now=later + timedelta(seconds=90)),
                              DiscrepancyDecision.DISMISSED)
                statuses = [r[0] for r in connection.execute(
                    "SELECT classification_status FROM cash_flows ORDER BY id")]
                self.assertEqual(statuses, ["UNCLASSIFIED_CASH_FLOW", "DISMISSED"])
            finally:
                connection.close()

    def test_learning_period_skips_buy_block_but_keeps_hwm_freeze_and_warns(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = initialize_database(Path(directory) / "t.db")
            try:
                day = date(2026, 1, 5)
                record_discrepancy(connection, 5_000, trade_date=day, now=T0, learning_period=True)
                record_discrepancy(connection, 5_000, trade_date=day, now=T0 + timedelta(seconds=61),
                                   learning_period=True)
                self.assertEqual(active_codes(connection, "BUY_BLOCK"), ())
                self.assertTrue(hwm_frozen_for_cash_flow(connection))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM system_events WHERE event_type = 'LEARNING_PERIOD_UNCLASSIFIED'"
                ).fetchone()[0], 1)
            finally:
                connection.close()


class UnresolvedHaltAlertTests(unittest.TestCase):
    """24시간 넘게 풀리지 않은 SAFE_HALT 는 같은 간격으로 CRITICAL 재경보합니다."""

    def test_alert_fires_after_24_hours_then_repeats_every_24_hours(self):
        from src.safety_flags import clear_flag, due_unresolved_halt_alerts
        with tempfile.TemporaryDirectory() as directory:
            connection = initialize_database(Path(directory) / "t.db")
            try:
                raise_flag(connection, "HALT", "DEPLOY_ROLLBACK", now=T0)
                self.assertEqual(due_unresolved_halt_alerts(connection, now=T0 + timedelta(hours=23)), ())
                self.assertEqual(
                    due_unresolved_halt_alerts(connection, now=T0 + timedelta(hours=24)),
                    ("DEPLOY_ROLLBACK",))
                self.assertEqual(due_unresolved_halt_alerts(connection, now=T0 + timedelta(hours=40)), ())
                self.assertEqual(
                    due_unresolved_halt_alerts(connection, now=T0 + timedelta(hours=48)),
                    ("DEPLOY_ROLLBACK",))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM system_events WHERE event_type = 'HALT_UNRESOLVED' "
                    "AND severity = 'CRITICAL'").fetchone()[0], 2)
                clear_flag(connection, "HALT", "DEPLOY_ROLLBACK", now=T0 + timedelta(hours=49),
                           cleared_by="op")
                self.assertEqual(due_unresolved_halt_alerts(connection, now=T0 + timedelta(hours=96)), ())
            finally:
                connection.close()


class TelegramClassifyArgumentTests(unittest.TestCase):
    """`/classify` 인자 형식에 v3.7.5 의 ADJUSTMENT·DISMISS 가 추가됐는지 봅니다."""

    def test_new_classification_words_are_accepted_and_unknown_ones_are_not(self):
        from src.telegram_bot import TelegramCommandHandler
        valid = TelegramCommandHandler._valid_arguments
        for word in ("deposit", "withdrawal", "distribution", "adjustment", "dismiss"):
            self.assertTrue(valid(f"/classify 12 {word}"), word)
        for bad in ("/classify 12 gift", "/classify x dismiss", "/classify 12", "/classify 12 dismiss now"):
            self.assertFalse(valid(bad), bad)


class ConfigV375Tests(unittest.TestCase):
    """10.5절 새 검사: 보호성 손절 설정은 끄기만 가능하고, 평가 현금 필드는 대사 필드와 같아야 합니다."""

    def test_shipped_values(self):
        config = load_config(environ={})
        self.assertEqual(config["risk"]["safe_halt"]["protective_exit"], {"enabled": True})
        self.assertEqual(config["accounting"]["cash_tolerance_krw"], 1000)
        self.assertNotIn("emergency", config["execution"])
        validate_config(config)

    def test_protective_exit_cannot_be_widened_and_removed_emergency_block_is_rejected(self):
        for mutate in (
            lambda c: c["risk"]["safe_halt"]["protective_exit"].update({"allowed_reasons": ["DB_INTEGRITY"]}),
            lambda c: c["risk"]["safe_halt"]["protective_exit"].update({"enabled": "yes"}),
            lambda c: c["execution"].update({"emergency": {"enabled": False}}),
            lambda c: c["accounting"].update({"valuation_cash_field": "dnca_tot_amt"}),
        ):
            config = load_config(environ={})
            mutate(config)
            with self.assertRaises(ConfigError):
                validate_config(config)


if __name__ == "__main__":
    unittest.main()
