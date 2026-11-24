"""명세 v3.7.4 순수 규칙(진리표·전이표·슬롯·낙폭·성과·설정) 단위 테스트입니다."""

import itertools
import math
import re
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from src.cash_recon import (
    in_learning_period,
    matches_expected_distribution,
    reconciliation_transaction_id,
    unexplained_cash_delta,
)
from src.config import ConfigError, load_config, validate_config
from src.order_manager import (
    ACTIVE_ORDER_STATUSES,
    TERMINAL_ORDER_STATUSES,
    OrderSide,
    OrderStatus,
    PendingConfirmationEvidence,
    _ALLOWED_TRANSITIONS,
    resolve_pending_confirmation,
    transition_order_status,
)
from src.order_policy import OrderKind, is_allowed
from src.performance import (
    apply_cash_flow,
    initialize_flow_adjusted_performance,
    rebase_hwm,
    update_flow_adjusted_performance,
)
from src.portfolio_manager import SlotCandidate, select_final_slots
from src.quant_engine import calculate_momentum_score
from src.risk_manager import DrawdownMode as D, OperatingMode as M, next_drawdown_stage

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc)


class OrderPolicyTruthTableTests(unittest.TestCase):
    """0.6절 진리표를 모든 조합에서 기대값과 대조합니다."""

    def test_safe_halt_allows_only_cancel(self):
        for stage, kind in itertools.product(D, OrderKind):
            self.assertEqual(is_allowed(M.SAFE_HALT, stage, kind), kind is OrderKind.CANCEL)

    def test_cancel_is_always_allowed(self):
        for mode, stage in itertools.product(M, D):
            self.assertTrue(is_allowed(mode, stage, OrderKind.CANCEL))

    def test_emergency_and_paused_allow_only_reduce_only_and_retry(self):
        allowed = {OrderKind.REDUCE_ONLY_SELL, OrderKind.EXIT_OBLIGATION_RETRY, OrderKind.CANCEL}
        for mode, stage, kind in itertools.product((M.EMERGENCY, M.PAUSED), D, OrderKind):
            self.assertEqual(is_allowed(mode, stage, kind), kind in allowed)

    def test_training_allows_parking_buy_but_no_rotation_or_parking_sell(self):
        self.assertTrue(is_allowed(M.TRAINING, D.NORMAL, OrderKind.PARKING_BUY))
        for kind in (OrderKind.RISK_BUY, OrderKind.RISK_ROTATION_SELL,
                     OrderKind.PARKING_SELL, OrderKind.SMOKE_TEST):
            self.assertFalse(is_allowed(M.TRAINING, D.NORMAL, kind))

    def test_normal_mode_respects_drawdown_stage(self):
        self.assertTrue(is_allowed(M.NORMAL, D.CAUTION, OrderKind.RISK_BUY))
        self.assertFalse(is_allowed(M.NORMAL, D.CAUTION, OrderKind.RISK_TOP_UP_BUY))
        self.assertFalse(is_allowed(M.NORMAL, D.WARNING, OrderKind.RISK_BUY))
        self.assertTrue(is_allowed(M.NORMAL, D.WARNING, OrderKind.PARKING_BUY))
        self.assertTrue(is_allowed(M.NORMAL, D.NORMAL, OrderKind.SMOKE_TEST))
        self.assertFalse(is_allowed(M.NORMAL, D.CAUTION, OrderKind.SMOKE_TEST))

    def test_contradictory_critical_stage_fails_closed(self):
        self.assertFalse(is_allowed(M.NORMAL, D.CRITICAL, OrderKind.RISK_BUY))
        self.assertFalse(is_allowed(M.NORMAL, D.CRITICAL, OrderKind.PARKING_BUY))
        self.assertTrue(is_allowed(M.NORMAL, D.CRITICAL, OrderKind.REDUCE_ONLY_SELL))


class OrderStateMachineTests(unittest.TestCase):
    """7.3절 전이표: 종결 상태 불변과 데드락 부재를 검증합니다."""

    def test_every_active_status_can_reach_a_terminal_status(self):
        for start in ACTIVE_ORDER_STATUSES:
            seen, frontier = {start}, [start]
            while frontier:
                current = frontier.pop()
                for nxt in _ALLOWED_TRANSITIONS.get(current, set()):
                    if nxt not in seen:
                        seen.add(nxt)
                        frontier.append(nxt)
            self.assertTrue(seen & TERMINAL_ORDER_STATUSES, start)

    def test_terminal_statuses_have_no_outgoing_transitions(self):
        for status in TERMINAL_ORDER_STATUSES:
            self.assertNotIn(status, _ALLOWED_TRANSITIONS)
        self.assertEqual(len(ACTIVE_ORDER_STATUSES), 6)
        self.assertEqual(len(TERMINAL_ORDER_STATUSES), 5)

    def test_confirmed_not_accepted_cannot_resubmit(self):
        with self.assertRaises(ValueError):
            transition_order_status(OrderStatus.CONFIRMED_NOT_ACCEPTED, OrderStatus.SUBMITTING)

    def test_added_transitions_are_allowed(self):
        for old, new in (
            (OrderStatus.CREATED, OrderStatus.EXPIRED),
            (OrderStatus.CANCEL_PENDING, OrderStatus.ACCEPTED),
            (OrderStatus.CANCEL_PENDING, OrderStatus.EXPIRED),
            (OrderStatus.ACCEPTED, OrderStatus.EXPIRED),
            (OrderStatus.PENDING_CONFIRMATION, OrderStatus.PARTIALLY_FILLED),
        ):
            self.assertEqual(transition_order_status(old, new), new)

    def test_partially_filled_cannot_skip_cancel_pending(self):
        with self.assertRaises(ValueError):
            transition_order_status(OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCELED)


class NotAcceptedConfirmationTests(unittest.TestCase):
    """장 마감 후 확정과 장중 전량 매도 확정 규칙입니다."""

    empty = dict(
        authoritative_acceptance_query_complete=True,
        authoritative_order_not_found=True,
        execution_query_complete=True,
        execution_not_found=True,
    )

    def test_buy_is_never_confirmed_intraday(self):
        evidence = PendingConfirmationEvidence(
            **self.empty, side=OrderSide.BUY, full_quantity_sell=False,
            min_wait_elapsed=True, websocket_continuously_connected=True,
            consecutive_empty_polls=5,
        )
        self.assertEqual(resolve_pending_confirmation(evidence), OrderStatus.PENDING_CONFIRMATION)

    def test_partial_sell_is_not_confirmed_intraday(self):
        evidence = PendingConfirmationEvidence(
            **self.empty, side=OrderSide.SELL, full_quantity_sell=False,
            min_wait_elapsed=True, websocket_continuously_connected=True,
            consecutive_empty_polls=5,
        )
        self.assertEqual(resolve_pending_confirmation(evidence), OrderStatus.PENDING_CONFIRMATION)

    def test_full_sell_confirms_only_when_every_condition_holds(self):
        base = dict(
            **self.empty, side=OrderSide.SELL, full_quantity_sell=True,
            min_wait_elapsed=True, websocket_continuously_connected=True,
            consecutive_empty_polls=3,
        )
        self.assertEqual(
            resolve_pending_confirmation(PendingConfirmationEvidence(**base)),
            OrderStatus.CONFIRMED_NOT_ACCEPTED,
        )
        for broken in (
            {"consecutive_empty_polls": 2},
            {"websocket_continuously_connected": False},
            {"acceptance_notice_seen": True},
            {"min_wait_elapsed": False},
        ):
            self.assertEqual(
                resolve_pending_confirmation(PendingConfirmationEvidence(**{**base, **broken})),
                OrderStatus.PENDING_CONFIRMATION,
            )

    def test_after_close_complete_query_confirms_any_side(self):
        evidence = PendingConfirmationEvidence(**self.empty, market_closed=True, side=OrderSide.BUY)
        self.assertEqual(resolve_pending_confirmation(evidence), OrderStatus.CONFIRMED_NOT_ACCEPTED)

    def test_incomplete_query_after_close_stays_pending(self):
        evidence = PendingConfirmationEvidence(
            authoritative_acceptance_query_complete=True,
            authoritative_order_not_found=True,
            market_closed=True,
        )
        self.assertEqual(resolve_pending_confirmation(evidence), OrderStatus.PENDING_CONFIRMATION)


class SlotSelectionTests(unittest.TestCase):
    """순위 버퍼·지수 패밀리·상관·지역 비중 규칙입니다."""

    @staticmethod
    def cand(ticker, region, family):
        return SlotCandidate(ticker, region, family, family)

    def test_us_pair_cannot_take_s1_and_s2(self):
        """US 1·2위는 S1+S2(75%)가 되어 US 한도(65%)를 넘으므로 2위가 제외됩니다."""
        ranked = [self.cand("A", "US", "SP500"), self.cand("B", "US", "NDX"),
                  self.cand("C", "KR", "K200")]
        slots = {s.slot_id: s.ticker for s in select_final_slots(ranked)}
        self.assertEqual(slots, {"S1": "A", "S2": "C", "S3": ""})  # 빈 슬롯은 파킹

    def test_us_pair_allowed_when_it_lands_on_s1_and_s3(self):
        ranked = [self.cand("A", "US", "SP500"), self.cand("C", "KR", "K200"),
                  self.cand("B", "US", "NDX")]
        slots = {s.slot_id: s.ticker for s in select_final_slots(ranked)}
        self.assertEqual(slots, {"S1": "A", "S2": "C", "S3": "B"})

    def test_hedged_and_unhedged_share_one_slot(self):
        ranked = [SlotCandidate("A", "US", "SP500", "SP500"),
                  SlotCandidate("AH", "US", "SP500(H)", "SP500"),
                  self.cand("C", "KR", "K200")]
        tickers = [s.ticker for s in select_final_slots(ranked)]
        self.assertNotIn("AH", tickers)

    def test_high_correlation_blocks_second_slot_and_nan_does_not(self):
        ranked = [self.cand("A", "KR", "X"), self.cand("B", "KR", "Y"), self.cand("C", "US", "Z")]
        blocked = [s.ticker for s in select_final_slots(
            ranked, correlation=lambda a, b: 0.95 if {a, b} == {"A", "B"} else 0.1)]
        self.assertNotIn("B", blocked)
        allowed = [s.ticker for s in select_final_slots(
            ranked, correlation=lambda a, b: math.nan)]
        self.assertIn("B", allowed)

    def test_buffered_holding_is_reranked_to_a_small_slot(self):
        ranked = [self.cand("A", "US", "SP500"), self.cand("C", "KR", "K200"),
                  self.cand("D", "KR", "SEMI"), self.cand("E", "GLOBAL", "ACWI"),
                  self.cand("H", "KR", "BIO")]
        slots = {s.slot_id: s for s in select_final_slots(ranked, held_tickers=["H"])}
        self.assertEqual(slots["S1"].ticker, "A")
        self.assertEqual(slots["S3"].ticker, "H")
        self.assertEqual(slots["S3"].reason, "rank_buffer_hold")

    def test_holding_outside_buffer_gets_no_priority(self):
        ranked = [self.cand(t, "KR", t) for t in "ABCDEFGH"]
        tickers = [s.ticker for s in select_final_slots(ranked, held_tickers=["H"])]
        self.assertNotIn("H", tickers)


class DrawdownHysteresisTests(unittest.TestCase):
    """회복 버퍼·동결·바닥 단계를 검증합니다."""

    def test_recovery_needs_buffer(self):
        self.assertEqual(next_drawdown_stage(D.WARNING, -0.09), D.WARNING)
        self.assertEqual(next_drawdown_stage(D.WARNING, -0.079), D.CAUTION)
        self.assertEqual(next_drawdown_stage(D.CAUTION, -0.04), D.NORMAL)

    def test_worsening_is_immediate_unless_frozen(self):
        self.assertEqual(next_drawdown_stage(D.NORMAL, -0.12), D.WARNING)
        self.assertEqual(next_drawdown_stage(D.NORMAL, -0.12, frozen=True), D.NORMAL)

    def test_floor_stage_after_emergency_release(self):
        self.assertEqual(next_drawdown_stage(D.NORMAL, 0.0, floor_stage=D.WARNING), D.WARNING)


class PerformanceRuleTests(unittest.TestCase):
    """HWM 갱신 시점·동결·epoch·리베이스입니다."""

    def setUp(self):
        self.state = initialize_flow_adjusted_performance(starting_equity=1_000_000, as_of=T0)

    def test_intraday_valuation_does_not_raise_hwm(self):
        later = datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc)
        state = update_flow_adjusted_performance(
            self.state, total_equity=1_200_000, as_of=later, close_snapshot=False)
        self.assertEqual(state.hwm, 100.0)
        self.assertEqual(state.drawdown, 0.0)

    def test_close_snapshot_raises_hwm_unless_frozen(self):
        later = datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc)
        frozen = update_flow_adjusted_performance(
            self.state, total_equity=1_200_000, as_of=later, freeze=True)
        self.assertEqual(frozen.hwm, 100.0)
        closed = update_flow_adjusted_performance(self.state, total_equity=1_200_000, as_of=later)
        self.assertAlmostEqual(closed.hwm, 120.0)

    def test_deposit_does_not_change_unit_price(self):
        later = datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc)
        state = apply_cash_flow(self.state, cash_flow=500_000, flow_type="DEPOSIT", occurred_at=later)
        state = update_flow_adjusted_performance(state, total_equity=1_500_000, as_of=later)
        self.assertAlmostEqual(state.unit_nav, 100.0)
        self.assertAlmostEqual(state.drawdown, 0.0)

    def test_full_withdrawal_closes_epoch_and_reopens_from_last_unit_price(self):
        t1 = datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 6, 7, 0, tzinfo=timezone.utc)
        state = update_flow_adjusted_performance(self.state, total_equity=1_100_000, as_of=t1)
        state = apply_cash_flow(state, cash_flow=-1_100_000, flow_type="WITHDRAWAL", occurred_at=t1)
        self.assertTrue(state.epoch_closed)
        state = update_flow_adjusted_performance(state, total_equity=0, as_of=t2)
        self.assertTrue(state.epoch_closed)  # 0 나누기 없이 유지
        state = update_flow_adjusted_performance(state, total_equity=2_200_000, as_of=t2)
        self.assertEqual(state.epoch, 2)
        self.assertAlmostEqual(state.unit_nav, 110.0)
        self.assertAlmostEqual(state.hwm, 110.0)

    def test_rebase_hwm_keeps_twr(self):
        t1 = datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc)
        up = update_flow_adjusted_performance(self.state, total_equity=1_200_000, as_of=t1)
        down = update_flow_adjusted_performance(up, total_equity=1_000_000, as_of=t1, close_snapshot=False)
        rebased = rebase_hwm(down)
        self.assertTrue(rebased.hwm_rebased)
        self.assertEqual(rebased.drawdown, 0.0)
        self.assertEqual(rebased.twr_cumulative, down.twr_cumulative)


class MomentumFloorTests(unittest.TestCase):
    """변동성 하한이 일간 σ 에 적용되는지 검증합니다."""

    def test_floor_caps_score_for_near_zero_volatility(self):
        asset = [100.0 * (1.001 ** i) for i in range(121)]
        parking = [100.0] * 121
        score = calculate_momentum_score(asset, parking)
        er = {k: 1.001 ** k - 1 for k in (20, 60, 120)}
        expected = sum(w * er[k] / (0.005 * math.sqrt(k)) for k, w in ((20, .4), (60, .4), (120, .2)))
        self.assertAlmostEqual(score, expected, places=9)


class CashReconciliationTests(unittest.TestCase):
    """D+2 예수금 대사 순수 함수입니다."""

    def test_within_tolerance_is_zero(self):
        self.assertEqual(unexplained_cash_delta(1_000_000, -300_000, 700_005), 0)

    def test_unexplained_deposit_is_returned(self):
        self.assertEqual(unexplained_cash_delta(1_000_000, 0, 1_500_000), 500_000)

    def test_distribution_match_tolerance(self):
        self.assertTrue(matches_expected_distribution(10_300, 10_000))
        self.assertFalse(matches_expected_distribution(11_000, 10_000))
        self.assertFalse(matches_expected_distribution(-10_000, 10_000))

    def test_synthetic_transaction_id_and_learning_period(self):
        self.assertEqual(reconciliation_transaction_id(date(2026, 1, 5), 2), "RECON:2026-01-05:2")
        self.assertTrue(in_learning_period(date(2026, 1, 1), date(2026, 1, 10)))
        self.assertFalse(in_learning_period(date(2026, 1, 1), date(2026, 1, 15)))


class ConfigValidationTests(unittest.TestCase):
    """10.5절 설정 정합성 검사와 명세 본문과의 일치를 검증합니다."""

    def test_shipped_rules_pass_validation(self):
        validate_config(load_config(environ={}))

    def test_rules_yaml_matches_spec_block(self):
        text = (ROOT / "mosasaurus_spec_v3.7.5.md").read_text(encoding="utf-8")
        start = text.index("### 10.3 ")
        fence = text.index("```\n", start) + 4
        end = text.index("\n```", fence)
        spec_config = yaml.safe_load(text[fence:end])
        self.assertEqual(spec_config, load_config(environ={}))

    def test_loosened_safety_settings_fail(self):
        for path, value in (
            (("risk", "safe_halt", "allow_reduce_only_sell"), True),
            (("execution", "allow_market_order"), True),
            (("execution", "retry_unknown_order"), True),
            (("universe", "reject_inverse"), False),
        ):
            config = load_config(environ={})
            node = config
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            with self.assertRaises(ConfigError, msg=path):
                validate_config(config)

    def test_deprecated_key_and_bad_time_order_fail(self):
        config = load_config(environ={})
        config["smoke_test_mode"] = False
        with self.assertRaisesRegex(ConfigError, "smoke_test_mode"):
            validate_config(config)
        config = load_config(environ={})
        config["market"]["post_close_reconcile_time"] = "15:20:00"
        with self.assertRaisesRegex(ConfigError, "post_close_reconcile_time"):
            validate_config(config)

    def test_trailing_stop_order(self):
        config = load_config(environ={})
        config["trailing_stop"]["training"] = -0.12
        with self.assertRaises(ConfigError):
            validate_config(config)


if __name__ == "__main__":
    unittest.main()
