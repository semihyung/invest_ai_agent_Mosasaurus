import unittest
from datetime import datetime, timezone

from src.health_monitor import HealthSnapshot, is_healthy


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


if __name__ == "__main__":
    unittest.main()
