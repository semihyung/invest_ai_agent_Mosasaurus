"""호스트 운영 도구(네트워크 감시·UPS/NTP 기록·토큰 갱신 상태)와 배포 구성 파일의 구조 검증입니다."""

import importlib.util
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.net_watchdog import (
    NONE,
    REBOOT,
    RESTART_INTERFACE,
    backoff_seconds,
    decide_action,
)
from src.safety_flags import read_host_state

ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc


def load_script(name):
    """`scripts/<name>.py` 를 패키지가 아니어도 불러옵니다."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def kst(day, hour, minute):
    """2026-11 의 KST 시각(day=23 월요일, 28 토요일)을 UTC datetime 으로 만듭니다."""
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC) - timedelta(hours=9)


class NetWatchdogTests(unittest.TestCase):
    """재연결은 단계적으로, 재부팅은 장외 1시간 1회만 허용합니다."""

    def test_no_failures_means_no_action_and_early_failures_only_restart(self):
        night = kst(23, 22, 0)
        self.assertEqual(decide_action(failures=0, now=night, last_reboot_at=None), NONE)
        self.assertEqual(decide_action(failures=4, now=night, last_reboot_at=None), RESTART_INTERFACE)

    def test_reboot_only_off_hours_and_once_per_hour(self):
        night = kst(23, 22, 0)
        self.assertEqual(decide_action(failures=5, now=night, last_reboot_at=None), REBOOT)
        self.assertEqual(
            decide_action(failures=9, now=night, last_reboot_at=night - timedelta(minutes=30)),
            RESTART_INTERFACE)
        self.assertEqual(
            decide_action(failures=9, now=night, last_reboot_at=night - timedelta(minutes=61)), REBOOT)

    def test_never_reboots_during_market_hours(self):
        self.assertEqual(decide_action(failures=50, now=kst(23, 10, 0), last_reboot_at=None),
                         RESTART_INTERFACE)
        self.assertEqual(decide_action(failures=50, now=kst(28, 10, 0), last_reboot_at=None), REBOOT)  # 토요일

    def test_backoff_is_exponential_and_capped(self):
        self.assertEqual([backoff_seconds(n) for n in (0, 1, 2, 3, 4)], [0, 1, 2, 4, 8])
        self.assertEqual(backoff_seconds(30), 300)


class HostStateWriterTests(unittest.TestCase):
    """UPS·NTP 출력 파싱과 원자적 기록(앱이 읽는 파일 형식과의 호환)입니다."""

    def setUp(self):
        self.script = load_script("ups_monitor")

    def test_chrony_output_is_parsed(self):
        output = ("System time     : 0.000012345 seconds fast of NTP time\n"
                  "Leap status     : Normal\n")
        parsed = self.script.parse_chrony_tracking(output)
        self.assertTrue(parsed["synchronized"])
        self.assertAlmostEqual(parsed["offset_seconds"], 1.2345e-5)
        slow = self.script.parse_chrony_tracking(
            "System time     : 2.5 seconds slow of NTP time\nLeap status     : Not synchronised\n")
        self.assertEqual(slow, {"synchronized": False, "offset_seconds": -2.5})
        self.assertEqual(self.script.parse_chrony_tracking("garbage"),
                         {"synchronized": False, "offset_seconds": None})

    def test_ups_status_validation(self):
        parse = self.script.parse_ups_status
        self.assertEqual(parse('{"battery_ratio": 0.8, "on_external_power": true}'),
                         {"battery_ratio": 0.8, "on_external_power": True})
        for bad in ('{"battery_ratio": 1.5, "on_external_power": true}',
                    '{"battery_ratio": 0.5, "on_external_power": "yes"}',
                    '{"battery_ratio": true, "on_external_power": true}', "not json", "{}"):
            self.assertIsNone(parse(bad), bad)

    def test_written_file_is_readable_by_the_app_with_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ups.json"
            now = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)
            self.script.write_atomic(path, {"battery_ratio": 0.9, "on_external_power": True}, now=now)
            self.assertEqual(read_host_state(path, now=now, ttl_seconds=120)["battery_ratio"], 0.9)
            self.assertIsNone(read_host_state(path, now=now + timedelta(seconds=121), ttl_seconds=120))
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["ups.json"])  # 임시 파일 없음


class TokenRefreshStatusTests(unittest.TestCase):
    """연속 3회 실패 시 경보 표식, 성공하면 초기화."""

    def test_consecutive_failures_raise_alert_and_success_resets(self):
        script = load_script("refresh_kis_token")
        now = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            results = [script.record_refresh_status(run_dir, ok=False, now=now) for _ in range(3)]
            self.assertEqual([r["alert"] for r in results], [False, False, True])
            self.assertEqual(results[-1]["consecutive_failures"], 3)
            self.assertEqual(script.record_refresh_status(run_dir, ok=True, now=now)["alert"], False)
            stored = json.loads((run_dir / "token_refresh_status.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["consecutive_failures"], 0)
            self.assertNotIn("token", json.dumps(stored).lower().replace("token_refresh", ""))


class DeploymentConfigStructureTests(unittest.TestCase):
    """컨테이너·워크플로우·유닛 파일이 명세 보안 규칙을 파일 수준에서 지키는지 확인합니다."""

    def test_compose_is_least_privilege(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for needle in ('user: "10001:10001"', "read_only: true", "cap_drop:", "- ALL",
                       "no-new-privileges:true", "/run/secrets:ro", "/run/approvals:ro",
                       "app.env"):
            self.assertIn(needle, compose, needle)
        # 시크릿 디렉터리 전체·단일 토큰 파일·host.env·백업 키는 마운트/주입하지 않습니다.
        self.assertNotIn("host.env", compose)
        self.assertNotIn("backup.key", compose)
        for line in compose.splitlines():
            if "SECRETS_DIR" in line and "volumes" not in line and line.strip().startswith("-"):
                self.assertTrue(line.strip().endswith(":ro") or "env" in line, line)
        self.assertIsNone(re.search(r"SECRETS_DIR[^/\n]*\}\s*:/", compose))

    def test_dockerfile_runs_as_unprivileged_user(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("--gid 10001 mosasecret", dockerfile)
        self.assertNotIn("COPY .env", dockerfile)

    def test_workflow_never_runs_self_hosted_job_on_pull_requests_and_uses_wrapper(self):
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        import yaml
        triggers = yaml.safe_load(workflow).get("on", yaml.safe_load(workflow).get(True))
        self.assertEqual(set(triggers), {"push"})  # pull_request 등 다른 이벤트로는 실행되지 않음
        self.assertIn("github.event_name == 'push'", workflow)
        self.assertIn("sudo -n /usr/local/sbin/mosasaurus-deploy", workflow)
        # 주석이 아니라 실제 실행 명령(run)만 검사: Runner 는 docker·저장소 사본 스크립트를 직접 실행하지 않습니다.
        deploy_steps = yaml.safe_load(workflow)["jobs"]["deploy"]["steps"]
        commands = " ".join(step.get("run", "") for step in deploy_steps)
        self.assertNotIn("docker", commands)
        self.assertNotIn("safe_deploy.sh", commands)
        self.assertIn("DEPLOY_ALLOWED_ACTORS", workflow)

    def test_env_example_has_names_only(self):
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                self.assertEqual(line.split("=", 1)[1], "", line)
        self.assertIn("ALLOWED_USER_ID", (ROOT / ".env.example").read_text(encoding="utf-8"))

    def test_token_timers_cover_hourly_and_weekday_0825(self):
        units = ROOT / "scripts" / "systemd"
        hourly = (units / "mosasaurus-token-hourly.timer").read_text(encoding="utf-8")
        weekday = (units / "mosasaurus-token-0825.timer").read_text(encoding="utf-8")
        self.assertIn("*-*-* *:00:00", hourly)
        self.assertIn("OnBootSec", hourly)
        self.assertIn("Mon..Fri *-*-* 08:25:00 Asia/Seoul", weekday)
        self.assertIn("--trigger recovery", (units / "mosasaurus-token-hourly.service").read_text(encoding="utf-8"))
        self.assertIn("--trigger morning_prep", (units / "mosasaurus-token-0825.service").read_text(encoding="utf-8"))

    def test_safe_deploy_has_ten_steps_and_rollback_semantics(self):
        script = (ROOT / "scripts" / "safe_deploy.sh").read_text(encoding="utf-8")
        for step in range(1, 11):
            self.assertIn(f"{step}단계", script, f"step {step}")
        for needle in ("python3 -m src.deployment check", "mosasaurus-agent:previous",
                       "backward_compatible: false", "DEPLOY_ROLLBACK", "docker compose stop -t 60",
                       "deploy_queue"):
            self.assertIn(needle, script, needle)
        self.assertLess(script.index("3단계"), script.index("4단계"))  # 점검 후에 중지
        self.assertNotIn("08:50-15:25", script)

    def test_deploy_wrapper_checks_ancestor_signature_and_sha_format(self):
        wrapper = (ROOT / "scripts" / "mosasaurus-deploy").read_text(encoding="utf-8")
        for needle in ("merge-base --is-ancestor", "verify-commit", "[!0-9a-f]", "-eq 40"):
            self.assertIn(needle, wrapper, needle)

    def test_no_real_secret_shaped_values_in_tracked_scripts(self):
        pattern = re.compile(r"(?i)(app[_-]?secret|api[_-]?key|token)\s*[:=]\s*['\"][A-Za-z0-9]{20,}['\"]")
        for path in list((ROOT / "scripts").rglob("*")) + [ROOT / "docker-compose.yml"]:
            if path.is_file():
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8", errors="ignore")), path)


if __name__ == "__main__":
    unittest.main()
