"""가용성 감사 수정의 회귀 테스트: 남은 텔레그램 잠금·복구 재시도·스레드 사망·헬스체크·가벼운 DB 점검."""

import signal
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from src.db import connect_database, initialize_database
from src.health_monitor import HealthSnapshot, heartbeat_is_fresh, probe_database, record_heartbeat
from src.kis_client import CachedAccessToken, KisTokenCache
from src.main import RuntimeDependencies, main, run_agent
from src.reconciliation import ReconciliationResult
from src.scheduler import ScheduleJob
from src.telegram_bot import _acquire_polling_lock

UTC = timezone.utc


class AvailabilityTestCase(unittest.TestCase):
    """임시 DB·토큰 캐시와 의존성 조립 도우미."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "t.db"
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.cache = KisTokenCache(root / "secrets" / "token.json", project_root=root / "project")
        self.cache.save(CachedAccessToken("dummy", self.now - timedelta(hours=1), self.now + timedelta(hours=23)))

    def dependencies(self, reconcile, on_health=lambda: None):
        """주어진 대조 콜백으로 의존성을 만든다(헬스 콜백이 호출될 때 `on_health` 도 실행)."""
        def snapshot():
            on_health()
            return HealthSnapshot(True, True, True, True, 20, 40, 50, self.now)

        return RuntimeDependencies(
            reconcile_account=reconcile,
            job_handlers={job: (lambda: None) for job in ScheduleJob if job is not ScheduleJob.BOOT_RECOVERY},
            market_is_open=lambda: True, health_snapshot=snapshot, refresh_watchdog=lambda: None)


class PollingLockTests(AvailabilityTestCase):
    """감사 1번: 정전으로 남은 텔레그램 잠금이 영구 부팅 실패로 이어지지 않는다."""

    def test_takeover_replaces_a_stale_lock_and_leaves_an_audit_event(self):
        initialize_database(self.db).close()
        factory = lambda: connect_database(self.db)
        _acquire_polling_lock(factory, owner_id="dead-process")
        with self.assertRaises(RuntimeError):  # 기본값은 여전히 두 번째 프로세스를 거부한다
            _acquire_polling_lock(factory, owner_id="new-process")
        _acquire_polling_lock(factory, owner_id="new-process", takeover=True)
        connection = factory()
        try:
            owner = connection.execute("SELECT value FROM system_state WHERE \"key\"='telegram_polling_lock'").fetchone()[0]
            events = connection.execute(
                "SELECT COUNT(*) FROM system_events WHERE event_type = 'POLLING_LOCK_TAKEOVER'").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual((owner, events), ("new-process", 1))


class RecoveryRetryTests(AvailabilityTestCase):
    """감사 2번: 부팅 복구 실패는 재시도하고, 매매 스레드가 죽으면 프로세스를 내린다."""

    def test_failed_recovery_is_retried_until_it_succeeds(self):
        stop, calls = threading.Event(), []

        def reconcile():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("KIS down")  # 첫 부팅: 증권사 조회 실패 → BOOT_RECOVERY_FAILED(자동 해제 원인)
            return ReconciliationResult(True, ())

        result = run_agent(self.db, self.cache, self.dependencies(reconcile, stop.set), stop_event=stop,
                           recovery_retry_seconds=0.01)
        self.assertTrue(result.recovery_ok)
        self.assertEqual(len(calls), 2)

    def test_retry_can_be_disabled_and_stop_interrupts_the_wait(self):
        def failing():
            raise RuntimeError("KIS down")

        once = run_agent(self.db, self.cache, self.dependencies(failing), stop_event=threading.Event(),
                         recovery_retry_seconds=None)
        self.assertFalse(once.recovery_ok)
        stopped = threading.Event()
        stopped.set()  # 이미 중지 요청 상태면 긴 대기 없이 바로 돌아온다
        interrupted = run_agent(self.db, self.cache, self.dependencies(failing), stop_event=stopped,
                                recovery_retry_seconds=3600)
        self.assertFalse(interrupted.recovery_ok)

    def test_dead_agent_thread_terminates_the_process(self):
        killed = threading.Event()
        env = {"TELEGRAM_BOT_TOKEN": "dummy", "ALLOWED_CHAT_ID": "1", "ALLOWED_USER_ID": "1"}

        def fake_kill(pid, sig):
            self.assertEqual(sig, signal.SIGTERM)
            killed.set()

        def fake_polling(*_args, **_kwargs):
            self.assertTrue(killed.wait(5))  # 실제 폴링은 SIGTERM 으로 정상 종료한다

        deps = self.dependencies(lambda: ReconciliationResult(True, ()))
        with mock.patch.dict("os.environ", env), mock.patch("src.main.run_agent", side_effect=RuntimeError("boom")), \
                mock.patch("src.main._run_telegram", side_effect=fake_polling), \
                mock.patch("src.main.os.kill", side_effect=fake_kill), mock.patch("src.main.traceback.print_exc"):
            code = main(deps, db_path=self.db, token_cache=self.cache)
        self.assertTrue(killed.is_set())
        self.assertEqual(code, 2)


class HealthTests(AvailabilityTestCase):
    """감사 3·4번: heartbeat 신선도 판정과 전체 무결성 검사 생략."""

    def test_heartbeat_freshness(self):
        connection = initialize_database(self.db)
        try:
            self.assertFalse(heartbeat_is_fresh(connection, now=self.now))  # 아직 기록 없음
            record_heartbeat(connection, now=self.now)
            self.assertTrue(heartbeat_is_fresh(connection, now=self.now + timedelta(seconds=30)))
            self.assertFalse(heartbeat_is_fresh(connection, now=self.now + timedelta(seconds=120)))
        finally:
            connection.close()

    def test_light_probe_skips_the_full_integrity_check(self):
        initialize_database(self.db).close()

        def probe(full):
            seen = []

            def factory():
                connection = connect_database(self.db)
                connection.set_trace_callback(seen.append)
                return connection

            self.assertTrue(probe_database(factory, full_check=full))
            return any("integrity_check" in sql for sql in seen)

        self.assertFalse(probe(False))
        self.assertTrue(probe(True))


if __name__ == "__main__":
    unittest.main()
