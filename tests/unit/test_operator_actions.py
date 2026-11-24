"""2단계 확인 보안과 승인·해제·분류·검증 작업(명세 6.4·7.1절)을 검증합니다."""

import json
from contextlib import closing
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import ConfigError, load_config, strategy_params_hash
from src.db import initialize_database
from src.operator_actions import (
    ApprovalError,
    adopt_broker_positions,
    approve_backtest,
    approve_real_activation,
    approve_smoke_session,
    classify_cash_flow,
    clear_halt,
    confirm_corporate_action,
    consume_smoke_session,
    has_valid_backtest_approval,
    real_gate_inputs,
    real_activation_valid,
    release_emergency,
    smoke_session_active,
)
from src.safety_flags import active_codes, raise_flag
from src.telegram_bot import (
    CONFIRM_MAX_FAILURES,
    PendingActionStore,
    TelegramCommandHandler,
    hash_confirm_code,
    new_confirm_code,
)

NOW = datetime(2026, 11, 24, 6, 0, tzinfo=timezone.utc)


class ConfirmationSecurityTests(unittest.TestCase):
    """확인 코드: 난수·해시 저장·단일 사용·잠금·사용자 결합."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "t.db"
        initialize_database(self.db).close()
        from src.db import connect_database
        self.store = PendingActionStore(lambda: connect_database(self.db))
        self.handler = TelegramCommandHandler(
            {"1"}, pending_store=self.store, allowed_user_ids={"10"})

    def _request(self, text, *, user="10", at=NOW):
        return self.handler.handle("1", text, at, user_id=user)

    def test_code_is_eight_base32_chars_and_random(self):
        codes = {new_confirm_code() for _ in range(50)}
        self.assertEqual(len(codes), 50)
        self.assertTrue(all(len(c) == 8 and set(c) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for c in codes))

    def test_only_hash_is_stored_and_code_is_single_use(self):
        status, action = self._request("/emergency")
        self.assertEqual(status, "CONFIRMATION_REQUIRED")
        from src.db import connect_database
        with closing(connect_database(self.db)) as connection:
            row = connection.execute("SELECT code_hash, args_hash FROM pending_actions").fetchone()
        self.assertEqual(row["code_hash"], hash_confirm_code(action.confirm_code))
        self.assertNotIn(action.confirm_code, json.dumps(dict(row)))
        first, _ = self._request(f"/confirm {action.confirm_code.lower()}")
        again, _ = self._request(f"/confirm {action.confirm_code.lower()}")
        self.assertEqual((first, again), ("CONFIRMED", "CONFIRMATION_NOT_FOUND"))

    def test_other_user_or_chat_cannot_use_the_code(self):
        _, action = self._request("/emergency")
        self.assertEqual(self._request("/status", user="11")[0], "UNAUTHORIZED")
        self.assertEqual(self.handler.handle("2", "/status", NOW, user_id="10")[0], "UNAUTHORIZED")
        self.assertEqual(self._request(f"/confirm {action.confirm_code}", user=None)[0], "UNAUTHORIZED")

    def test_five_wrong_codes_lock_two_step_commands_for_thirty_minutes(self):
        for _ in range(CONFIRM_MAX_FAILURES):
            self.assertEqual(self._request("/confirm aaaaaaaa")[0], "CONFIRMATION_NOT_FOUND")
        self.assertEqual(self._request("/emergency")[0], "CONFIRMATIONS_LOCKED")
        self.assertEqual(self._request("/status")[0], "EXECUTE")  # 읽기 명령은 영향 없음
        later = NOW + timedelta(minutes=31)
        self.assertEqual(self._request("/emergency", at=later)[0], "CONFIRMATION_REQUIRED")

    def test_rate_limit_per_minute(self):
        results = [self._request("/status")[0] for _ in range(12)]
        self.assertEqual(results[:10], ["EXECUTE"] * 10)
        self.assertEqual(results[10:], ["RATE_LIMITED"] * 2)
        self.assertEqual(self._request("/status", at=NOW + timedelta(minutes=2))[0], "EXECUTE")

    def test_risk_increasing_and_release_commands_need_two_steps_but_pause_does_not(self):
        for text in ("/resume", "/training on 7", "/training off", "/emergency off", "/reconcile adopt",
                     "/clear_halt account_mismatch", "/classify 3 deposit", "/ca_confirm 4",
                     "/approve_real real-2026-12", "/approve_smoke smoke-001"):
            self.assertEqual(self._request(text, at=NOW + timedelta(hours=1))[0], "CONFIRMATION_REQUIRED", text)
            self.handler.pending.clear()
            self.handler._recent_commands.clear()
        for text in ("/pause", "/stop", "/reconcile", "/status"):
            self.assertEqual(self._request(text)[0], "EXECUTE", text)

    def test_invalid_arguments_are_rejected_before_any_code_is_issued(self):
        for text in ("/classify x deposit", "/classify 3 gift", "/approve_real", "/approve_real A B",
                     "/approve_backtest run1 notahash", "/reconcile restore", "/clear_halt"):
            self.assertEqual(self._request(text)[0], "INVALID_ARGUMENTS", text)
            self.handler._recent_commands.clear()


class ApprovalWorkflowTests(unittest.TestCase):
    """호스트 승인 파일 + DB 승인 레코드 3중 잠금과 스모크·백테스트 승인."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = initialize_database(Path(self.temp.name) / "t.db")
        self.addCleanup(self.connection.close)
        self.dir = Path(self.temp.name) / "approvals"
        self.dir.mkdir()

    def _approved_action(self, action_type, action_id="act1"):
        self.connection.execute(
            "INSERT OR IGNORE INTO pending_actions (id, action_type, code_hash, reason, approval_status,"
            " created_at, expires_at) VALUES (?,?,?,?,'APPROVED','t','t')",
            (action_id, action_type, f"h-{action_id}", "r"))
        return action_id

    def _write(self, name, approval_id, *, days=10, alias="isa-main", expired=False):
        approved = NOW - timedelta(hours=1)
        expires = NOW - timedelta(minutes=1) if expired else approved + timedelta(days=days)
        (self.dir / name).write_text(json.dumps({
            "approval_id": approval_id, "approved_at": approved.isoformat(),
            "expires_at": expires.isoformat(), "account_alias": alias}), encoding="utf-8")

    def test_real_activation_requires_approved_action_matching_file_and_alias(self):
        self._write("real_activation.json", "real-001")
        with self.assertRaises(ApprovalError):  # 2단계 확인 이력이 없음
            approve_real_activation(self.connection, approval_id="real-001", action_id="nope",
                                    approvals_dir=self.dir, now=NOW)
        action = self._approved_action("APPROVE_REAL")
        with self.assertRaises(ApprovalError):  # id 불일치
            approve_real_activation(self.connection, approval_id="real-999", action_id=action,
                                    approvals_dir=self.dir, now=NOW)
        approve_real_activation(self.connection, approval_id="real-001", action_id=action,
                                approvals_dir=self.dir, now=NOW)
        self.assertTrue(real_activation_valid(self.connection, self.dir, now=NOW))

    def test_account_number_in_alias_and_overlong_validity_are_rejected(self):
        action = self._approved_action("APPROVE_REAL")
        self._write("real_activation.json", "real-001", alias="1234567801")
        with self.assertRaises(ApprovalError):
            approve_real_activation(self.connection, approval_id="real-001", action_id=action,
                                    approvals_dir=self.dir, now=NOW)
        self._write("real_activation.json", "real-001", days=45)
        with self.assertRaises(ApprovalError):
            approve_real_activation(self.connection, approval_id="real-001", action_id=action,
                                    approvals_dir=self.dir, now=NOW)

    def test_tampered_or_expired_host_file_invalidates_real_activation(self):
        action = self._approved_action("APPROVE_REAL")
        self._write("real_activation.json", "real-001")
        approve_real_activation(self.connection, approval_id="real-001", action_id=action,
                                approvals_dir=self.dir, now=NOW)
        self._write("real_activation.json", "real-001", days=20)  # 내용 변조 → 해시 불일치
        self.assertFalse(real_activation_valid(self.connection, self.dir, now=NOW))
        self._write("real_activation.json", "real-001")
        self.assertTrue(real_activation_valid(self.connection, self.dir, now=NOW))
        self.assertFalse(real_activation_valid(self.connection, self.dir, now=NOW + timedelta(days=11)))
        (self.dir / "real_activation.json").unlink()
        self.assertFalse(real_activation_valid(self.connection, self.dir, now=NOW))

    def test_smoke_session_needs_real_expires_in_an_hour_and_is_consumed_once(self):
        smoke_action = self._approved_action("APPROVE_SMOKE", "act2")
        self._write("smoke_session.json", "smoke-001")
        with self.assertRaises(ApprovalError):  # REAL 활성화 없음
            approve_smoke_session(self.connection, approval_id="smoke-001", action_id=smoke_action,
                                  approvals_dir=self.dir, now=NOW)
        self._write("real_activation.json", "real-001")
        approve_real_activation(self.connection, approval_id="real-001",
                                action_id=self._approved_action("APPROVE_REAL"),
                                approvals_dir=self.dir, now=NOW)
        approve_smoke_session(self.connection, approval_id="smoke-001", action_id=smoke_action,
                              approvals_dir=self.dir, now=NOW)
        self.assertEqual(smoke_session_active(self.connection, now=NOW), "smoke-001")
        self.assertTrue(consume_smoke_session(self.connection, "smoke-001", now=NOW))
        self.assertFalse(consume_smoke_session(self.connection, "smoke-001", now=NOW))
        self.assertIsNone(smoke_session_active(self.connection, now=NOW))

    def test_smoke_session_expires_after_sixty_minutes(self):
        self._write("real_activation.json", "real-001")
        approve_real_activation(self.connection, approval_id="real-001",
                                action_id=self._approved_action("APPROVE_REAL"),
                                approvals_dir=self.dir, now=NOW)
        self._write("smoke_session.json", "smoke-002", days=5)
        approve_smoke_session(self.connection, approval_id="smoke-002",
                              action_id=self._approved_action("APPROVE_SMOKE", "act3"),
                              approvals_dir=self.dir, now=NOW)
        self.assertEqual(smoke_session_active(self.connection, now=NOW + timedelta(minutes=59)), "smoke-002")
        self.assertIsNone(smoke_session_active(self.connection, now=NOW + timedelta(minutes=61)))

    def test_backtest_approval_is_bound_to_the_current_strategy_hash(self):
        config = load_config(environ={})
        current = strategy_params_hash(config)
        action = self._approved_action("APPROVE_BACKTEST", "bt1")
        approve_backtest(self.connection, run_id="run1", report_sha256="a" * 64,
                         code_commit_sha="abc123", strategy_params_hash=current,
                         data_range=("2023-01-01", "2026-11-20"), action_id=action, now=NOW)
        self.assertTrue(has_valid_backtest_approval(self.connection, current))
        config["trailing_stop"]["normal"] = -0.12
        self.assertFalse(has_valid_backtest_approval(self.connection, strategy_params_hash(config)))
        with self.assertRaises(ValueError):
            approve_backtest(self.connection, run_id="run2", report_sha256="zz",
                             code_commit_sha="abc", strategy_params_hash=current,
                             data_range=("a", "b"), action_id=action, now=NOW)

    def test_gate_inputs_require_all_three_locks_for_real_and_paper_skips_backtest(self):
        config = load_config(environ={})
        paper = real_gate_inputs(self.connection, config, self.dir, now=NOW)
        self.assertEqual(paper, {"operator_approval": False, "backtest_calibrated": True,
                                 "smoke_test_approved": False})
        config["deployment"]["environment"] = "REAL"
        self._write("real_activation.json", "real-001")
        # 승인 파일만 있고 DB 레코드가 없으면 REAL 은 열리지 않습니다.
        blocked = real_gate_inputs(self.connection, config, self.dir, now=NOW)
        self.assertEqual(blocked, {"operator_approval": False, "backtest_calibrated": False,
                                   "smoke_test_approved": False})
        approve_real_activation(self.connection, approval_id="real-001",
                                action_id=self._approved_action("APPROVE_REAL"),
                                approvals_dir=self.dir, now=NOW)
        still = real_gate_inputs(self.connection, config, self.dir, now=NOW)
        self.assertTrue(still["operator_approval"])
        self.assertFalse(still["backtest_calibrated"])  # 백테스트 승인 없이는 전략 주문 불가
        approve_backtest(self.connection, run_id="r1", report_sha256="c" * 64, code_commit_sha="x",
                         strategy_params_hash=strategy_params_hash(config),
                         data_range=("2023-01-01", "2026-11-20"),
                         action_id=self._approved_action("APPROVE_BACKTEST", "bt9"), now=NOW)
        ready = real_gate_inputs(self.connection, config, self.dir, now=NOW)
        self.assertEqual((ready["operator_approval"], ready["backtest_calibrated"]), (True, True))
        self.assertFalse(ready["smoke_test_approved"])  # 스모크 세션은 별도 승인

    def test_strategy_hash_covers_fee_and_drawdown_but_not_unrelated_keys(self):
        base = strategy_params_hash(load_config(environ={}))
        # v3.7.5: execution.emergency 블록이 삭제돼 수수료율·낙폭 임계값이 전략 해시에 남는다.
        for path, value in ((("execution", "fee_rate"), 0.001),
                            (("risk", "portfolio_drawdown", "critical"), -0.2)):
            config = load_config(environ={})
            node = config
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            self.assertNotEqual(base, strategy_params_hash(config), path)
        config = load_config(environ={})
        config["telegram"]["max_commands_per_minute"] = 5
        self.assertEqual(base, strategy_params_hash(config))
        del config["momentum"]
        with self.assertRaises(ConfigError):
            strategy_params_hash(config)


class OperatorActionTests(unittest.TestCase):
    """`/clear_halt`·`/classify`·`/ca_confirm`·`/reconcile adopt`·`/emergency off`."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = initialize_database(Path(self.temp.name) / "t.db")
        self.addCleanup(self.connection.close)
        self.connection.execute(
            "INSERT INTO universe_assets (ticker, name, category, region, underlying_index,"
            " product_type, replication_method, status, data_status, added_at)"
            " VALUES ('000001','E','RISK_ASSET','KR','K','SPOT_ETF','PHYSICAL','ACTIVE','VALID','t')")

    def _approved(self, action_type, action_id="act1"):
        self.connection.execute(
            "INSERT OR IGNORE INTO pending_actions (id, action_type, code_hash, reason, approval_status,"
            " created_at, expires_at) VALUES (?,?,?,?,'APPROVED','t','t')",
            (action_id, action_type, f"h-{action_id}", "r"))
        return action_id

    def test_clear_halt_needs_confirmation_and_clean_reconciliation(self):
        raise_flag(self.connection, "HALT", "ACCOUNT_MISMATCH", now=NOW)
        with self.assertRaises(ApprovalError):
            clear_halt(self.connection, code="ACCOUNT_MISMATCH", action_id="none",
                       reconciliation_ok=True, now=NOW)
        action = self._approved("CLEAR_HALT")
        with self.assertRaises(PermissionError):
            clear_halt(self.connection, code="ACCOUNT_MISMATCH", action_id=action,
                       reconciliation_ok=False, now=NOW)
        self.assertTrue(clear_halt(self.connection, code="ACCOUNT_MISMATCH", action_id=action,
                                   reconciliation_ok=True, now=NOW))
        self.assertEqual(active_codes(self.connection, "HALT"), ())
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = 'CLEAR_HALT'").fetchone()[0], 1)

    def test_classify_flow_checks_sign_and_releases_buy_block_when_last(self):
        for tx, amount in (("RECON:1", 500000), ("RECON:2", 300000)):
            self.connection.execute(
                "INSERT INTO cash_flows (broker_transaction_id, flow_date, occurred_at, amount, flow_type,"
                " classification_status, source, observed_at, first_observed_at) VALUES (?, '2026-11-24', 't', ?,"
                " 'UNCLASSIFIED', 'UNCLASSIFIED_CASH_FLOW', 'CASH_RECON', 't', 't')", (tx, amount))
        raise_flag(self.connection, "BUY_BLOCK", "UNCLASSIFIED_CASH_FLOW", now=NOW)
        action = self._approved("CLASSIFY_FLOW")
        with self.assertRaises(ValueError):
            classify_cash_flow(self.connection, flow_id=1, flow_type="WITHDRAWAL", action_id=action, now=NOW)
        classify_cash_flow(self.connection, flow_id=1, flow_type="DEPOSIT", action_id=action, now=NOW)
        self.assertEqual(active_codes(self.connection, "BUY_BLOCK"), ("UNCLASSIFIED_CASH_FLOW",))
        classify_cash_flow(self.connection, flow_id=2, flow_type="DISTRIBUTION", action_id=action, now=NOW)
        self.assertEqual(active_codes(self.connection, "BUY_BLOCK"), ())
        with self.assertRaises(ValueError):  # 이미 확정된 흐름은 다시 분류할 수 없음
            classify_cash_flow(self.connection, flow_id=1, flow_type="DEPOSIT", action_id=action, now=NOW)

    def test_classify_supports_adjustment_and_dismiss_without_unit_change(self):
        """v3.7.5: ADJUSTMENT(부호 무관)·DISMISS(오탐 무효화)로도 미분류를 해소합니다."""
        for tx, amount in (("RECON:1", -15), ("RECON:2", 700)):
            self.connection.execute(
                "INSERT INTO cash_flows (broker_transaction_id, flow_date, occurred_at, amount, flow_type,"
                " classification_status, source, observed_at, first_observed_at) VALUES (?, '2026-11-24',"
                " 't', ?, 'UNCLASSIFIED', 'UNCLASSIFIED_CASH_FLOW', 'CASH_RECON', 't', 't')", (tx, amount))
        raise_flag(self.connection, "BUY_BLOCK", "UNCLASSIFIED_CASH_FLOW", now=NOW)
        action = self._approved("CLASSIFY_FLOW")
        classify_cash_flow(self.connection, flow_id=1, flow_type="ADJUSTMENT", action_id=action, now=NOW)
        classify_cash_flow(self.connection, flow_id=2, flow_type="DISMISS", action_id=action, now=NOW)
        rows = [tuple(r) for r in self.connection.execute(
            "SELECT flow_type, classification_status FROM cash_flows ORDER BY id")]
        self.assertEqual(rows, [("ADJUSTMENT", "CONFIRMED"), ("UNCLASSIFIED", "DISMISSED")])
        self.assertEqual(active_codes(self.connection, "BUY_BLOCK"), ())

    def test_corporate_action_confirmation_rebases_peak_and_releases_ticker(self):
        self.connection.execute(
            "INSERT INTO positions (ticker, quantity, avg_buy_price, market_value, peak_price_adj,"
            " last_synced_at) VALUES ('000001', 5, 100, 500, 200.0, 't')")
        self.connection.execute(
            "INSERT INTO corporate_actions (provider_event_id, ticker, action_type, ex_date,"
            " detected_factor, source) VALUES ('DETECT:000001:2026-11-24','000001','CASH_DISTRIBUTION',"
            " '2026-11-24', 0.98, 'DETECTED')")
        raise_flag(self.connection, "TICKER_BLOCK", "CORPORATE_ACTION_UNVERIFIED", now=NOW, ticker="000001")
        factor = confirm_corporate_action(self.connection, corporate_action_id=1,
                                          action_id=self._approved("CA_CONFIRM"), now=NOW)
        self.assertEqual(factor, 0.98)
        self.assertAlmostEqual(
            self.connection.execute("SELECT peak_price_adj FROM positions").fetchone()[0], 196.0)
        self.assertEqual(active_codes(self.connection, "TICKER_BLOCK"), ())
        row = self.connection.execute(
            "SELECT verified, verification_source FROM corporate_actions").fetchone()
        self.assertEqual(tuple(row), (1, "OPERATOR_CONFIRMED"))
        with self.assertRaises(ValueError):
            confirm_corporate_action(self.connection, corporate_action_id=1,
                                     action_id=self._approved("CA_CONFIRM"), now=NOW)

    def test_adopt_broker_positions_marks_outside_universe_as_unmanaged(self):
        self.connection.execute(
            "INSERT INTO positions (ticker, quantity, avg_buy_price, market_value, peak_price_adj,"
            " last_synced_at) VALUES ('000001', 5, 100, 500, 100.0, 't')")
        changes = adopt_broker_positions(
            self.connection, {"000001": 3, "999999": 2},
            action_id=self._approved("RECONCILE_ADOPT"), now=NOW)
        self.assertEqual(changes, {"000001": (5, 3), "999999": (0, 2)})
        rows = {r["ticker"]: (r["quantity"], r["tracking_status"]) for r in
                self.connection.execute("SELECT ticker, quantity, tracking_status FROM positions")}
        self.assertEqual(rows, {"000001": (3, "MANAGED"), "999999": (2, "UNMANAGED")})

    def test_release_emergency_schedules_rebase_only_for_drawdown_and_refuses_when_frozen(self):
        action = self._approved("EMERGENCY_OFF")
        for key, value in (("emergency_mode", "true"), ("emergency_reason", "DRAWDOWN")):
            self.connection.execute(
                'INSERT INTO system_state ("key", value, updated_at) VALUES (?, ?, ?)', (key, value, "t"))
        raise_flag(self.connection, "BUY_BLOCK", "UNCLASSIFIED_CASH_FLOW", now=NOW)
        with self.assertRaises(ApprovalError):
            release_emergency(self.connection, action_id=action, floor_until_date="2026-12-01", now=NOW)
        self.connection.execute("UPDATE safety_flags SET cleared_at = 't'")
        self.assertTrue(release_emergency(self.connection, action_id=action,
                                          floor_until_date="2026-12-01", now=NOW))
        state = {r["key"]: r["value"] for r in self.connection.execute('SELECT "key", value FROM system_state')}
        self.assertEqual(state["emergency_mode"], "false")
        self.assertEqual(state["hwm_rebase_pending"], "true")
        self.assertEqual(state["post_emergency_floor_until"], "2026-12-01")


if __name__ == "__main__":
    unittest.main()
