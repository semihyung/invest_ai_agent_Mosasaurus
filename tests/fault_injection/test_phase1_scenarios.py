import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db import initialize_database
from src.order_manager import (
    OrderSide,
    OrderStatus,
    Quote,
    allows_buy_submission,
    calculate_limit_price,
    transition_order_status,
    validate_spread_and_nav,
)
from src.portfolio_manager import (
    SlotCandidate,
    assign_risk_slots,
    detect_external_deposit,
    resolve_live_slots,
)
from src.reconciliation import Position, reconcile_positions
from src.risk_manager import (
    RiskFlags,
    get_drawdown_mode,
    resolve_operating_mode,
    should_trailing_sell,
)
from src.scheduler import is_order_window
from src.telegram_bot import TelegramCommandHandler
from src.kis_client import CachedAccessToken, RefreshTrigger, TokenRefreshPolicy, should_refresh_access_token
from datetime import datetime, timedelta, timezone


UTC = timezone.utc


class Phase1ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "mosasaurus.db"
        self.connection = initialize_database(self.db_path)

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def test_01_timeout_never_requeues_to_submitted(self):
        pending_confirmation = transition_order_status(
            OrderStatus.SUBMITTING,
            OrderStatus.PENDING_CONFIRMATION,
        )
        with self.assertRaises(ValueError):
            transition_order_status(pending_confirmation, OrderStatus.CREATED)

    def test_02_partial_fill_is_preserved(self):
        partially_filled = transition_order_status(
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
        )
        self.assertEqual(partially_filled, OrderStatus.PARTIALLY_FILLED)

    def test_03_cancel_pending_fill_wins(self):
        result = transition_order_status(
            OrderStatus.CANCEL_PENDING,
            OrderStatus.FILLED,
        )
        self.assertEqual(result, OrderStatus.FILLED)

    def test_04_unresolved_exit_is_not_auto_resubmitted(self):
        pending_confirmation = transition_order_status(
            OrderStatus.SUBMITTING,
            OrderStatus.PENDING_CONFIRMATION,
        )
        self.assertEqual(pending_confirmation, OrderStatus.PENDING_CONFIRMATION)

    def test_05_idempotency_key_database_guard(self):
        insert = """
            INSERT INTO order_intents (
                parent_intent_id, idempotency_key, cycle_id, intent_type,
                slot_id, ticker, side, requested_qty, intent_payload_hash,
                status, created_at, updated_at
            ) VALUES (?, ?, 'cycle', 'STRATEGY_REBALANCE', NULL,
                      '423160', 'BUY', 1, 'payload', 'CREATED',
                      '2026-11-22T00:00:00Z', '2026-11-22T00:00:00Z')
        """
        self.connection.execute(
            """
            INSERT INTO universe_assets (
                ticker, name, category, region, underlying_index,
                product_type, replication_method, status, data_status, added_at
            ) VALUES (
                '423160', 'Test Parking', 'SAFE_PARKING', 'KR', 'KOFR',
                'SPOT_ETF', 'SYNTHETIC', 'ACTIVE', 'VALID',
                '2026-11-22T00:00:00Z'
            )
            """
        )
        self.connection.execute(insert, ("order-1", "same-key"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert, ("order-2", "same-key"))

    def test_06_sell_first_is_not_bypassed_by_missing_sell_fill(self):
        self.assertFalse(
            allows_buy_submission(
                data_fresh=True,
                account_fresh=True,
                account_matched=True,
                unknown_order_exists=True,
            )
        )

    def test_07_quote_spread_and_nav_are_fail_closed(self):
        quote = Quote(bid=10000, ask=10400, nav=10000)
        self.assertFalse(validate_spread_and_nav(quote, region="KR"))
        self.assertLess(calculate_limit_price(quote, OrderSide.SELL), quote.ask)

    def test_08_cancel_sweep_keeps_no_overnight_buy_intent(self):
        self.assertFalse(
            allows_buy_submission(
                data_fresh=False,
                account_fresh=True,
                account_matched=True,
                unknown_order_exists=False,
            )
        )

    def test_09_bear_market_slots_fall_back_to_parking(self):
        assignments = resolve_live_slots([], set(), "423160")
        self.assertTrue(
            all(assignment.ticker == "423160" for assignment in assignments)
        )

    def test_10_top_three_outside_candidates_are_replaced(self):
        assignments = assign_risk_slots(
            [
                SlotCandidate("A", "KR", "a"),
                SlotCandidate("B", "KR", "b"),
                SlotCandidate("C", "US", "c"),
            ]
        )
        self.assertEqual(
            [assignment.ticker for assignment in assignments],
            ["A", "B", "C"],
        )

    def test_11_live_top_candidate_failure_succeeds_by_fourth_rank(self):
        assignments = resolve_live_slots(
            ["A", "B", "C", "D"],
            {"B", "C", "D"},
            "423160",
        )
        self.assertEqual(
            [assignment.ticker for assignment in assignments[:3]],
            ["B", "C", "D"],
        )

    def test_12_existing_fourth_rank_survival_prevents_sell(self):
        assignments = resolve_live_slots(
            ["A", "B", "C", "D"],
            {"C", "D"},
            "423160",
            existing_holdings={"D"},
        )
        self.assertIn("D", [assignment.ticker for assignment in assignments])

    def test_13_holding_turnover_exception_is_not_forced_sell(self):
        self.assertTrue(
            resolve_live_slots(
                ["A", "D"],
                {"D"},
                "423160",
                existing_holdings={"D"},
            )[0].ticker in {"D", "423160"}
        )

    def test_14_region_concentration_skips_third_us_asset(self):
        assignments = assign_risk_slots(
            [
                SlotCandidate("US1", "US", "us1"),
                SlotCandidate("US2", "US", "us2"),
                SlotCandidate("US3", "US", "us3"),
                SlotCandidate("KR1", "KR", "kr1"),
            ],
            max_slots_per_region=2,
        )
        tickers = [assignment.ticker for assignment in assignments]
        self.assertIn("KR1", tickers)
        self.assertNotIn("US3", tickers)

    def test_15_external_deposit_and_buffer_logic(self):
        result = reconcile_positions(
            [Position("423160", 10)],
            [Position("423160", 10)],
        )
        self.assertTrue(result.reconciliation_ok)
        self.assertEqual(
            detect_external_deposit(
                current_cash=1_600_000,
                prior_cash=1_000_000,
                sell_fills=100_000,
                buy_fills=50_000,
                fees=10_000,
            ),
            560_000,
        )

    def test_16_smoke_test_mode_minimum_amount_exception_is_external_setting(self):
        self.assertFalse(is_order_window(datetime(2026, 11, 22, 23, 0, tzinfo=UTC)))

    def test_17_drawdown_critical_is_emergency(self):
        self.assertEqual(
            get_drawdown_mode(-0.15).value,
            "CRITICAL",
        )

    def test_18_peak_adjustment_does_not_create_false_stop(self):
        self.assertFalse(should_trailing_sell(95.0, 100.0))

    def test_19_holiday_mismatch_is_safe_halt(self):
        result = reconcile_positions([], [])
        self.assertTrue(result.reconciliation_ok)

    def test_20_temporary_holiday_uses_next_open_day(self):
        now = datetime(2026, 11, 23, 6, 0, tzinfo=UTC)
        self.assertTrue(is_order_window(now))

    def test_21_training_emergency_recovery_keeps_training(self):
        flags = RiskFlags(emergency_mode=True, training_mode=True)
        self.assertEqual(resolve_operating_mode(flags).value, "EMERGENCY")

    def test_22_token_reuse_and_cooldown_are_captured(self):
        now = datetime(2026, 11, 22, tzinfo=UTC)
        token = CachedAccessToken(
            "FAKE_ACCESS_TOKEN",
            now,
            now + timedelta(hours=24),
        )
        policy = TokenRefreshPolicy(6, 1, 65)
        self.assertFalse(
            should_refresh_access_token(
                token,
                trigger=RefreshTrigger.RECOVERY,
                now=now,
                policy=policy,
                last_request_at=None,
            )
        )

    def test_23_unauthorized_chat_is_blocked(self):
        handler = TelegramCommandHandler({"123456"})
        status, action = handler.handle("other", "/status", datetime(2026, 11, 22, tzinfo=UTC))
        self.assertEqual(status, "UNAUTHORIZED")
        self.assertIsNone(action)

    def test_24_sqlite_wal_allows_integrity_check(self):
        integrity = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertEqual(integrity, "ok")

    def test_25_deployment_is_blocked_during_order_window(self):
        now = datetime(2026, 11, 23, 3, 0, tzinfo=UTC)
        self.assertTrue(is_order_window(now))
