"""부팅 복구 게이트(명세 3.3절 Step 0~6)의 정상·실패·무인 자동 복구 경로를 테스트합니다."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.boot_recovery import boot_recovery
from src.db import initialize_database
from src.kis_client import CachedAccessToken, KisTokenCache
from src.reconciliation import ReconciliationResult
from src.safety_flags import active_codes, active_halt_reasons, raise_flag
from src.scheduler import is_order_scheduler_locked

UTC = timezone.utc
OK = lambda: ReconciliationResult(True, ())  # noqa: E731


class BootRecoveryTests(unittest.TestCase):
    """부팅 복구가 안전 잠금을 올바르게 유지하고, 풀린 원인은 스스로 해제하는지 확인합니다."""

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
        self._save_token(valid=True)

    def _save_token(self, *, valid: bool) -> None:
        """유효 또는 만료 토큰을 캐시에 저장합니다."""
        delta = timedelta(hours=23) if valid else timedelta(hours=-1)
        self.cache.save(
            CachedAccessToken(
                access_token="dummy-token",
                issued_at=self.now - timedelta(hours=1 if valid else 25),
                expires_at=self.now + delta,
            )
        )

    def _boot(self, reconcile=OK, **kwargs):
        """같은 시각 기준으로 부팅 복구를 실행합니다."""
        return boot_recovery(
            self.connection, self.cache, reconcile_account=reconcile, now=self.now, **kwargs
        )

    def test_successful_reconciliation_enables_order_jobs(self):
        """유효 DB·토큰·계좌 대조가 모두 성공해야만 주문 작업을 풉니다."""
        result = self._boot()

        self.assertTrue(result.recovery_ok)
        self.assertTrue(result.order_jobs_enabled)
        self.assertFalse(is_order_scheduler_locked(self.connection))
        self.assertEqual(active_halt_reasons(self.connection), ())

    def test_missing_reconciliation_raises_boot_recovery_failed(self):
        """계좌 대조 콜백이 없으면 BOOT_RECOVERY_FAILED 원인으로 주문을 잠급니다."""
        result = self._boot(reconcile=None)

        self.assertFalse(result.recovery_ok)
        self.assertFalse(result.order_jobs_enabled)
        self.assertIn("BOOT_RECOVERY_FAILED", result.failure_codes)
        self.assertTrue(is_order_scheduler_locked(self.connection))

    def test_boot_recovery_failure_clears_itself_on_next_successful_boot(self):
        """자동 해제 원인은 사람 개입 없이 다음 부팅의 성공으로 풀립니다(무인 복구)."""
        self._boot(reconcile=None)
        result = self._boot()

        self.assertTrue(result.recovery_ok)
        self.assertEqual(active_halt_reasons(self.connection), ())

    def test_manual_halt_reason_survives_successful_boot(self):
        """수동 해제 원인(계좌 불일치)은 대조가 성공해도 운영자 확인 전까지 유지됩니다."""
        raise_flag(self.connection, "HALT", "ACCOUNT_MISMATCH", now=self.now)

        result = self._boot()

        self.assertFalse(result.recovery_ok)
        self.assertEqual(result.failure_codes, ("ACCOUNT_MISMATCH",))
        self.assertTrue(is_order_scheduler_locked(self.connection))

    def test_expired_token_is_credentials_unavailable_and_auto_clears(self):
        """만료 토큰은 CREDENTIALS_UNAVAILABLE 이고, 호스트가 갱신하면 자동 해제됩니다."""
        self._save_token(valid=False)
        result = self._boot()
        self.assertEqual(result.failure_codes, ("CREDENTIALS_UNAVAILABLE",))
        self.assertTrue(is_order_scheduler_locked(self.connection))

        self._save_token(valid=True)
        recovered = self._boot()
        self.assertTrue(recovered.recovery_ok)
        self.assertEqual(active_halt_reasons(self.connection), ())

    def test_account_mismatch_needs_two_observations_before_manual_halt(self):
        """v3.7.5: 첫 관측은 매수 대기만, 간격 뒤 같은 불일치가 재현돼야 수동 해제 원인이 됩니다."""
        mismatch = lambda: ReconciliationResult(False, ("X",), (), ("POSITION_MISMATCH",))  # noqa: E731

        first = self._boot(reconcile=mismatch)
        self.assertNotIn("ACCOUNT_MISMATCH", first.failure_codes)
        self.assertEqual(active_codes(self.connection, "BUY_BLOCK"), ("ACCOUNT_DATA_STALE",))

        self.now += timedelta(seconds=61)
        second = self._boot(reconcile=mismatch)
        self.assertIn("ACCOUNT_MISMATCH", second.failure_codes)

    def test_reconciliation_query_failure_is_not_an_account_mismatch(self):
        """조회 자체의 실패는 자동 해제 원인(BOOT_RECOVERY_FAILED)이며 수동 원인으로 격상되지 않습니다."""
        def broken():
            raise RuntimeError("KIS 5xx")

        result = self._boot(reconcile=broken)

        self.assertIn("BOOT_RECOVERY_FAILED", result.failure_codes)
        self.assertNotIn("ACCOUNT_MISMATCH", result.failure_codes)

    def test_orphan_order_blocks_orders_cancels_and_never_resubmits(self):
        """DB 에 없는 KIS 당일 주문은 어떤 주문인지 알 수 없어 계좌 전체(BOOT_RECOVERY_FAILED)를 막고 취소만 요청합니다."""
        cancelled = []
        orphan = lambda: ReconciliationResult(  # noqa: E731
            False, (), (), ("ORPHAN_ORDER",), orphan_orders=("KIS-777",)
        )

        result = self._boot(reconcile=orphan, cancel_order=cancelled.append)

        self.assertEqual(cancelled, ["KIS-777"])
        self.assertIn("BOOT_RECOVERY_FAILED", result.failure_codes)
        self.assertNotIn("ACCOUNT_MISMATCH", result.failure_codes)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM system_events WHERE event_type = 'ORPHAN_ORDER' "
            "AND severity = 'CRITICAL'").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0], 0)

    def test_submitting_orders_become_pending_confirmation_and_created_expire(self):
        """크래시로 남은 SUBMITTING 은 PENDING_CONFIRMATION, CREATED 는 EXPIRED 가 됩니다."""
        c = self.connection
        c.execute(
            "INSERT INTO universe_assets (ticker, name, category, region, underlying_index,"
            " product_type, replication_method, status, data_status, added_at)"
            " VALUES ('000001','E','RISK_ASSET','KR','K','SPOT_ETF','PHYSICAL','ACTIVE','VALID','t')")
        for key, status in (("a", "SUBMITTING"), ("b", "CREATED")):
            c.execute(
                "INSERT INTO order_intents (parent_intent_id, idempotency_key, cycle_id, intent_type,"
                " ticker, side, requested_qty, intent_payload_hash, status, created_at, updated_at)"
                " VALUES (?,?,'c','RISK_REDUCTION','000001','SELL',5,'h','IN_PROGRESS','t','t')",
                (f"p{key}", key))
            c.execute(
                "INSERT INTO orders (order_id, parent_intent_id, attempt_id, ticker, side, requested_qty,"
                " remaining_qty, order_type, order_price, status, strategy_reason, updated_at)"
                " VALUES (?,?,?,'000001','SELL',5,5,'LIMIT',1000,?,'r','t')",
                (f"o{key}", f"p{key}", f"at{key}", status))

        result = self._boot()

        statuses = {r[0]: r[1] for r in c.execute("SELECT order_id, status FROM orders")}
        self.assertEqual(statuses, {"oa": "PENDING_CONFIRMATION", "ob": "EXPIRED"})
        # v3.7.5 D3: 불확실 주문은 SAFE_HALT 를 만들지 않는다(방향별 게이트가 막고 경보만 남김).
        self.assertEqual(result.failure_codes, ())
        self.assertTrue(result.recovery_ok)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM system_events WHERE "
                                   "event_type = 'PENDING_CONFIRMATION_REMAINS'").fetchone()[0], 1)

    def test_unclassified_cash_flow_blocks_buys_without_safe_halt(self):
        """미분류 현금흐름은 SAFE_HALT 가 아니라 매수 차단 플래그만 만듭니다."""
        self.connection.execute(
            "INSERT INTO cash_flows (broker_transaction_id, flow_date, occurred_at, amount, flow_type,"
            " classification_status, source, observed_at, first_observed_at)"
            " VALUES ('RECON:1','2026-11-23','t',100000,'UNCLASSIFIED','UNCLASSIFIED_CASH_FLOW','CASH_RECON','t','t')")

        result = self._boot()

        self.assertTrue(result.recovery_ok)
        self.assertEqual(active_codes(self.connection, "BUY_BLOCK"), ("UNCLASSIFIED_CASH_FLOW",))

    def test_invalid_config_raises_config_invalid_halt(self):
        """설정 정합성 검사가 실패하면 CONFIG_INVALID 로 주문을 잠급니다."""
        result = self._boot(config={"risk": {"safe_halt": {"allow_reduce_only_sell": True}}})

        self.assertIn("CONFIG_INVALID", result.failure_codes)


if __name__ == "__main__":
    unittest.main()
