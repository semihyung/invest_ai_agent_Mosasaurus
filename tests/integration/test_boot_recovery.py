"""부팅 복구 게이트의 정상·실패 경로를 테스트합니다."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.boot_recovery import boot_recovery
from src.db import initialize_database
from src.kis_client import CachedAccessToken, KisTokenCache
from src.reconciliation import ReconciliationResult
from src.scheduler import is_order_scheduler_locked


UTC = timezone.utc


class BootRecoveryTests(unittest.TestCase):
    """부팅 복구가 안전 잠금을 올바르게 유지하는지 확인합니다."""

    def setUp(self):
        """격리된 DB와 워크스페이스 외부 토큰 캐시를 준비합니다."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.connection = initialize_database(root / "mosasaurus.db")
        self.addCleanup(self.connection.close)
        self.cache = KisTokenCache(
            root / "external-secrets" / "token.json",
            project_root=root / "project",
        )
        self.now = datetime(2026, 11, 23, 6, 0, tzinfo=UTC)
        self.cache.save(
            CachedAccessToken(
                access_token="dummy-token",
                issued_at=self.now - timedelta(hours=1),
                expires_at=self.now + timedelta(hours=23),
            )
        )

    def test_successful_reconciliation_enables_order_jobs(self):
        """유효 DB·토큰·계좌 대조가 모두 성공해야만 주문 작업을 풉니다."""
        """정상 부팅 검사 후 예약 잠금이 해제되는지 확인합니다."""
        result = boot_recovery(
            self.connection,
            self.cache,
            reconcile_account=lambda: ReconciliationResult(True, ()),
            now=self.now,
        )

        self.assertTrue(result.recovery_ok)
        self.assertTrue(result.order_jobs_enabled)
        self.assertFalse(is_order_scheduler_locked(self.connection))

    def test_missing_reconciliation_latches_safe_halt(self):
        """계좌 대조 콜백이 없으면 자동으로 주문 잠금과 SAFE_HALT를 기록합니다."""
        """대조 기능이 준비되지 않으면 SAFE_HALT가 걸리는지 확인합니다."""
        result = boot_recovery(
            self.connection,
            self.cache,
            reconcile_account=None,
            now=self.now,
        )

        self.assertFalse(result.recovery_ok)
        self.assertFalse(result.order_jobs_enabled)
        self.assertIn("RECONCILIATION_UNAVAILABLE", result.failure_codes)
        self.assertTrue(is_order_scheduler_locked(self.connection))
        value =         """기존 안전 정지 상태가 부팅 후에도 유지되는지 확인합니다."""
        value = self.connection.execute(
            'SELECT value FROM system_state WHERE "key" = ?',
            ("is_safe_halt",),
        ).fetchone()[0]
        self.assertEqual(value, "true")

    def test_preexisting_safe_halt_is_not_automatically_cleared(self):
        """기존 SAFE_HALT는 부팅 대조가 성공해도 자동으로 해제하지 않습니다."""
        self.connection.execute(
            'INSERT INTO system_state ("key", value, updated_at) '
            "VALUES ('is_safe_halt', 'true', ?)",
            (self.now.isoformat(),),
        )

        result = boot_recovery(
            self.connection,
            self.cache,
            reconcile_account=lambda: ReconciliationResult(True, ()),
            now=self.now,
        )

        self.assertFalse(result.recovery_ok)
        self.assertIn("SAFE_HALT_ALREADY_LATCHED", result.failure_codes)
        self.assertEqual(
            self.connection.execute(
                'SELECT value FROM system_state WHERE "key" = ?',
                ("is_safe_halt",),
            ).fetchone()[0],
            "true",
        )

    def test_expired_token_keeps_order_jobs_locked(self):
        """만료 토큰으로는 부팅 복구를 통과하지 못합니다."""
        """만료된 토큰으로는 예약 잠금이 해제되지 않는지 확인합니다."""
        self.cache.save(
            CachedAccessToken(
                access_token="dummy-expired",
                issued_at=self.now - timedelta(hours=25),
                expires_at=self.now - timedelta(hours=1),
            )
        )

        result = boot_recovery(
            self.connection,
            self.cache,
            reconcile_account=lambda: ReconciliationResult(True, ()),
            now=self.now,
        )

        self.assertIn("TOKEN_MISSING_OR_EXPIRED", result.failure_codes)
        self.assertTrue(is_order_scheduler_locked(self.connection))


if __name__ == "__main__":
    unittest.main()
