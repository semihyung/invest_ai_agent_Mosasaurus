import unittest
from datetime import datetime, timedelta, timezone

from src.deployment import is_deployment_blocked, validate_deploy_request

UTC = timezone.utc
SHA = "a" * 40


def kst(year, month, day, hour, minute):
    """KST 시각을 UTC 로 바꿔 만듭니다(KST = UTC+9)."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC) - timedelta(hours=9)


class FreezeWindowTests(unittest.TestCase):
    """명세 3.4절: 평일 08:20–15:45, 토 09:50–10:30, 일 02:50–03:30 과 완료 신호."""

    def test_weekday_window_edges(self):
        # 2026-11-23 은 월요일.
        self.assertFalse(is_deployment_blocked(kst(2026, 11, 23, 8, 19)))
        self.assertTrue(is_deployment_blocked(kst(2026, 11, 23, 8, 20)))
        self.assertTrue(is_deployment_blocked(kst(2026, 11, 23, 15, 45)))
        self.assertFalse(is_deployment_blocked(kst(2026, 11, 23, 15, 46)))

    def test_old_window_start_is_now_covered_by_the_wider_freeze(self):
        self.assertTrue(is_deployment_blocked(kst(2026, 11, 23, 8, 25)))  # 토큰 갱신 시각

    def test_after_close_requires_the_reconciliation_done_signal_on_open_days(self):
        late = kst(2026, 11, 23, 16, 30)
        self.assertFalse(is_deployment_blocked(late))
        self.assertTrue(is_deployment_blocked(late, post_close_required=True, post_close_done=False))
        self.assertFalse(is_deployment_blocked(late, post_close_required=True, post_close_done=True))

    def test_saturday_preview_and_sunday_backup_windows(self):
        self.assertTrue(is_deployment_blocked(kst(2026, 11, 28, 10, 0)))
        self.assertFalse(is_deployment_blocked(kst(2026, 11, 28, 11, 0)))
        self.assertTrue(is_deployment_blocked(kst(2026, 11, 29, 3, 0)))
        self.assertTrue(is_deployment_blocked(kst(2026, 11, 29, 3, 40), weekly_maintenance_done=False))
        self.assertFalse(is_deployment_blocked(kst(2026, 11, 29, 3, 40), weekly_maintenance_done=True))
        self.assertFalse(is_deployment_blocked(kst(2026, 11, 29, 12, 0), weekly_maintenance_done=False))

    def test_requires_timezone_aware_datetime(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            is_deployment_blocked(datetime(2026, 11, 22, 10, 0))


class DeployRequestTests(unittest.TestCase):
    """배포 래퍼의 검증 규칙(조상·서명·CI·행위자·SHA 형식)입니다."""

    ok = dict(is_ancestor_of_main=True, signature_valid=True, ci_passed=True)

    def test_valid_request_passes(self):
        validate_deploy_request(SHA, **self.ok)

    def test_each_failed_check_refuses(self):
        for key in ("is_ancestor_of_main", "signature_valid", "ci_passed"):
            with self.assertRaises(PermissionError, msg=key):
                validate_deploy_request(SHA, **{**self.ok, key: False})
        with self.assertRaises(PermissionError):
            validate_deploy_request(SHA, **self.ok, actor_allowed=False)

    def test_malformed_sha_is_refused(self):
        for bad in ("", "main", "A" * 40, "a" * 39, "a" * 40 + "; rm -rf /", None):
            with self.assertRaises(PermissionError, msg=bad):
                validate_deploy_request(bad, **self.ok)


if __name__ == "__main__":
    unittest.main()
