"""시나리오 60: Telegram 확인 코드는 단일 사용·행위 결합·해시 저장되고, 다른 사용자·만료·5회 실패 후 잠금이 거부됩니다."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.db import connect_database, initialize_database
from src.telegram_bot import PendingActionStore, TelegramCommandHandler

T0 = datetime(2026, 11, 25, 1, 0, tzinfo=timezone.utc)
CHAT = "1001"


class ConfirmationSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        initialize_database(self.path).close()
        self.factory = lambda: connect_database(self.path)
        self.handler = TelegramCommandHandler(
            {CHAT}, pending_store=PendingActionStore(self.factory), allowed_user_ids={"2001", "2002"})
        self.clock = T0

    def tearDown(self):
        self.tmp.cleanup()

    def say(self, text, user="2001", step=11):
        """명령을 보낸다(분당 속도 제한을 피하려고 매번 11초씩 진행)."""
        self.clock += timedelta(seconds=step)
        return self.handler.handle(CHAT, text, self.clock, user_id=user)

    def request_code(self):
        status, action = self.say("/resume")
        self.assertEqual(status, "CONFIRMATION_REQUIRED")
        return action.confirm_code

    def test_code_is_single_use(self):
        code = self.request_code()
        self.assertEqual(self.say(f"/confirm {code.lower()}")[0], "CONFIRMED")
        self.assertEqual(self.say(f"/confirm {code.lower()}")[0], "CONFIRMATION_NOT_FOUND")

    def test_code_is_bound_to_the_requesting_user(self):
        code = self.request_code()
        self.assertEqual(self.say(f"/confirm {code.lower()}", user="2002")[0], "CONFIRMATION_NOT_FOUND")
        self.assertEqual(self.say(f"/confirm {code.lower()}", user="2001")[0], "CONFIRMED")

    def test_unlisted_user_is_ignored(self):
        self.assertEqual(self.say("/status", user="9999")[0], "UNAUTHORIZED")

    def test_code_is_bound_to_the_requested_action_and_stored_as_a_hash(self):
        status, action = self.say("/resume")
        code = action.confirm_code
        self.assertEqual(action.command, "/resume")
        connection = self.factory()
        try:
            rows = connection.execute("SELECT action_type, code_hash, args_hash FROM pending_actions").fetchall()
        finally:
            connection.close()
        self.assertEqual(rows[0]["action_type"], "RESUME")
        self.assertNotIn(code, "".join(str(value) for value in tuple(rows[0])))   # 평문 코드는 DB 에 없다
        self.assertEqual(len(rows[0]["code_hash"]), 64)

    def test_expired_code_is_rejected_after_five_minutes(self):
        code = self.request_code()
        self.assertEqual(self.say(f"/confirm {code.lower()}", step=301)[0], "CONFIRMATION_EXPIRED")

    def test_five_wrong_codes_lock_all_two_step_commands_for_thirty_minutes(self):
        code = self.request_code()
        for _ in range(5):
            self.assertEqual(self.say("/confirm aaaaaaaa")[0], "CONFIRMATION_NOT_FOUND")
        self.assertEqual(self.say(f"/confirm {code.lower()}")[0], "CONFIRMATIONS_LOCKED")   # 옳은 코드도 잠김
        self.assertEqual(self.say("/resume")[0], "CONFIRMATIONS_LOCKED")                     # 새 2단계 요청도 잠김
        status, action = self.say("/resume", step=1801)
        self.assertEqual(status, "CONFIRMATION_REQUIRED")                                    # 30분 뒤 풀림


if __name__ == "__main__":
    unittest.main()
