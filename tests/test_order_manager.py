import unittest

from src.order_manager import (
    OrderIntent,
    OrderSide,
    OrderStatus,
    Quote,
    allows_buy_submission,
    calculate_limit_price,
    transition_order_status,
    validate_spread_and_nav,
)


class OrderManagerTests(unittest.TestCase):
    def test_idempotency_key_is_deterministic(self):
        intent = OrderIntent(
            trade_date="2026-11-22",
            run_id="run-1",
            slot_id="S1",
            ticker="123450",
            side=OrderSide.BUY,
            quantity=1,
        )

        self.assertEqual(
            intent.idempotency_key(),
            "2026-11-22:run-1:S1:123450:BUY:1",
        )

    def test_valid_transition_is_allowed(self):
        self.assertEqual(
            transition_order_status(
                OrderStatus.PENDING,
                OrderStatus.SUBMITTING,
            ),
            OrderStatus.SUBMITTING,
        )

    def test_timeout_uses_unknown_and_cannot_transition_to_submitted(self):
        unknown = transition_order_status(
            OrderStatus.SUBMITTING,
            OrderStatus.UNKNOWN,
        )
        self.assertEqual(unknown, OrderStatus.UNKNOWN)
        with self.assertRaisesRegex(ValueError, "Invalid order transition"):
            transition_order_status(unknown, OrderStatus.SUBMITTED)

    def test_unknown_to_accepted_requires_lookup_recovery(self):
        self.assertEqual(
            transition_order_status(
                OrderStatus.UNKNOWN,
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

    def test_emergency_sell_floor_is_applied(self):
        quote = Quote(bid=10000, ask=10010, nav=10050.0)

        self.assertEqual(
            calculate_limit_price(
                quote,
                OrderSide.SELL,
                emergency_sell=True,
                tick=10,
            ),
            9980,
        )

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
