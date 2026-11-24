"""telegram_actions: 명령 콜백이 DB·운영 동작에 안전하게 연결되는지 검증합니다(더미 데이터만 사용)."""

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.db import connect_database, initialize_database
from src.job_handlers import save_daily_snapshot
from src.safety_flags import active_codes, raise_flag
from src.telegram_actions import FAIL, build_command_actions
from src.telegram_bot import ALLOWED_COMMANDS

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 25, 10, 0, tzinfo=KST)


class TelegramActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        self.conn = initialize_database(self.path)
        self.actions = build_command_actions(lambda: connect_database(self.path), approvals_dir=self.tmp.name, backtest_dir=self.tmp.name,
                                             clock=lambda: NOW)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def approve(self, command, action_type):
        """2단계 확인이 끝난 상태(APPROVED)를 만든다."""
        digest = hashlib.sha256(command.encode()).hexdigest()
        self.conn.execute(
            "INSERT INTO pending_actions (id, action_type, args_hash, code_hash, reason, approval_status, created_at,"
            " expires_at, confirmed_at) VALUES (?,?,?,?,'t','APPROVED','t','t','2026-11-25T01:00:00')",
            (f"act-{digest[:8]}", action_type, digest, digest))
        self.conn.commit()

    def run_cmd(self, text):
        return self.actions[text.split()[0]]("chat", text)

    def state(self, key):
        row = self.conn.execute('SELECT value FROM system_state WHERE "key" = ?', (key,)).fetchone()
        return row[0] if row else None

    def test_covers_every_phase1_command_except_confirm(self):
        self.assertEqual(set(self.actions), ALLOWED_COMMANDS - {"/confirm"})

    def test_read_only_commands_work_on_empty_db(self):
        for text in ("/status", "/preview", "/pool", "/trades 5", "/health", "/reconcile"):
            reply = self.run_cmd(text)
            self.assertIsInstance(reply, str)
            self.assertNotEqual(reply, FAIL)

    def test_pause_then_resume_needs_confirmation_and_blocks_during_safe_halt(self):
        self.run_cmd("/pause")
        self.assertEqual(self.state("is_paused"), "true")
        self.assertEqual(self.run_cmd("/resume"), FAIL)               # 승인 이력 없음
        self.approve("/resume", "RESUME")
        self.run_cmd("/resume")
        self.assertEqual(self.state("is_paused"), "false")
        raise_flag(self.conn, "HALT", "ACCOUNT_MISMATCH", now=NOW)
        self.assertIn("SAFE_HALT", self.run_cmd("/resume"))

    def test_training_on_with_days_sets_until_and_off_clears(self):
        self.approve("/training on 3", "TRAINING_ON")
        self.run_cmd("/training on 3")
        self.assertEqual((self.state("training_mode"), self.state("training_until")), ("true", "2026-11-28"))
        self.approve("/training off", "TRAINING_OFF")
        self.run_cmd("/training off")
        self.assertEqual(self.state("training_mode"), "false")

    def test_clear_halt_needs_approval_and_refuses_auto_codes_without_reconcile(self):
        raise_flag(self.conn, "HALT", "ACCOUNT_MISMATCH", now=NOW)
        self.assertEqual(self.run_cmd("/clear_halt account_mismatch"), FAIL)
        self.approve("/clear_halt account_mismatch", "CLEAR_HALT")
        self.assertEqual(self.run_cmd("/clear_halt account_mismatch"), FAIL)   # 대조 성공 전에는 거부
        self.conn.execute("INSERT INTO system_state (key, value, updated_at) VALUES ('reconciliation_ok','true','t')")
        self.conn.commit()
        self.assertIn("해제했습니다", self.run_cmd("/clear_halt account_mismatch"))
        self.assertEqual(active_codes(self.conn, "HALT"), ())

    def test_emergency_on_and_remove(self):
        self.approve("/emergency", "EMERGENCY_ON")
        self.run_cmd("/emergency")
        self.assertEqual((self.state("emergency_mode"), self.state("emergency_reason")), ("true", "MANUAL"))
        self.conn.execute(
            "INSERT INTO universe_assets (ticker, name, category, region, underlying_index, product_type,"
            " replication_method, status, data_status, added_at, asset_class) VALUES ('069500','E','RISK_ASSET','KR','K',"
            "'SPOT_ETF','PHYSICAL','ACTIVE','VALID','t','EQUITY')")
        self.conn.commit()
        self.approve("/remove 069500", "UNIVERSE_REMOVE")
        self.assertIn("제거", self.run_cmd("/remove 069500"))
        self.assertEqual(self.conn.execute("SELECT status FROM universe_assets").fetchone()[0], "REMOVED")

    def write_report(self, strategy_hash=None):
        """승인 대상 백테스트 보고서를 만들고 (명령, 정규화 해시)를 돌려준다."""
        import json
        from src.config import load_config, strategy_params_hash
        report = {"schema": "t", "strategy_params_hash": strategy_hash or strategy_params_hash(load_config()),
                  "code_commit_sha": "abc123", "data_range": ["2020-01-02", "2026-11-20"]}
        (Path(self.tmp.name) / "run-1.json").write_text(json.dumps(report), encoding="utf-8")
        canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "/approve_backtest run-1 " + hashlib.sha256(canonical.encode()).hexdigest()

    def test_backtest_approval_requires_matching_report_and_current_strategy(self):
        command = self.write_report()
        self.approve(command, "APPROVE_BACKTEST")
        self.assertIn("승인 완료", self.run_cmd(command))
        self.assertEqual(self.conn.execute("SELECT status FROM backtest_approvals").fetchone()[0], "APPROVED")

    def test_backtest_approval_rejects_wrong_hash_and_other_strategy(self):
        command = self.write_report()
        bad = command[:-4] + "0000"
        self.approve(bad, "APPROVE_BACKTEST")
        self.assertEqual(self.run_cmd(bad), FAIL)
        other = self.write_report(strategy_hash="b" * 64)
        self.approve(other, "APPROVE_BACKTEST")
        self.assertEqual(self.run_cmd(other), FAIL)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM backtest_approvals").fetchone()[0], 0)

    def test_approval_is_single_use(self):
        self.approve("/resume", "RESUME")
        self.assertNotEqual(self.run_cmd("/resume"), FAIL)
        self.assertEqual(self.run_cmd("/resume"), FAIL)  # 같은 확인으로 재실행 불가

    def test_stale_approval_is_rejected(self):
        digest = hashlib.sha256(b"/resume").hexdigest()
        self.conn.execute(
            "INSERT INTO pending_actions (id, action_type, args_hash, code_hash, reason, approval_status, created_at,"
            " expires_at, confirmed_at) VALUES ('old','RESUME',?,?,'t','APPROVED','t','t','2026-11-25T00:00:00+00:00')",
            (digest, digest))
        self.conn.commit()
        self.assertEqual(self.run_cmd("/resume"), FAIL)  # 확인 후 10분 초과

    def test_failure_reply_hides_exception_details(self):
        self.assertEqual(self.run_cmd("/classify 999 deposit"), FAIL)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM system_events WHERE event_type='COMMAND_FAILED'").fetchone()[0], 1)


class HwmRebaseTests(unittest.TestCase):
    def test_pending_rebase_is_consumed_once_by_next_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = initialize_database(Path(tmp) / "t.db")
            try:
                save_daily_snapshot(conn, trade_date="2026-11-24", total_equity=1_000_000, cash=0, now=NOW)
                save_daily_snapshot(conn, trade_date="2026-11-25", total_equity=800_000, cash=0, now=NOW + timedelta(days=1))
                conn.execute("INSERT INTO system_state (key, value, updated_at) VALUES ('hwm_rebase_pending','true','t')")
                conn.commit()
                save_daily_snapshot(conn, trade_date="2026-11-26", total_equity=800_000, cash=0, now=NOW + timedelta(days=2))
                row = conn.execute("SELECT hwm_flow_adjusted, flow_adjusted_unit_nav, hwm_rebased FROM daily_portfolio "
                                   "WHERE snapshot_date = '2026-11-26'").fetchone()
                self.assertAlmostEqual(row[0], row[1])
                self.assertEqual(row[2], 1)
                self.assertEqual(conn.execute("SELECT value FROM system_state WHERE key='hwm_rebase_pending'"
                                              ).fetchone()[0], "false")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
