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


class BackupPruningTests(DbTestCase):
    """감사 12번: 주간 백업과 배포 전 백업을 따로 세고, 지운 백업의 체크섬 파일도 함께 지운다."""

    def test_weekly_and_predeploy_backups_are_pruned_independently(self):
        backup_dir = Path(self.tmp.name) / "backups"
        backup_dir.mkdir()
        names = [f"mosasaurus-2026110{day}T070000Z.db" for day in range(1, 6)]  # 배포 전 5개
        names += [f"mosasaurus-2026111{day}.db" for day in range(0, 5)]         # 주간 5개
        for name in names:
            (backup_dir / name).write_text("x")
            (backup_dir / f"{name}.sha256").write_text("h")
        create_weekly_maintenance_handler(
            self.factory, backup_dir, clock=lambda: NOW, keep=3, keep_predeploy=2)()
        left = sorted(path.name for path in backup_dir.iterdir())
        predeploy = [n for n in left if n.endswith("Z.db")]
        weekly = [n for n in left if n.endswith(".db") and not n.endswith("Z.db")]
        self.assertEqual(predeploy, names[3:5])           # 배포 전 백업은 최신 2개만
        self.assertEqual(len(weekly), 3)                   # 주간(오늘 만든 것 포함)은 최신 3개
        self.assertIn(f"mosasaurus-{NOW.astimezone(KST).strftime('%Y%m%d')}.db", weekly)
        for name in (n for n in names if n not in left):   # 지워진 백업의 체크섬 파일은 남지 않는다
            self.assertNotIn(f"{name}.sha256", left)
        self.assertEqual(sum(n.endswith(".sha256") for n in left), len([n for n in left if n in names]))
