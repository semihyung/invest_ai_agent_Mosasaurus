import unittest
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path

from src.db import connect_database, initialize_database
from src.telegram_bot import (
    PendingActionStore,
    TelegramCommandHandler,
    _acquire_polling_lock,
    load_telegram_settings,
    mask_secret,
    sanitize_telegram_reply,
)
from src.universe import (
    ProductMetadata,
    has_approved_classification,
    product_metadata_fingerprint,
)


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
        _, action = self.handler.handle("123456", "/emergency", self.now)

        result, confirmed = self.handler.handle(
            "123456",
            f"/confirm {action.action_id}",
            self.now + timedelta(minutes=5),
        )

        self.assertEqual(result, "CONFIRMATION_EXPIRED")
        self.assertIsNone(confirmed)

    def test_add_is_blocked_without_official_metadata_and_persistent_store(self):
        result, action = self.handler.handle("123456", "/add 123450", self.now)

        self.assertEqual(result, "CLASSIFICATION_UNAVAILABLE")
        self.assertIsNone(action)

    def test_add_rejects_leveraged_official_metadata(self):
        metadata = ProductMetadata(
            ticker="123450",
            name="Sample ETF",
            region="KR",
            underlying_index="Sample Index",
            product_type="SPOT_ETF",
            replication_method="PHYSICAL",
            leverage_multiplier=2.0,
            is_inverse=False,
            is_futures_based=False,
            classification_source="KIS_PRODUCT_MASTER",
        )
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "mosasaurus.db"
            connection = initialize_database(db_path)
            connection.close()
            handler = TelegramCommandHandler(
                {"123456"},
                pending_store=PendingActionStore(
                    lambda: connect_database(db_path)
                ),
                product_metadata_lookup=lambda _ticker: metadata,
            )
            result, action = handler.handle("123456", "/add 123450", self.now)

        self.assertEqual(result, "INELIGIBLE_PRODUCT")
        self.assertIsNone(action)

    def test_unknown_command_is_rejected(self):
        result, action = self.handler.handle("123456", "/unknown", self.now)

        self.assertEqual(result, "UNKNOWN_COMMAND")
        self.assertIsNone(action)

    def test_phase_two_command_and_invalid_ticker_are_not_accepted(self):
        """Phase 2 명령과 잘못된 티커 인자를 거부하는지 확인합니다."""
        override = self.handler.handle("123456", "/override 123450", self.now)
        invalid_ticker = self.handler.handle("123456", "/add 12345", self.now)

        self.assertEqual(override[0], "UNKNOWN_COMMAND")
        self.assertEqual(invalid_ticker[0], "INVALID_ARGUMENTS")

    def test_confirmation_is_persisted_and_add_ticker_does_not_break_fk(self):
        """2단계 확인 이력과 유니버스 편입 전 티커 저장을 검증합니다."""
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "mosasaurus.db"
            connection = initialize_database(db_path)
            connection.close()
            connection_factory = lambda: connect_database(db_path)
            store = PendingActionStore(connection_factory)
            metadata = ProductMetadata(
                ticker="654321",
                name="Sample ETF",
                region="KR",
                underlying_index="Sample Index",
                product_type="SPOT_ETF",
                replication_method="PHYSICAL",
                leverage_multiplier=1.0,
                is_inverse=False,
                is_futures_based=False,
                classification_source="KIS_PRODUCT_MASTER",
            )
            handler = TelegramCommandHandler(
                {"123456"},
                pending_store=store,
                product_metadata_lookup=lambda _ticker: metadata,
            )

            status, action = handler.handle("123456", "/add 654321", self.now)
            confirmed, _ = handler.handle(
                "123456",
                f"/confirm {action.action_id}",
                self.now + timedelta(minutes=1),
            )
            connection = connection_factory()
            try:
                row = connection.execute(
                    "SELECT action_type, ticker, reason, approval_status "
                    "FROM pending_actions WHERE id = ?",
                    (action.action_id,),
                ).fetchone()
                audit_count = connection.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE target = ?",
                    (action.action_id,),
                ).fetchone()[0]
                asset = connection.execute(
                    "SELECT * FROM universe_assets WHERE ticker = '654321'"
                ).fetchone()
            finally:
                connection.close()

            approval_connection = connection_factory()
            try:
                approval_is_valid = has_approved_classification(
                    approval_connection,
                    asset,
                    allowlist_by_ticker={},
                )
            finally:
                approval_connection.close()
            approval_connection = connection_factory()
            try:
                approval_connection.execute(
                    "UPDATE universe_assets SET name = 'Changed ETF' "
                    "WHERE ticker = '654321'"
                )
                approval_connection.commit()
                changed_asset = approval_connection.execute(
                    "SELECT * FROM universe_assets WHERE ticker = '654321'"
                ).fetchone()
                changed_approval_is_valid = has_approved_classification(
                    approval_connection,
                    changed_asset,
                    allowlist_by_ticker={},
                )
            finally:
                approval_connection.close()

            self.assertEqual(status, "CONFIRMATION_REQUIRED")
            self.assertIn("Sample ETF", action.confirmation_summary)
            self.assertIn("KIS_PRODUCT_MASTER", action.confirmation_summary)
            self.assertEqual(confirmed, "CONFIRMED")
            self.assertEqual(tuple(row), (
                "UNIVERSE_ADD",
                "654321",
                "TWO_STEP_CONFIRMATION;TICKER=654321;METADATA_SHA256="
                + product_metadata_fingerprint(metadata, category="RISK_ASSET"),
                "APPROVED",
            ))
            self.assertEqual(audit_count, 2)
            self.assertTrue(approval_is_valid)
            self.assertFalse(changed_approval_is_valid)

    def test_polling_lock_rejects_second_process(self):
        """두 번째 Telegram polling 프로세스가 잠금을 얻지 못하는지 확인합니다."""
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "mosasaurus.db"
            connection = initialize_database(db_path)
            connection.close()
            connection_factory = lambda: connect_database(db_path)

            _acquire_polling_lock(connection_factory, owner_id="first")
            with self.assertRaisesRegex(RuntimeError, "already locked"):
                _acquire_polling_lock(connection_factory, owner_id="second")

    def test_telegram_reply_masks_tokens_and_long_account_numbers(self):
        """Telegram 응답에서 민감한 값이 마스킹되는지 확인합니다."""
        reply = sanitize_telegram_reply(
            "access_token=secret-value account_no=123456789012"
        )

        self.assertNotIn("secret-value", reply)
        self.assertNotIn("123456789012", reply)

    def test_telegram_settings_load_only_from_dummy_environment(self):
        """더미 환경 변수만으로 봇 토큰과 허용 채팅을 읽습니다."""
        token, allowed = load_telegram_settings(
            {
                "TELEGRAM_BOT_TOKEN": "dummy-token",
                "ALLOWED_CHAT_ID": "123456,654321",
            }
        )

        self.assertEqual(token, "dummy-token")
        self.assertEqual(allowed, {"123456", "654321"})

    def test_telegram_settings_reject_non_numeric_chat_ids(self):
        """허용 채팅 ID가 숫자가 아니면 설정을 거부합니다."""
        with self.assertRaisesRegex(ValueError, "numeric IDs"):
            load_telegram_settings(
                {
                    "TELEGRAM_BOT_TOKEN": "dummy-token",
                    "ALLOWED_CHAT_ID": "not-a-chat",
                }
            )

    def test_mask_secret_preserves_only_first_two_characters(self):
        self.assertEqual(mask_secret("ABSECRET"), "AB****")
        self.assertEqual(mask_secret("A"), "****")


if __name__ == "__main__":
    unittest.main()
