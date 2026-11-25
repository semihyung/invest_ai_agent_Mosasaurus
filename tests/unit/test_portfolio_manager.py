import unittest

from src.portfolio_manager import (
    SLOT_WEIGHTS,
    SlotCandidate,
    assign_risk_slots,
    build_weekly_preview,
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

    def test_live_successors_keep_region_and_underlying_concentration_limits(self):
        assignments = resolve_live_slots(
            preview_ranked_tickers=[
                SlotCandidate("A", "KR", "index-a"),
                SlotCandidate("B", "KR", "index-a"),
                SlotCandidate("C", "KR", "index-c"),
                SlotCandidate("D", "US", "index-d"),
            ],
            live_eligible_tickers={"A", "B", "C", "D"},
            parking_ticker="423160",
        )

        self.assertEqual(
            [assignment.ticker for assignment in assignments],
            ["A", "C", "D"],
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

    def test_builds_weekly_preview_and_avoids_trades_for_surviving_holdings(self):
        preview = build_weekly_preview(
            "2026-11-20",
            [
                SlotCandidate("A", "KR", "index-a"),
                SlotCandidate("B", "US", "index-b"),
                SlotCandidate("C", "KR", "index-c"),
                SlotCandidate("D", "US", "index-d"),
            ],
            parking_ticker="423160",
            existing_slot_tickers={
                "S1": "A",
                "S2": "D",
                "S3": "423160",
            },
        )

        self.assertEqual(
            [assignment.ticker for assignment in preview.projected_slots],
            ["A", "B", "C"],
        )
        self.assertEqual(
            [(trade.side, trade.slot_id, trade.ticker) for trade in preview.projected_trades],
            [
                ("SELL", "S2", "D"),
                ("BUY", "S2", "B"),
                ("BUY", "S3", "C"),
            ],
        )

    def test_weekly_preview_rejects_non_iso_date_and_parking_as_risk_candidate(self):
        with self.assertRaisesRegex(ValueError, "ISO date"):
            build_weekly_preview(
                "2026-2-3",
                [],
                parking_ticker="423160",
            )
        with self.assertRaisesRegex(ValueError, "parking ticker"):
            build_weekly_preview(
                "2026-11-20",
                [SlotCandidate("423160", "KR", "parking-index")],
                parking_ticker="423160",
            )

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

    def test_smart_dca_reserves_pending_buys_and_enforces_minimum_trade(self):
        below_minimum = calculate_smart_dca(
            cash_balance=2_000_000,
            total_equity=10_000_000,
            target_amount=4_600_000,
            current_amount=4_500_000,
            limit_price=100_000,
            available_cash=1_000_000,
            estimated_fee=0,
        )
        with_pending = calculate_smart_dca(
            cash_balance=2_000_000,
            total_equity=10_000_000,
            target_amount=4_800_000,
            current_amount=4_000_000,
            limit_price=100_000,
            available_cash=1_000_000,
            estimated_fee=0,
            pending_buy_amount=350_000,
        )

        self.assertEqual(below_minimum, 0)
        self.assertEqual(with_pending, 4)

    def test_smart_dca_checks_minimum_against_unreserved_target_gap(self):
        quantity = calculate_smart_dca(
            cash_balance=2_000_000,
            total_equity=10_000_000,
            target_amount=4_650_000,
            current_amount=4_500_000,
            limit_price=50_000,
            available_cash=1_000_000,
            estimated_fee=0,
            pending_buy_amount=100_000,
        )

        self.assertEqual(quantity, 1)


if __name__ == "__main__":
    unittest.main()


class PreviewUsesExecutionSlotRulesTests(unittest.TestCase):
    """감사 1번: 주말 프리뷰는 월요일 실행과 같은 슬롯 규칙(지역 비중 한도 등)을 보여 준다."""

    def test_global_candidate_over_region_weight_is_not_promised_a_slot(self):
        from src.portfolio_manager import SlotCandidate, build_weekly_preview, select_final_slots

        ranked = (SlotCandidate("G1", "GLOBAL", "IDX_G"), SlotCandidate("K1", "KR", "IDX_K"))
        simple = build_weekly_preview("2026-11-27", ranked, parking_ticker="P")
        self.assertEqual(simple.projected_slots[0].ticker, "G1")  # 단순 규칙은 GLOBAL 45% 를 약속한다
        real = build_weekly_preview(
            "2026-11-27", ranked, parking_ticker="P", slots=select_final_slots(ranked))
        self.assertEqual(real.projected_slots[0].ticker, "K1")  # 실행 규칙: GLOBAL 합계 30% 한도로 제외
