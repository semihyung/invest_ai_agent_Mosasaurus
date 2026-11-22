import unittest
from datetime import datetime, timedelta, timezone

from src.telegram_bot import TelegramCommandHandler, mask_secret


UTC = timezone.utc


class TelegramBotTests(unittest.TestCase):
    def setUp(self):
        self.handler = TelegramCommandHandler({"123456"})
        self.now = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)

    def test_rejects_unapproved_chat(self):
        result, action = self.handler.handle("999999", "/status", self.now)

        self.assertEqual(result, "UNAUTHORIZED")
        self.assertIsNone(action)

    def test_known_read_command_executes_immediately(self):
        result, action = self.handler.handle("123456", "/status", self.now)

        self.assertEqual(result, "EXECUTE")
        self.assertIsNone(action)

    def test_emergency_command_requires_confirmation(self):
        result, action = self.handler.handle("123456", "/emergency", self.now)

        self.assertEqual(result, "CONFIRMATION_REQUIRED")
        self.assertIsNotNone(action)
        confirm_result, confirmed_action = self.handler.handle(
            "123456",
            f"/confirm {action.action_id}",
            self.now + timedelta(minutes=1),
        )
        self.assertEqual(confirm_result, "CONFIRMED")
        self.assertEqual(confirmed_action, action)

    def test_confirmation_expires_after_five_minutes(self):
        _, action = self.handler.handle("123456", "/add 123450", self.now)

        result, confirmed = self.handler.handle(
            "123456",
            f"/confirm {action.action_id}",
            self.now + timedelta(minutes=5),
        )

        self.assertEqual(result, "CONFIRMATION_EXPIRED")
        self.assertIsNone(confirmed)

    def test_unknown_command_is_rejected(self):
        result, action = self.handler.handle("123456", "/unknown", self.now)

        self.assertEqual(result, "UNKNOWN_COMMAND")
        self.assertIsNone(action)

    def test_mask_secret_preserves_only_first_two_characters(self):
        self.assertEqual(mask_secret("ABSECRET"), "AB****")
        self.assertEqual(mask_secret("A"), "****")


if __name__ == "__main__":
    unittest.main()
