"""실행 조립이 복구·콜백 조건을 지키는지 테스트합니다."""

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.db import connect_database
from src.health_monitor import HealthSnapshot
from src.kis_client import CachedAccessToken, KisTokenCache
from src.main import RuntimeDependencies, main, run_agent
from src.reconciliation import ReconciliationResult
from src.scheduler import ScheduleJob, is_order_scheduler_locked


UTC = timezone.utc


class MainRuntimeTests(unittest.TestCase):
    """실제 외부 API 없이 메인 런타임의 Fail-Closed 동작을 검사합니다."""

    def setUp(self):
        """격리된 외부 토큰 캐시와 시간 기준을 준비합니다."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.cache = KisTokenCache(
            root / "external-secrets" / "token.json",
            project_root=root / "project",
        )
        self.cache.save(
            CachedAccessToken(
                access_token="dummy-token",
                issued_at=self.now - timedelta(hours=1),
                expires_at=self.now + timedelta(hours=23),
            )
        )

    def test_cli_without_callbacks_latches_safe_halt(self):
        """운영 콜백이 없으면 스케줄러를 시작하지 않고 SAFE_HALT 합니다."""
        result = main(
            db_path=Path(self.temp_dir.name) / "main.db",
            token_cache=self.cache,
        )

        self.assertEqual(result, 2)
        connection = connect_database(Path(self.temp_dir.name) / "main.db")
        try:
            self.assertTrue(is_order_scheduler_locked(connection))
            self.assertEqual(
                connection.execute(
                    'SELECT value FROM system_state WHERE "key" = ?',
                    ("is_safe_halt",),
                ).fetchone()[0],
                "true",
            )
        finally:
            connection.close()

    def test_injected_runtime_starts_only_after_successful_recovery(self):
        """정상 대조와 전체 작업 콜백이 있을 때 런타임을 조립합니다."""
        handlers = {
            job: lambda: None
            for job in ScheduleJob
            if job is not ScheduleJob.BOOT_RECOVERY
        }
        dependencies = RuntimeDependencies(
            reconcile_account=lambda: ReconciliationResult(True, ()),
            job_handlers=handlers,
            market_is_open=lambda: True,
            health_snapshot=lambda: HealthSnapshot(
                database_ok=True,
                heartbeat_ok=True,
                token_valid=True,
                reconciliation_ok=True,
                cpu_percent=20,
                disk_percent=40,
                temperature_c=50,
                checked_at=self.now,
            ),
            refresh_watchdog=lambda: None,
        )
        stop_event = threading.Event()
        stop_event.set()

        result = run_agent(
            Path(self.temp_dir.name) / "runtime.db",
            self.cache,
            dependencies,
            stop_event=stop_event,
        )

        self.assertTrue(result.recovery_ok)
        connection = connect_database(Path(self.temp_dir.name) / "runtime.db")
        try:
            self.assertFalse(is_order_scheduler_locked(connection))
        finally:
            connection.close()

    def test_health_failure_locks_scheduler_and_fails_runtime(self):
        """부팅 후 헬스 장애가 발생하면 주문 작업을 잠그고 실패를 반환합니다."""
        stop_event = threading.Event()
        watchdog_calls = []

        def unhealthy_snapshot():
            """루프를 종료하면서 의도적으로 불량한 헬스 상태를 반환합니다."""
            stop_event.set()
            return HealthSnapshot(
                database_ok=True,
                heartbeat_ok=True,
                token_valid=True,
                reconciliation_ok=False,
                cpu_percent=20,
                disk_percent=40,
                temperature_c=50,
                checked_at=self.now,
            )

        handlers = {
            job: lambda: None
            for job in ScheduleJob
            if job is not ScheduleJob.BOOT_RECOVERY
        }
        dependencies = RuntimeDependencies(
            reconcile_account=lambda: ReconciliationResult(True, ()),
            job_handlers=handlers,
            market_is_open=lambda: True,
            health_snapshot=unhealthy_snapshot,
            refresh_watchdog=lambda: watchdog_calls.append("refreshed"),
        )
        db_path = Path(self.temp_dir.name) / "unhealthy-runtime.db"

        result = run_agent(
            db_path,
            self.cache,
            dependencies,
            stop_event=stop_event,
        )

        connection = connect_database(db_path)
        try:
            self.assertTrue(is_order_scheduler_locked(connection))
        finally:
            connection.close()
        self.assertFalse(result.recovery_ok)
        self.assertFalse(result.order_jobs_enabled)
        self.assertEqual(result.failure_codes, ("HEALTH_CHECK_FAILED",))
        self.assertEqual(watchdog_calls, [])


if __name__ == "__main__":
    unittest.main()
