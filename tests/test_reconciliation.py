import unittest

from src.reconciliation import (
    Position,
    reconcile_positions,
    reconciliation_safe_halt,
)


class ReconciliationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
