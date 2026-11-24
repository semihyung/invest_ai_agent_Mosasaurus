"""ops_jobs: 알림 전송기(토큰 마스킹·실패 무시)·일일 브리핑·주간 DB 백업을 검증합니다."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.db import connect_database, initialize_database
from src.ops_jobs import (
    build_notifier, compose_briefing, create_daily_briefing_handler, create_weekly_maintenance_handler,
)
from src.safety_flags import raise_flag

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 25, 18, 5, tzinfo=KST)


class NotifierTests(unittest.TestCase):
    def test_no_credentials_means_no_send(self):
        sent = []
        build_notifier({}, post=lambda url, payload: sent.append(payload))("INFO", "x")
        self.assertEqual(sent, [])

    def test_sends_prefixed_sanitized_message(self):
        sent = []
        notify = build_notifier({"TELEGRAM_BOT_TOKEN": "dummy-token", "ALLOWED_CHAT_ID": "42"},
                                post=lambda url, payload: sent.append((url, payload)))
        notify("CRITICAL", "계좌 1234567801 과 token=abc123 노출 금지")
        url, payload = sent[0]
        self.assertTrue(payload["text"].startswith("[모사사우르스] [CRITICAL]"))
        self.assertNotIn("1234567801", payload["text"])
        self.assertNotIn("abc123", payload["text"])
        self.assertEqual(payload["chat_id"], "42")

    def test_send_failure_is_swallowed_and_does_not_leak_token(self):
        def boom(url, payload):
            raise OSError(url)
        build_notifier({"TELEGRAM_BOT_TOKEN": "dummy-token", "ALLOWED_CHAT_ID": "42"}, post=boom)("INFO", "x")


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        self.conn = initialize_database(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def factory(self):
        return connect_database(self.path)


class BriefingTests(DbTestCase):
    def test_briefing_lists_halts_and_blocks_without_identifiers(self):
        raise_flag(self.conn, "HALT", "ACCOUNT_MISMATCH", now=NOW)
        raise_flag(self.conn, "BUY_BLOCK", "UNCLASSIFIED_CASH_FLOW", now=NOW)
        text = compose_briefing(self.conn, NOW)
        self.assertIn("ACCOUNT_MISMATCH", text)
        self.assertIn("UNCLASSIFIED_CASH_FLOW", text)
        self.assertIn("스냅샷 없음", text)

    def test_handler_records_event_and_notifies(self):
        sent = []
        create_daily_briefing_handler(self.factory, clock=lambda: NOW, notify=lambda s, m: sent.append(m))()
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM system_events WHERE event_type='DAILY_BRIEFING'").fetchone()[0], 1)


class MaintenanceTests(DbTestCase):
    def test_backup_is_created_verified_and_rotated(self):
        backup_dir = Path(self.tmp.name) / "backups"
        for day in range(10):
            when = NOW + timedelta(days=day)
            create_weekly_maintenance_handler(self.factory, backup_dir, clock=lambda w=when: w, keep=3)()
        files = sorted(backup_dir.glob("mosasaurus-*.db"))
        self.assertEqual(len(files), 3)
        copy = connect_database(files[-1])
        try:
            self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            copy.close()


if __name__ == "__main__":
    unittest.main()
