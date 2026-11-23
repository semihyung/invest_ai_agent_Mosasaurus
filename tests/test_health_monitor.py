import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.db import connect_database, initialize_database
from src.health_monitor import (
    HealthSnapshot,
    is_healthy,
    probe_database,
    record_heartbeat,
    refresh_watchdog_if_healthy,
)


UTC = timezone.utc


class HealthMonitorTests(unittest.TestCase):
    def test_healthy_snapshot(self):
        snapshot = HealthSnapshot(
            database_ok=True,
            heartbeat_ok=True,
            token_valid=True,
            reconciliation_ok=True,
            cpu_percent=40,
            disk_percent=50,
            temperature_c=55,
            checked_at=datetime(2026, 11, 22, 10, 0, tzinfo=UTC),
        )

        self.assertTrue(is_healthy(snapshot))

    def test_reconciliation_failure_marks_unhealthy(self):
        snapshot = HealthSnapshot(
            database_ok=True,
            heartbeat_ok=True,
            token_valid=True,
            reconciliation_ok=False,
            cpu_percent=40,
            disk_percent=50,
            temperature_c=55,
            checked_at=datetime(2026, 11, 22, 10, 0, tzinfo=UTC),
        )

        self.assertFalse(is_healthy(snapshot))

    def test_cpu_disk_and_temperature_limits(self):
        snapshot = HealthSnapshot(
            database_ok=True,
            heartbeat_ok=True,
            token_valid=True,
            reconciliation_ok=True,
            cpu_percent=96,
            disk_percent=91,
            temperature_c=86,
            checked_at=datetime(2026, 11, 22, 10, 0, tzinfo=UTC),
        )

        self.assertFalse(is_healthy(snapshot))

    def test_database_probe_and_heartbeat(self):
        """SQLite 읽기·쓰기 검사와 heartbeat 기록을 확인합니다."""
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "mosasaurus.db"
            connection = initialize_database(db_path)
            connection.close()

            self.assertTrue(probe_database(lambda: connect_database(db_path)))
            connection = connect_database(db_path)
            try:
                record_heartbeat(
                    connection,
                    now=datetime(2026, 11, 22, 10, 0, tzinfo=UTC),
                )
                heartbeat = connection.execute(
                    'SELECT value FROM system_state WHERE "key" = ?',
                    ("scheduler_heartbeat",),
                ).fetchone()
                self.assertEqual(heartbeat[0], "2026-11-22T10:00:00+00:00")
            finally:
                connection.close()

    def test_watchdog_is_not_refreshed_when_any_gate_fails(self):
        """계좌 대조가 실패하면 워치독 콜백을 실행하지 않는지 확인합니다."""
        snapshot = HealthSnapshot(
            database_ok=True,
            heartbeat_ok=True,
            token_valid=True,
            reconciliation_ok=False,
            cpu_percent=40,
            disk_percent=50,
            temperature_c=55,
            checked_at=datetime(2026, 11, 22, 10, 0, tzinfo=UTC),
        )
        calls = []

        result = refresh_watchdog_if_healthy(
            snapshot,
            refresh_watchdog=lambda: calls.append("refreshed"),
        )

        self.assertFalse(result)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
