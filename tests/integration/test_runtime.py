"""runtime 조립 테스트: 더미 환경변수만 사용하고 네트워크·실계좌에는 접근하지 않습니다."""

import copy
import tempfile
import unittest
from pathlib import Path

from src.config import load_config
from src.db import initialize_database
from src.kis_client import KisTokenCache
from src.runtime import build_dependencies
from src.scheduler import ScheduleJob

DUMMY_ENV = {"KIS_APP_KEY": "dummy-key", "KIS_APP_SECRET": "dummy-secret", "KIS_ACCOUNT_NO": "1234567801"}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        initialize_database(self.db).close()
        self.cache = KisTokenCache(Path(self.tmp.name) / "token.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_builds_all_required_handlers_with_dummy_env(self):
        deps = build_dependencies(self.db, self.cache, environ=DUMMY_ENV)
        self.assertTrue(all(callable(h) for h in deps.job_handlers.values()))
        self.assertIn(ScheduleJob.INTRADAY_GUARD, deps.job_handlers)

    def test_every_scheduler_job_has_a_real_handler(self):
        deps = build_dependencies(self.db, self.cache, environ=DUMMY_ENV)
        for job in (ScheduleJob.MORNING_PREP, ScheduleJob.DAILY_PIPELINE, ScheduleJob.DAILY_BRIEFING,
                    ScheduleJob.WEEKLY_MAINTENANCE, ScheduleJob.PENDING_CONFIRMATION_POLLER):
            self.assertIn(job, deps.job_handlers)

    def test_missing_secret_is_rejected_without_echoing_values(self):
        with self.assertRaises(ValueError) as caught:
            build_dependencies(self.db, self.cache, environ={"KIS_APP_KEY": "dummy-key"})
        self.assertNotIn("dummy-key", str(caught.exception))

    def test_real_environment_is_refused(self):
        config = copy.deepcopy(load_config())
        config["deployment"]["environment"] = "REAL"
        with self.assertRaises(ValueError):
            build_dependencies(self.db, self.cache, environ=DUMMY_ENV, config=config)


if __name__ == "__main__":
    unittest.main()
