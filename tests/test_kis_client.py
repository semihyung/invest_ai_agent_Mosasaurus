import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.kis_client import (
    CachedAccessToken,
    KisTokenCache,
    RefreshTrigger,
    TokenRefreshPolicy,
    should_refresh_access_token,
)


UTC = timezone.utc
POLICY = TokenRefreshPolicy(
    proactive_refresh_min_age_hours=6,
    hard_guard_before_expiry_hours=1,
    min_refresh_cooldown_seconds=65,
)


class KisTokenCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "workspace"
        self.root.mkdir()
        self.cache_path = Path(self.temp_dir.name) / "secrets" / "kis_token.json"
        self.cache = KisTokenCache(self.cache_path, project_root=self.root)

    def test_round_trip_hides_token_from_record_repr(self):
        issued_at = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
        token = CachedAccessToken(
            "FAKE_ACCESS_TOKEN",
            issued_at,
            issued_at + timedelta(hours=24),
        )

        self.cache.save(token)
        loaded = self.cache.load()

        self.assertEqual(loaded, token)
        self.assertNotIn("FAKE_ACCESS_TOKEN", repr(loaded))
        payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertSetEqual(set(payload), {"access_token", "issued_at", "expires_at"})

    def test_returns_none_when_cache_does_not_exist(self):
        self.assertIsNone(self.cache.load())

    def test_rejects_cache_inside_project_workspace(self):
        with self.assertRaisesRegex(ValueError, "outside the project workspace"):
            KisTokenCache(self.root / "data" / "kis_token.json", project_root=self.root)

    def test_rejects_malformed_cache(self):
        self.cache_path.parent.mkdir(parents=True)
        self.cache_path.write_text("{invalid", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            self.cache.load()

    def test_rejects_naive_timestamps(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            CachedAccessToken(
                "FAKE_ACCESS_TOKEN",
                datetime(2026, 11, 21),
                datetime(2026, 11, 22, tzinfo=UTC),
            )


class TokenRefreshPolicyTests(unittest.TestCase):
    def setUp(self):
        self.issued_at = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
        self.token = CachedAccessToken(
            "FAKE_ACCESS_TOKEN",
            self.issued_at,
            self.issued_at + timedelta(hours=24),
        )

    def test_requests_initial_token_when_cache_is_empty(self):
        self.assertTrue(
            should_refresh_access_token(
                None,
                trigger=RefreshTrigger.MORNING_PREP,
                now=self.issued_at,
                policy=POLICY,
                last_request_at=None,
            )
        )

    def test_morning_prep_refreshes_at_six_hour_age(self):
        before_threshold = self.issued_at + timedelta(hours=6) - timedelta(seconds=1)
        at_threshold = self.issued_at + timedelta(hours=6)

        self.assertFalse(
            should_refresh_access_token(
                self.token,
                trigger=RefreshTrigger.MORNING_PREP,
                now=before_threshold,
                policy=POLICY,
                last_request_at=None,
            )
        )
        self.assertTrue(
            should_refresh_access_token(
                self.token,
                trigger=RefreshTrigger.MORNING_PREP,
                now=at_threshold,
                policy=POLICY,
                last_request_at=None,
            )
        )

    def test_recovery_refreshes_only_inside_one_hour_hard_guard(self):
        before_threshold = self.token.expires_at - timedelta(hours=1, seconds=1)
        at_threshold = self.token.expires_at - timedelta(hours=1)

        self.assertFalse(
            should_refresh_access_token(
                self.token,
                trigger=RefreshTrigger.RECOVERY,
                now=before_threshold,
                policy=POLICY,
                last_request_at=None,
            )
        )
        self.assertTrue(
            should_refresh_access_token(
                self.token,
                trigger=RefreshTrigger.RECOVERY,
                now=at_threshold,
                policy=POLICY,
                last_request_at=None,
            )
        )

    def test_cooldown_blocks_until_65_seconds_have_elapsed(self):
        now = self.issued_at + timedelta(hours=7)

        self.assertFalse(
            should_refresh_access_token(
                self.token,
                trigger=RefreshTrigger.MORNING_PREP,
                now=now,
                policy=POLICY,
                last_request_at=now - timedelta(seconds=64),
            )
        )
        self.assertTrue(
            should_refresh_access_token(
                self.token,
                trigger=RefreshTrigger.MORNING_PREP,
                now=now,
                policy=POLICY,
                last_request_at=now - timedelta(seconds=65),
            )
        )

    def test_rejects_unknown_refresh_trigger(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            should_refresh_access_token(
                self.token,
                trigger="unknown",
                now=self.issued_at,
                policy=POLICY,
                last_request_at=None,
            )


if __name__ == "__main__":
    unittest.main()
