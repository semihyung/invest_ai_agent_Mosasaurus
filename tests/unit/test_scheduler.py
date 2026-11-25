import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from apscheduler.events import EVENT_JOB_MISSED

from src.db import connect_database, initialize_database
from src.safety_flags import raise_flag
from src.scheduler import (
    KST,
    ScheduleJob,
    _guarded_job,
    _make_event_listener,
    build_scheduler,
    is_order_scheduler_locked,
    is_order_window,
    jobs_due_now,
    set_order_scheduler_lock,
)


UTC = timezone.utc


class SchedulerTests(unittest.TestCase):
    """명세서의 예약 시각과 지연 시 안전 잠금을 검증합니다."""

    def setUp(self):
        """각 테스트를 격리된 SQLite DB로 실행합니다."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "mosasaurus.db"
        connection = initialize_database(self.db_path)
        connection.close()

    def _connection_factory(self):
        """초기화된 테스트 DB에 새 연결을 반환합니다."""
        return connect_database(self.db_path)

    def _handlers(self):
        """모든 명세서 작업에 대한 비활성 테스트 콜백을 반환합니다."""
        return {job: lambda: None for job in ScheduleJob
                if job not in (ScheduleJob.BOOT_RECOVERY, ScheduleJob.PENDING_CONFIRMATION_POLLER)}

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

    def test_build_scheduler_registers_all_jobs_and_exact_grace_periods(self):
        """예약 작업을 모두 등록하고 마감 민감 작업의 지연 한도를 적용합니다."""
        scheduler = build_scheduler(
            self._handlers(),
            connection_factory=self._connection_factory,
            market_is_open=lambda: True,
        )

        self.assertEqual(len(scheduler.get_jobs()), 10)
        # 명세 9.1절: 안전 점검은 장 시간과 무관한 자체 10분 잡이다.
        safety_job = scheduler.get_job(ScheduleJob.SAFETY_FLAG_AUTO_CLEAR.value)
        self.assertEqual(safety_job.trigger.interval.total_seconds(), 600)
        self.assertEqual(safety_job.misfire_grace_time, 60)
        self.assertEqual(
            scheduler.get_job(ScheduleJob.ORDER_CANCEL_SWEEP.value).misfire_grace_time,
            15,
        )
        self.assertEqual(
            scheduler.get_job(ScheduleJob.DAILY_PIPELINE.value).misfire_grace_time,
            60,
        )
        self.assertEqual(
            scheduler.get_job(ScheduleJob.INTRADAY_GUARD.value)
            .trigger.interval.total_seconds(),
            10,
        )
        cancel_job = scheduler.get_job(ScheduleJob.ORDER_CANCEL_SWEEP.value)
        next_cancel = cancel_job.trigger.get_next_fire_time(
            None,
            datetime(2026, 11, 23, 15, 14, 29, tzinfo=KST),
        )
        self.assertEqual(next_cancel.time().replace(tzinfo=None), datetime(2026, 11, 23, 15, 14, 30).time())

    def test_scheduler_rejects_missing_job_handler(self):
        """콜백이 빠진 채로 예약 작업이 성공한 것처럼 시작되는 것을 막습니다."""
        handlers = self._handlers()
        del handlers[ScheduleJob.DAILY_PIPELINE]

        with self.assertRaisesRegex(ValueError, "Missing scheduler handlers"):
            build_scheduler(
                handlers,
                connection_factory=self._connection_factory,
                market_is_open=lambda: True,
            )

    def test_scheduler_lock_cannot_clear_during_safe_halt(self):
        """SAFE_HALT가 설정된 계좌는 대조 성공 플래그만으로 잠금을 풀 수 없습니다."""
        connection = self._connection_factory()
        try:
            connection.execute(
                'INSERT INTO system_state ("key", value, updated_at) VALUES '
                "('scheduler_order_lock', 'true', '2026-11-23T06:00:00+00:00'), "
                "('reconciliation_ok', 'true', '2026-11-23T06:00:00+00:00')"
            )
            raise_flag(
                connection, "HALT", "ACCOUNT_MISMATCH",
                now=datetime(2026, 11, 23, 6, 0, tzinfo=UTC),
            )
            with self.assertRaises(PermissionError):
                set_order_scheduler_lock(
                    connection,
                    locked=False,
                    reason="RECONCILIATION_REQUIRED",
                    occurred_at=datetime(2026, 11, 23, 6, 0, tzinfo=UTC),
                )
        finally:
            connection.close()

    def test_missed_order_job_persists_event_and_locks_order_jobs(self):
        """주문 관련 작업이 미스파이어되면 이벤트와 잠금을 DB에 남깁니다."""
        listener = _make_event_listener(self._connection_factory)

        listener(
            SimpleNamespace(
                job_id=ScheduleJob.DAILY_PIPELINE.value,
                code=EVENT_JOB_MISSED,
            )
        )

        connection = self._connection_factory()
        try:
            self.assertTrue(is_order_scheduler_locked(connection))
            event = connection.execute(
                "SELECT event_type FROM system_events "
                "WHERE component = 'scheduler' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(event[0], "MISFIRE_SKIPPED")
        finally:
            connection.close()

    def test_repeated_health_failures_do_not_flood_system_events(self):
        """계속되는 동일 헬스 장애는 안전 잠금 이벤트를 한 번만 남깁니다."""
        connection = self._connection_factory()
        try:
            for _ in range(3):
                set_order_scheduler_lock(
                    connection,
                    locked=True,
                    reason="HEALTH_CHECK_FAILED",
                    occurred_at=datetime(2026, 11, 23, 6, 0, tzinfo=UTC),
                )
            count = connection.execute(
                "SELECT COUNT(*) FROM system_events "
                "WHERE component = 'scheduler' AND event_type = 'HEALTH_CHECK_FAILED'"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(count, 1)

    def test_order_job_is_skipped_when_locked_or_market_closed(self):
        """잠금 또는 휴장 상태에서는 주문 관련 콜백이 실행되지 않습니다."""
        connection = self._connection_factory()
        try:
            self.assertTrue(is_order_scheduler_locked(connection))
            set_order_scheduler_lock(
                connection,
                locked=True,
                reason="RECONCILIATION_REQUIRED",
                occurred_at=datetime(2026, 11, 23, 6, 0, tzinfo=UTC),
            )
        finally:
            connection.close()
        calls = []

        _guarded_job(
            ScheduleJob.DAILY_PIPELINE,
            lambda: calls.append("called"),
            self._connection_factory,
            lambda: True,
        )
        connection = self._connection_factory()
        try:
            with self.assertRaises(PermissionError):
                set_order_scheduler_lock(
                    connection,
                    locked=False,
                    reason="RECONCILIATION_REQUIRED",
                    occurred_at=datetime(2026, 11, 23, 6, 0, tzinfo=UTC),
                )
            connection.execute(
                'INSERT INTO system_state ("key", value, updated_at) '
                "VALUES ('reconciliation_ok', 'true', '2026-11-23T06:00:00+00:00')"
            )
            set_order_scheduler_lock(
                connection,
                locked=False,
                reason="RECONCILIATION_REQUIRED",
                occurred_at=datetime(2026, 11, 23, 6, 0, tzinfo=UTC),
            )
        finally:
            connection.close()
        _guarded_job(
            ScheduleJob.DAILY_PIPELINE,
            lambda: calls.append("called"),
            self._connection_factory,
            lambda: False,
        )

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
