import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.db import initialize_database
from src.order_manager import (
    ExitObligation,
    IntentConflictError,
    OrderIntentType,
    OrderIntent,
    OrderSide,
    OrderStatus,
    PendingConfirmationEvidence,
    Quote,
    record_exit_obligation,
    record_order_attempt,
    recover_exit_obligation_quantity,
    persist_order_intent,
    persist_order_transition,
    orders_due_for_cancel,
    resolve_pending_confirmation,
    allows_buy_submission,
    calculate_limit_price,
    MarketAnomalyError,
    RealOrderGateContext,
    transition_order_status,
    submit_recorded_limit_order,
    validate_order_submission_gate,
    validate_spread_and_nav,
    validate_real_order_gate,
)


UTC = timezone.utc


class OrderManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.connection = initialize_database(
            Path(self.temp_dir.name) / "mosasaurus.db"
        )
        self.addCleanup(self.connection.close)
        self.now = datetime(2026, 11, 23, 6, 0, tzinfo=UTC)
        self.connection.execute(
            """
            INSERT INTO universe_assets (
                ticker, name, category, region, underlying_index,
                product_type, replication_method, status, data_status, added_at
            ) VALUES (
                '123450', 'Test ETF', 'RISK_ASSET', 'KR', 'TEST',
                'SPOT_ETF', 'PHYSICAL', 'ACTIVE', 'VALID',
                '2026-01-01T00:00:00+00:00'
            )
            """
        )
        self.connection.execute(
            """
            INSERT INTO portfolio_slots (
                slot_id, target_weight, ticker, assigned_at, assignment_reason
            ) VALUES (
                'S1', 0.45, '123450',
                '2026-01-01T00:00:00+00:00', 'test'
            )
            """
        )

    def test_idempotency_key_is_deterministic(self):
        intent = OrderIntent(
            cycle_id="2026-W48",
            intent_type=OrderIntentType.STRATEGY_REBALANCE,
            slot_id="S1",
            ticker="123450",
            side=OrderSide.BUY,
            quantity=1,
        )

        same_logical_intent = OrderIntent(
            cycle_id="2026-W48",
            intent_type=OrderIntentType.STRATEGY_REBALANCE,
            slot_id="S1",
            ticker="123450",
            side=OrderSide.BUY,
            quantity=9,
        )
        self.assertEqual(intent.idempotency_key(), same_logical_intent.idempotency_key())
        self.assertNotEqual(
            intent.payload_hash(policy_payload={"limit": "best_ask"}),
            same_logical_intent.payload_hash(policy_payload={"limit": "best_ask"}),
        )

    def test_valid_transition_is_allowed(self):
        self.assertEqual(
            transition_order_status(
                OrderStatus.CREATED,
                OrderStatus.SUBMITTING,
            ),
            OrderStatus.SUBMITTING,
        )

    def test_timeout_uses_unknown_and_cannot_transition_to_submitted(self):
        pending_confirmation = transition_order_status(
            OrderStatus.SUBMITTING,
            OrderStatus.PENDING_CONFIRMATION,
        )
        self.assertEqual(pending_confirmation, OrderStatus.PENDING_CONFIRMATION)
        with self.assertRaisesRegex(ValueError, "Invalid order transition"):
            transition_order_status(pending_confirmation, OrderStatus.CREATED)

    def test_unknown_to_accepted_requires_lookup_recovery(self):
        self.assertEqual(
            transition_order_status(
                OrderStatus.PENDING_CONFIRMATION,
                OrderStatus.ACCEPTED,
            ),
            OrderStatus.ACCEPTED,
        )

    def test_buy_limit_uses_ask_and_sell_uses_bid(self):
        quote = Quote(bid=10000, ask=10010, nav=10005.0)

        self.assertEqual(
            calculate_limit_price(quote, OrderSide.BUY),
            10010,
        )
        self.assertEqual(
            calculate_limit_price(quote, OrderSide.SELL),
            10000,
        )

    def test_reduce_only_sell_uses_bid1_once_and_refuses_market_anomaly(self):
        """v3.7.5: 위험 축소 매도는 Bid1 1회(재가격 없음), iNAV 하한 아래면 당일 미제출입니다."""
        quote = Quote(bid=10000, ask=10010, nav=10050.0)
        self.assertEqual(
            calculate_limit_price(quote, OrderSide.SELL, reduce_only=True, tick=10), 10000)
        # iNAV 가 없거나 무효여도 위험 축소 매도 자체는 막지 않는다.
        self.assertEqual(
            calculate_limit_price(Quote(10000, 10010, None), OrderSide.SELL, reduce_only=True, tick=10),
            10000)
        # Bid1 이 iNAV×(1-2%) 보다 낮으면 이상 시장.
        with self.assertRaises(MarketAnomalyError):
            calculate_limit_price(Quote(9700, 9710, 10050.0), OrderSide.SELL, reduce_only=True, tick=10)
        with self.assertRaises(ValueError):  # 매수에는 쓸 수 없다
            calculate_limit_price(quote, OrderSide.BUY, reduce_only=True)
        import src.order_manager as om
        for removed in ("next_emergency_price", "calculate_emergency_reprice", "tick_down"):
            self.assertFalse(hasattr(om, removed), removed)

    def test_limit_prices_round_down_to_tick(self):
        quote = Quote(bid=10009, ask=10019, nav=10010.0)

        self.assertEqual(
            calculate_limit_price(quote, OrderSide.BUY, tick=10),
            10010,
        )
        self.assertEqual(
            calculate_limit_price(quote, OrderSide.SELL, tick=10),
            10000,
        )

    def test_real_gate_requires_all_hard_checks(self):
        context = RealOrderGateContext(
            environment="REAL",
            operator_approval=True,
            reconciliation_ok=True,
            reconciliation_fresh=True,
            unresolved_order_exists=False,
            unclassified_cash_flow_exists=False,
            token_valid=True,
            calendar_valid=True,
            quote_valid=True,
            adjusted_data_valid=True,
            backtest_calibrated=True,
            mode_allows_order=True,
            idempotency_conflict=False,
        )
        self.assertTrue(validate_real_order_gate(context))
        self.assertFalse(
            validate_real_order_gate(
                RealOrderGateContext(
                    environment="REAL",
                    operator_approval=True,
                    reconciliation_ok=True,
                    reconciliation_fresh=True,
                    unresolved_order_exists=False,
                    unclassified_cash_flow_exists=False,
                    token_valid=True,
                    calendar_valid=True,
                    quote_valid=True,
                    adjusted_data_valid=True,
                    backtest_calibrated=False,
                    mode_allows_order=True,
                    idempotency_conflict=False,
                )
            )
        )

    def test_smoke_test_gate_is_separate_and_has_one_share_limits(self):
        context = RealOrderGateContext(
            environment="REAL",
            operator_approval=True,
            reconciliation_ok=True,
            reconciliation_fresh=True,
            unresolved_order_exists=False,
            unclassified_cash_flow_exists=False,
            token_valid=True,
            calendar_valid=True,
            quote_valid=True,
            adjusted_data_valid=True,
            backtest_calibrated=False,
            mode_allows_order=True,
            idempotency_conflict=False,
            intent_type=OrderIntentType.SMOKE_TEST,
            smoke_test_approved=True,
            smoke_test_ticker_allowed=True,
            smoke_test_order_limit=True,
        )

        self.assertTrue(validate_real_order_gate(context))
        self.assertFalse(
            validate_real_order_gate(
                RealOrderGateContext(
                    **{
                        **context.__dict__,
                        "smoke_test_ticker_allowed": False,
                    }
                )
            )
        )

    def test_same_idempotency_key_reuses_intent_and_payload_change_conflicts(self):
        intent = OrderIntent(
            cycle_id="cycle-1",
            intent_type=OrderIntentType.STRATEGY_REBALANCE,
            slot_id="S1",
            ticker="123450",
            side=OrderSide.BUY,
            quantity=2,
        )
        first = persist_order_intent(
            self.connection,
            intent,
            policy_payload={"limit_policy": "best_ask"},
            created_at=self.now,
        )
        second = persist_order_intent(
            self.connection,
            intent,
            policy_payload={"limit_policy": "best_ask"},
            created_at=self.now,
        )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.parent_intent_id, second.parent_intent_id)
        with self.assertRaisesRegex(IntentConflictError, "INTENT_CONFLICT"):
            persist_order_intent(
                self.connection,
                OrderIntent(
                    cycle_id="cycle-1",
                    intent_type=OrderIntentType.STRATEGY_REBALANCE,
                    slot_id="S1",
                    ticker="123450",
                    side=OrderSide.BUY,
                    quantity=3,
                ),
                policy_payload={"limit_policy": "best_ask"},
                created_at=self.now,
            )

    def test_new_attempt_requires_authoritative_not_accepted_transition(self):
        intent = OrderIntent(
            cycle_id="cycle-2",
            intent_type=OrderIntentType.STRATEGY_REBALANCE,
            slot_id="S1",
            ticker="123450",
            side=OrderSide.BUY,
            quantity=1,
        )
        saved = persist_order_intent(
            self.connection,
            intent,
            policy_payload={"quantity": 1},
            created_at=self.now,
        )
        record_order_attempt(
            self.connection,
            order_id="order-1",
            parent_intent_id=saved.parent_intent_id,
            attempt_id="attempt-1",
            order_price=1000,
            strategy_reason="rebalance",
            submitted_at=self.now,
            request_payload_hash="safe-hash",
        )
        with self.assertRaisesRegex(ValueError, "authoritative"):
            record_order_attempt(
                self.connection,
                order_id="order-2",
                parent_intent_id=saved.parent_intent_id,
                attempt_id="attempt-2",
                order_price=1000,
                strategy_reason="rebalance",
                submitted_at=self.now,
                request_payload_hash="safe-hash",
            )
        persist_order_transition(
            self.connection,
            order_id="order-1",
            new_status=OrderStatus.PENDING_CONFIRMATION,
            actor="order_manager",
            reason_code="TIMEOUT",
            correlation_id="attempt-1",
            occurred_at=self.now,
        )
        persist_order_transition(
            self.connection,
            order_id="order-1",
            new_status=OrderStatus.CONFIRMED_NOT_ACCEPTED,
            actor="reconciliation",
            reason_code="AUTHORITATIVE_NOT_FOUND",
            correlation_id="attempt-1",
            occurred_at=self.now,
        )
        record_order_attempt(
            self.connection,
            order_id="order-2",
            parent_intent_id=saved.parent_intent_id,
            attempt_id="attempt-2",
            order_price=1000,
            strategy_reason="rebalance",
            submitted_at=self.now,
            request_payload_hash="safe-hash",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM orders WHERE parent_intent_id = ?",
                (saved.parent_intent_id,),
            ).fetchone()[0],
            2,
        )

    def test_pending_confirmation_needs_complete_authoritative_evidence(self):
        not_enough = resolve_pending_confirmation(
            PendingConfirmationEvidence(
                authoritative_acceptance_query_complete=True,
                execution_query_complete=True,
            )
        )
        confirmed_absence = resolve_pending_confirmation(
            PendingConfirmationEvidence(
                authoritative_acceptance_query_complete=True,
                authoritative_order_not_found=True,
                execution_query_complete=True,
                execution_not_found=True,
                market_closed=True,
            )
        )
        found = resolve_pending_confirmation(
            PendingConfirmationEvidence(found_status=OrderStatus.PARTIALLY_FILLED)
        )

        self.assertEqual(not_enough, OrderStatus.PENDING_CONFIRMATION)
        self.assertEqual(confirmed_absence, OrderStatus.CONFIRMED_NOT_ACCEPTED)
        self.assertEqual(found, OrderStatus.PARTIALLY_FILLED)

    def test_timeout_does_not_retry_submission(self):
        class FakeTransport:
            def __init__(self):
                self.calls = 0

            def submit_limit_order(self, **_kwargs):
                self.calls += 1
                raise TimeoutError("sensitive broker response")

        transport = FakeTransport()
        gate_context = RealOrderGateContext(
            environment="PAPER",
            operator_approval=False,
            reconciliation_ok=True,
            reconciliation_fresh=True,
            unresolved_order_exists=False,
            unclassified_cash_flow_exists=False,
            token_valid=True,
            calendar_valid=True,
            quote_valid=True,
            adjusted_data_valid=True,
            backtest_calibrated=False,
            mode_allows_order=True,
            idempotency_conflict=False,
        )
        result = submit_recorded_limit_order(
            transport,
            gate_context=gate_context,
            ticker="123450",
            side=OrderSide.BUY,
            quantity=1,
            price=1000,
        )

        self.assertEqual(result.status, OrderStatus.PENDING_CONFIRMATION)
        self.assertEqual(transport.calls, 1)

    def test_order_submission_fails_closed_before_transport(self):
        class FakeTransport:
            def __init__(self):
                self.calls = 0

            def submit_limit_order(self, **_kwargs):
                self.calls += 1
                return {"accepted": True, "order_number": "paper-1"}

        transport = FakeTransport()
        context = RealOrderGateContext(
            environment="PAPER",
            operator_approval=False,
            reconciliation_ok=True,
            reconciliation_fresh=True,
            unresolved_order_exists=False,
            unclassified_cash_flow_exists=False,
            token_valid=True,
            calendar_valid=True,
            quote_valid=True,
            adjusted_data_valid=True,
            backtest_calibrated=False,
            mode_allows_order=False,
            idempotency_conflict=False,
        )

        self.assertFalse(validate_order_submission_gate(context))
        with self.assertRaises(PermissionError):
            submit_recorded_limit_order(
                transport,
                gate_context=context,
                ticker="123450",
                side=OrderSide.BUY,
                quantity=1,
                price=1000,
            )
        self.assertEqual(transport.calls, 0)

    def test_cancel_cutoff_includes_all_unfilled_sides_only_after_deadline(self):
        intent = OrderIntent(
            cycle_id="cycle-cancel",
            intent_type=OrderIntentType.STRATEGY_REBALANCE,
            slot_id="S1",
            ticker="123450",
            side=OrderSide.SELL,
            quantity=2,
        )
        saved = persist_order_intent(
            self.connection,
            intent,
            policy_payload={"limit_policy": "best_bid"},
            created_at=self.now,
        )
        record_order_attempt(
            self.connection,
            order_id="order-cancel",
            parent_intent_id=saved.parent_intent_id,
            attempt_id="attempt-cancel",
            order_price=1000,
            strategy_reason="rebalance",
            submitted_at=self.now,
            request_payload_hash="safe-hash",
        )
        persist_order_transition(
            self.connection,
            order_id="order-cancel",
            new_status=OrderStatus.ACCEPTED,
            actor="order_manager",
            reason_code="BROKER_ACCEPTED",
            correlation_id="attempt-cancel",
            occurred_at=self.now,
        )
        deadline = self.now.replace(hour=6, minute=14, second=30)

        self.assertEqual(
            orders_due_for_cancel(
                self.connection,
                now=deadline.replace(second=29),
                cancel_deadline=deadline,
            ),
            (),
        )
        self.assertEqual(
            orders_due_for_cancel(
                self.connection,
                now=deadline,
                cancel_deadline=deadline,
            ),
            ("order-cancel",),
        )

    def test_exit_obligation_is_unique_and_recovery_is_fail_closed(self):
        obligation = ExitObligation(
            obligation_id="exit-1",
            ticker="123450",
            source_event_id="risk-event-1",
            required_qty=5,
            reason_code="TRAILING_STOP",
        )
        self.assertTrue(
            record_exit_obligation(
                self.connection,
                obligation,
                created_at=self.now,
            )
        )
        self.assertFalse(
            record_exit_obligation(
                self.connection,
                obligation,
                created_at=self.now,
            )
        )
        self.assertIsNone(
            recover_exit_obligation_quantity(
                self.connection,
                obligation_id="exit-1",
                broker_position_qty=5,
                reconciliation_ok=False,
                existing_order_terminal=True,
                account_data_fresh=True,
                quote_and_adjustment_valid=True,
                checked_at=self.now,
            )
        )
        self.assertEqual(
            recover_exit_obligation_quantity(
                self.connection,
                obligation_id="exit-1",
                broker_position_qty=3,
                reconciliation_ok=True,
                existing_order_terminal=True,
                account_data_fresh=True,
                quote_and_adjustment_valid=True,
                checked_at=self.now,
            ),
            3,
        )

    def test_retry_intent_requires_a_linked_obligation_and_fills_resolve_it(self):
        """v3.7.5: 의무 재주문은 obligation_id 로 연결되고 체결 합계가 의무를 RESOLVED 로 닫습니다."""
        record_exit_obligation(
            self.connection,
            ExitObligation("exit-9", "123450", "risk-event-9", 5, "TRAILING_STOP"),
            created_at=self.now,
        )
        with self.assertRaises(ValueError):
            persist_order_intent(
                self.connection,
                OrderIntent("X-exit-9:2026-11-24", OrderIntentType.EXIT_OBLIGATION_RETRY,
                            None, "123450", OrderSide.SELL, 5),
                policy_payload={"limit_policy": "best_bid"}, created_at=self.now)
        saved = persist_order_intent(
            self.connection,
            OrderIntent("X-exit-9:2026-11-24", OrderIntentType.EXIT_OBLIGATION_RETRY,
                        None, "123450", OrderSide.SELL, 5, obligation_id="exit-9"),
            policy_payload={"limit_policy": "best_bid"}, created_at=self.now)
        record_order_attempt(
            self.connection, order_id="o-x", parent_intent_id=saved.parent_intent_id,
            attempt_id="a-x", order_price=1000, strategy_reason="exit",
            submitted_at=self.now, request_payload_hash="h")
        from src.order_manager import record_fill_from_cumulative
        record_fill_from_cumulative(
            self.connection, order_id="o-x", new_cumulative_qty=2, new_average_price=1000.0,
            executed_at=self.now)
        row = self.connection.execute(
            "SELECT filled_qty, status FROM exit_obligations WHERE obligation_id = 'exit-9'").fetchone()
        self.assertEqual(tuple(row), (2, "OPEN"))
        record_fill_from_cumulative(
            self.connection, order_id="o-x", new_cumulative_qty=5, new_average_price=1000.0,
            executed_at=self.now)
        row = self.connection.execute(
            "SELECT filled_qty, status FROM exit_obligations WHERE obligation_id = 'exit-9'").fetchone()
        self.assertEqual(tuple(row), (5, "RESOLVED"))

    def test_obligation_is_voided_when_position_is_gone_and_supersede_order_is_fixed(self):
        """보유 0 이면 VOIDED, 수량이 달라지면 SUPERSEDED + 새 OPEN(required 불변, filled 0)입니다."""
        from src.order_manager import supersede_exit_obligation
        record_exit_obligation(
            self.connection,
            ExitObligation("exit-a", "123450", "ev-a", 100, "TRAILING_STOP"),
            created_at=self.now)
        supersede_exit_obligation(
            self.connection, obligation_id="exit-a", created_at=self.now,
            new_obligation=ExitObligation("exit-b", "123450", "ev-b", 40, "TRAILING_STOP"))
        rows = {r["obligation_id"]: tuple(r) for r in self.connection.execute(
            "SELECT obligation_id, status, required_qty, filled_qty, superseded_by FROM exit_obligations")}
        self.assertEqual(rows["exit-a"], ("exit-a", "SUPERSEDED", 100, 0, "exit-b"))
        self.assertEqual(rows["exit-b"], ("exit-b", "OPEN", 40, 0, None))
        with self.assertRaises(sqlite3.DatabaseError):  # required_qty 는 DB 트리거로도 불변
            self.connection.execute("UPDATE exit_obligations SET required_qty = 1 WHERE obligation_id = 'exit-b'")
        self.assertEqual(
            recover_exit_obligation_quantity(
                self.connection, obligation_id="exit-b", broker_position_qty=0,
                reconciliation_ok=True, existing_order_terminal=True, account_data_fresh=True,
                quote_and_adjustment_valid=True, checked_at=self.now),
            0)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exit_obligations WHERE obligation_id = 'exit-b'").fetchone()[0], "VOIDED")

    def test_spread_and_nav_guards_reject_invalid_quote(self):
        self.assertFalse(
            validate_spread_and_nav(
                Quote(bid=10000, ask=11000, nav=10500.0),
                region="KR",
            )
        )
        self.assertFalse(
            validate_spread_and_nav(
                Quote(bid=10000, ask=10010, nav=9800.0),
                region="KR",
            )
        )

    def test_stale_or_unknown_state_blocks_buy(self):
        self.assertFalse(
            allows_buy_submission(
                data_fresh=True,
                account_fresh=False,
                account_matched=True,
                unknown_order_exists=False,
            )
        )
        self.assertFalse(
            allows_buy_submission(
                data_fresh=True,
                account_fresh=True,
                account_matched=True,
                unknown_order_exists=True,
            )
        )
        self.assertTrue(
            allows_buy_submission(
                data_fresh=True,
                account_fresh=True,
                account_matched=True,
                unknown_order_exists=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
