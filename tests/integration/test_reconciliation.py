import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.reconciliation import (
    OrderSnapshot,
    Position,
    persist_reconciliation_result,
    reconcile_account_state,
    reconcile_orders,
    reconcile_positions,
    reconciliation_safe_halt,
)
from src.db import initialize_database


from src.safety_flags import active_halt_reasons

UTC = timezone.utc


class ReconciliationTests(unittest.TestCase):
    """잔고·주문 차이와 SAFE_HALT 영속화를 확인합니다."""

    def setUp(self):
        """안전정지 플래그를 검증할 격리 테스트 DB를 준비합니다."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.connection = initialize_database(
            Path(self.temp_dir.name) / "mosasaurus.db"
        )
        self.addCleanup(self.connection.close)

    def test_matching_positions_are_safe(self):
        result = reconcile_positions(
            [Position("423160", 10)],
            [Position("423160", 10)],
        )

        self.assertTrue(result.reconciliation_ok)
        self.assertEqual(result.mismatches, ())
        self.assertFalse(
            reconciliation_safe_halt(
                position_result=result,
                unknown_order_exists=False,
                stale_account_data=False,
            )
        )

    def test_quantity_mismatch_triggers_safe_halt(self):
        result = reconcile_positions(
            [Position("423160", 10), Position("123450", 1)],
            [Position("423160", 9)],
        )

        self.assertFalse(result.reconciliation_ok)
        self.assertEqual(set(result.mismatches), {"423160", "123450"})
        self.assertTrue(
            reconciliation_safe_halt(
                position_result=result,
                unknown_order_exists=False,
                stale_account_data=False,
            )
        )

    def test_unknown_order_or_stale_data_triggers_safe_halt(self):
        matching = reconcile_positions([], [])

        self.assertTrue(
            reconciliation_safe_halt(
                position_result=matching,
                unknown_order_exists=True,
                stale_account_data=False,
            )
        )
        self.assertTrue(
            reconciliation_safe_halt(
                position_result=matching,
                unknown_order_exists=False,
                stale_account_data=True,
            )
        )

    def test_duplicate_position_tickers_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate position"):
            reconcile_positions(
                [Position("423160", 1), Position("423160", 2)],
                [],
            )

    def test_reconciles_order_status_quantities_and_missing_orders(self):
        broker = [
            OrderSnapshot(
                "KIS-1",
                "423160",
                "BUY",
                requested_qty=3,
                filled_qty=1,
                remaining_qty=2,
                status="PARTIALLY_FILLED",
            )
        ]
        local = [
            OrderSnapshot(
                "KIS-1",
                "423160",
                "BUY",
                requested_qty=3,
                filled_qty=0,
                remaining_qty=3,
                status="ACCEPTED",
            ),
            OrderSnapshot(
                "LOCAL-UNKNOWN",
                "423160",
                "SELL",
                requested_qty=1,
                filled_qty=0,
                remaining_qty=1,
                status="PENDING_CONFIRMATION",
            ),
        ]

        self.assertEqual(
            reconcile_orders(broker, local),
            ("KIS-1", "LOCAL-UNKNOWN"),
        )
        result = reconcile_account_state(
            broker_positions=[Position("423160", 5)],
            sqlite_positions=[Position("423160", 5)],
            broker_orders=broker,
            sqlite_orders=local,
            account_data_fresh=True,
        )
        self.assertFalse(result.reconciliation_ok)
        self.assertIn("ORDER_MISMATCH", result.failure_codes)

    def test_unclassified_cash_flow_and_stale_account_fail_closed(self):
        stale = reconcile_account_state(
            broker_positions=[],
            sqlite_positions=[],
            broker_orders=[],
            sqlite_orders=[],
            account_data_fresh=False,
        )
        unknown_flow = reconcile_account_state(
            broker_positions=[],
            sqlite_positions=[],
            broker_orders=[],
            sqlite_orders=[],
            account_data_fresh=True,
            unclassified_cash_flow_exists=True,
        )

        self.assertIn("STALE_ACCOUNT_DATA", stale.failure_codes)
        self.assertIn("UNCLASSIFIED_CASH_FLOW", unknown_flow.failure_codes)

    def test_mismatch_persists_safe_halt_and_matching_result_does_not_clear_it(self):
        mismatch = reconcile_account_state(
            broker_positions=[Position("423160", 2)],
            sqlite_positions=[Position("423160", 1)],
            broker_orders=[],
            sqlite_orders=[],
            account_data_fresh=True,
        )
        now = datetime(2026, 11, 23, 6, 0, tzinfo=UTC)
        # v3.7.5: 첫 관측은 매수 대기(PROVISIONAL)만 만들고 SAFE_HALT 는 만들지 않는다.
        self.assertEqual(
            persist_reconciliation_result(self.connection, mismatch, checked_at=now), "PROVISIONAL")
        self.assertEqual(active_halt_reasons(self.connection), ())
        # 체결이 진행 중이면(quiescent=False) 판정을 건너뛴다.
        self.assertEqual(
            persist_reconciliation_result(
                self.connection, mismatch, checked_at=now + timedelta(seconds=120), quiescent=False),
            "SKIPPED")
        self.assertEqual(active_halt_reasons(self.connection), ())
        # 간격(60초) 안의 재관측은 그대로 PROVISIONAL, 간격 뒤 같은 불일치가 재현돼야 확정된다.
        self.assertEqual(
            persist_reconciliation_result(
                self.connection, mismatch, checked_at=now + timedelta(seconds=30)), "PROVISIONAL")
        self.assertEqual(
            persist_reconciliation_result(
                self.connection, mismatch, checked_at=now + timedelta(seconds=61)), "CONFIRMED")
        self.assertEqual(active_halt_reasons(self.connection), ("ACCOUNT_MISMATCH",))
        now = now + timedelta(seconds=61)

        matching = reconcile_account_state(
            broker_positions=[],
            sqlite_positions=[],
            broker_orders=[],
            sqlite_orders=[],
            account_data_fresh=True,
        )
        persist_reconciliation_result(
            self.connection,
            matching,
            checked_at=now,
        )
        # 수동 해제 원인이므로 대조가 일치해도 자동으로 풀리지 않습니다.
        self.assertEqual(active_halt_reasons(self.connection), ("ACCOUNT_MISMATCH",))


if __name__ == "__main__":
    unittest.main()
