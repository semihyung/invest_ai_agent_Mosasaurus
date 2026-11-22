import unittest
from datetime import datetime, timezone

from src.scheduler import ScheduleJob, is_order_window, jobs_due_now


UTC = timezone.utc


class SchedulerTests(unittest.TestCase):
    def test_weekly_preview_is_due_only_at_saturday_10(self):
        saturday = datetime(2026, 11, 28, 1, 0, tzinfo=UTC)
        friday = datetime(2026, 11, 27, 1, 0, tzinfo=UTC)

        self.assertIn(
            ScheduleJob.WEEKLY_RANKING_PREVIEW,
            jobs_due_now(saturday, weekday_is_open=True),
        )
        self.assertNotIn(
            ScheduleJob.WEEKLY_RANKING_PREVIEW,
            jobs_due_now(friday, weekday_is_open=True),
        )

    def test_morning_prep_is_not_due_on_weekend(self):
        saturday = datetime(2026, 11, 27, 23, 30, tzinfo=UTC)

        self.assertNotIn(
            ScheduleJob.MORNING_PREP,
            jobs_due_now(saturday, weekday_is_open=False),
        )

    def test_open_jobs_are_due_only_when_market_is_open(self):
        now = datetime(2026, 11, 23, 6, 0, tzinfo=UTC)

        self.assertIn(
            ScheduleJob.DAILY_PIPELINE,
            jobs_due_now(now, weekday_is_open=True),
        )
        self.assertNotIn(
            ScheduleJob.DAILY_PIPELINE,
            jobs_due_now(now, weekday_is_open=False),
        )

    def test_order_window_matches_deployment_guard_hours(self):
        in_window = datetime(2026, 11, 23, 3, 0, tzinfo=UTC)
        outside_window = datetime(2026, 11, 22, 23, 0, tzinfo=UTC)

        self.assertTrue(is_order_window(in_window))
        self.assertFalse(is_order_window(outside_window))


if __name__ == "__main__":
    unittest.main()
