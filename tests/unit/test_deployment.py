import unittest
from datetime import datetime, timezone

from src.deployment import is_deployment_blocked


UTC = timezone.utc


class DeploymentTests(unittest.TestCase):
    def test_blocks_at_0850_kst(self):
        now = datetime(2026, 11, 22, 23, 50, tzinfo=UTC)

        self.assertTrue(is_deployment_blocked(now))

    def test_blocks_at_1525_kst(self):
        now = datetime(2026, 11, 22, 6, 25, tzinfo=UTC)

        self.assertTrue(is_deployment_blocked(now))

    def test_allows_before_0850_kst(self):
        now = datetime(2026, 11, 22, 23, 49, tzinfo=UTC)

        self.assertFalse(is_deployment_blocked(now))

    def test_allows_after_1525_kst(self):
        now = datetime(2026, 11, 22, 6, 26, tzinfo=UTC)

        self.assertFalse(is_deployment_blocked(now))

    def test_requires_timezone_aware_datetime(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            is_deployment_blocked(datetime(2026, 11, 22, 10, 0))


if __name__ == "__main__":
    unittest.main()
