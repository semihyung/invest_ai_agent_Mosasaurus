import unittest

from src.portfolio_manager import (
    SlotCandidate,
    assign_risk_slots,
    build_weekly_preview,
    select_final_slots,
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

    def test_live_successor_replaces_failed_top_candidate(self):
        # 라이브 탈락 후보는 호출 전에 걷어 내므로, 남은 순위 후보가 차례로 슬롯을 승계한다.
        assignments = select_final_slots(
            [SlotCandidate("B", "KR", "b"), SlotCandidate("C", "US", "c"), SlotCandidate("D", "KR", "d")],
            held_tickers={"D"},
        )

        self.assertEqual([a.ticker for a in assignments], ["B", "C", "D"])

    def test_live_successors_keep_underlying_concentration_limits(self):
        assignments = select_final_slots(
            [
                SlotCandidate("A", "KR", "index-a"),
                SlotCandidate("B", "KR", "index-a"),
                SlotCandidate("C", "KR", "index-c"),
                SlotCandidate("D", "US", "index-d"),
            ]
        )

        self.assertNotIn("B", [a.ticker for a in assignments])

    def test_insufficient_live_candidates_fill_with_parking(self):
        assignments = select_final_slots([SlotCandidate("A", "KR", "a")])

        self.assertEqual([a.ticker for a in assignments], ["A", "", ""])
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
