import unittest

from src.portfolio_manager import (
    SLOT_WEIGHTS,
    SlotCandidate,
    assign_risk_slots,
    calculate_smart_dca,
    calculate_target_amounts,
    detect_external_deposit,
    resolve_live_slots,
)


class PortfolioManagerTests(unittest.TestCase):
    def test_assigns_45_30_20_slots_with_concentration_limits(self):
        assignments = assign_risk_slots(
            [
                SlotCandidate("A", "KR", "index-a"),
                SlotCandidate("B", "KR", "index-b"),
                SlotCandidate("C", "US", "index-c"),
                SlotCandidate("D", "US", "index-d"),
            ]
        )

        self.assertEqual(
            [assignment.ticker for assignment in assignments],
            ["A", "B", "C"],
        )
        self.assertEqual(
            [assignment.target_weight for assignment in assignments],
            [0.45, 0.30, 0.20],
        )

    def test_rejects_duplicate_underlying_index(self):
        assignments = assign_risk_slots(
            [
                SlotCandidate("A", "KR", "same-index"),
                SlotCandidate("B", "KR", "same-index"),
                SlotCandidate("C", "US", "other-index"),
            ]
        )

        self.assertEqual(
            [assignment.ticker for assignment in assignments],
            ["A", "C", ""],
        )

    def test_live_resolution_skips_top_candidate_and_successor_survives(self):
        assignments = resolve_live_slots(
            preview_ranked_tickers=["A", "B", "C", "D"],
            live_eligible_tickers={"B", "C", "D"},
            parking_ticker="423160",
            existing_holdings={"D"},
        )

        self.assertEqual(
            [assignment.ticker for assignment in assignments],
            ["B", "C", "D"],
        )

    def test_insufficient_live_candidates_fill_with_parking(self):
        assignments = resolve_live_slots(
            preview_ranked_tickers=["A", "B"],
            live_eligible_tickers={"A"},
            parking_ticker="423160",
        )

        self.assertEqual(
            [assignment.ticker for assignment in assignments],
            ["A", "423160", "423160"],
        )
        self.assertEqual(assignments[1].reason, "parking_fallback")

    def test_target_amounts_reserve_5_percent_as_parking(self):
        targets = calculate_target_amounts(
            total_equity=10_000_000,
            parking_ticker="423160",
        )

        self.assertEqual(targets["423160"], 500_000)
        self.assertEqual(
            sum(targets.values()),
            10_000_000,
        )
        self.assertEqual(SLOT_WEIGHTS["S1"], 0.45)

    def test_detect_external_deposit_uses_net_cash_formula(self):
        net_deposit = detect_external_deposit(
            current_cash=1_600_000,
            prior_cash=1_000_000,
            sell_fills=100_000,
            buy_fills=50_000,
            fees=10_000,
        )

        self.assertEqual(net_deposit, 560_000)

    def test_smart_dca_preserves_cash_buffer(self):
        quantity = calculate_smart_dca(
            cash_balance=1_000_000,
            total_equity=10_000_000,
            target_amount=4_500_000,
            current_amount=4_000_000,
            limit_price=100_000,
            available_cash=900_000,
            estimated_fee=10_000,
        )

        self.assertEqual(quantity, 3)

    def test_smart_dca_blocked_below_deposit_threshold(self):
        quantity = calculate_smart_dca(
            cash_balance=600_000,
            total_equity=10_000_000,
            target_amount=4_500_000,
            current_amount=4_000_000,
            limit_price=100_000,
            available_cash=600_000,
            estimated_fee=10_000,
        )

        self.assertEqual(quantity, 0)


if __name__ == "__main__":
    unittest.main()
