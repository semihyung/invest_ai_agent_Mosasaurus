"""명세 6.4절 Telegram 세부: /pool 점수·순위, /health, /reconcile 읽기 대조, /clear_halt ALL, 확인 메시지 효과, EMERGENCY 매수 취소."""

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.db import connect_database, initialize_database
from src.job_handlers import cancel_active_buys, create_intraday_guard_handler, create_safety_maintenance_handler
from src.reconciliation import Position
from src.safety_flags import raise_flag
from src.telegram_actions import build_command_actions, build_summary_provider
from tests.integration.test_job_handlers import MORNING, FakeBroker, HandlerTestCase

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 25, 10, 0, tzinfo=KST)


class TelegramSpecItemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        self.conn = initialize_database(self.path)
        for ticker, cat, asset in (("069500", "RISK_ASSET", "EQUITY"), ("423160", "SAFE_PARKING", "MONEY_MARKET")):
            self.conn.execute(
                "INSERT INTO universe_assets (ticker, name, category, region, underlying_index, product_type,"
                " replication_method, status, data_status, added_at, asset_class) VALUES (?,?,?,'KR','K','SPOT_ETF',"
                "'PHYSICAL','ACTIVE','VALID','t',?)", (ticker, ticker, cat, asset))
        for i in range(130):
            day = f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"
            for ticker, close in (("069500", 100 + i), ("423160", 100)):
                self.conn.execute(
                    "INSERT INTO daily_candles (ticker, trade_date, raw_close, adjusted_close, volume, source,"
                    " source_timestamp) VALUES (?,?,?,?,1,'KIS','t')", (ticker, day, close, close))
        self.conn.commit()
        self.broker = FakeBroker()
        self.actions = build_command_actions(
            lambda: connect_database(self.path), approvals_dir=self.tmp.name, clock=lambda: NOW, broker=self.broker,
            parking_ticker="423160", token_expiry=lambda: NOW + timedelta(hours=5), run_dir=self.tmp.name)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_cmd(self, text):
        return self.actions[text.split()[0]]("chat", text)

    def approve(self, command, action_type):
        digest = hashlib.sha256(command.encode()).hexdigest()
        self.conn.execute(
            "INSERT INTO pending_actions (id, action_type, args_hash, code_hash, reason, approval_status, created_at,"
            " expires_at, confirmed_at) VALUES (?,?,?,?,'t','APPROVED','t','t','2026-11-25T01:00:00')",
            (f"a-{digest[:8]}", action_type, digest, digest))
        self.conn.commit()

    def test_pool_shows_momentum_score_and_rank(self):
        reply = self.run_cmd("/pool")
        self.assertRegex(reply, r"069500 RISK_ASSET VALID .* 점수 [0-9.]+ \(순위 1\)")
        self.assertIn("423160 SAFE_PARKING", reply)

    def test_health_lists_system_and_token_status(self):
        reply = self.run_cmd("/health")
        for expected in ("CPU 부하", "DB 무결성: ok", "KIS 토큰 만료", "UPS:", "NTP:"):
            self.assertIn(expected, reply)

    def test_reconcile_compares_read_only_and_never_writes(self):
        self.conn.execute("INSERT INTO positions (ticker, tracking_status, quantity, avg_buy_price, market_value,"
                          " peak_price_adj, last_synced_at) VALUES ('069500','MANAGED',10,100,1000,100,'t')")
        self.conn.commit()
        self.broker._positions = [Position("069500", 7)]
        before = self.conn.execute("SELECT COUNT(*) FROM system_state").fetchone()[0]
        reply = self.run_cmd("/reconcile")
        self.assertIn("불일치", reply)
        self.assertIn("069500", reply)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM system_state").fetchone()[0], before)
        self.broker._positions = [Position("069500", 10)]
        self.assertIn("일치합니다", self.run_cmd("/reconcile"))

    def test_clear_halt_all_clears_only_manual_reasons(self):
        raise_flag(self.conn, "HALT", "ACCOUNT_MISMATCH", now=NOW)
        raise_flag(self.conn, "HALT", "DB_INTEGRITY", now=NOW)
        raise_flag(self.conn, "HALT", "CREDENTIALS_UNAVAILABLE", now=NOW)   # 자동 해제 원인은 강제 해제 불가
        self.conn.execute("INSERT INTO system_state (key, value, updated_at) VALUES ('reconciliation_ok','true','t')")
        self.conn.commit()
        self.approve("/clear_halt all", "CLEAR_HALT")
        reply = self.run_cmd("/clear_halt all")
        self.assertIn("ACCOUNT_MISMATCH", reply)
        remaining = [r[0] for r in self.conn.execute("SELECT code FROM safety_flags WHERE cleared_at IS NULL")]
        self.assertEqual(remaining, ["CREDENTIALS_UNAVAILABLE"])

    def test_confirmation_summaries_show_the_effect(self):
        provider = build_summary_provider(lambda: connect_database(self.path), self.tmp.name)
        self.conn.execute("INSERT INTO positions (ticker, tracking_status, quantity, avg_buy_price, market_value,"
                          " peak_price_adj, last_synced_at) VALUES ('423160','MANAGED',10,100,1000,120,'t')")
        self.conn.commit()
        self.assertIn("423160", provider("/training on"))          # 120 대비 100 = -16.7% → 즉시 스탑 도달
        raise_flag(self.conn, "HALT", "ACCOUNT_MISMATCH", now=NOW)
        self.assertIn("ACCOUNT_MISMATCH", provider("/clear_halt all"))
        self.assertIn("매수가 재개", provider("/resume"))


class EmergencyTests(HandlerTestCase):
    """EMERGENCY 중 미체결 매수 취소(6.2절)와 훈련 모드 7일 경과 알림(6.4절)."""

    def add_order(self, oid, kis, side):
        self.conn.execute(
            "INSERT INTO order_intents (parent_intent_id, idempotency_key, cycle_id, intent_type, ticker, side,"
            " requested_qty, intent_payload_hash, status, created_at, updated_at) VALUES (?,?,'c','STRATEGY_REBALANCE',"
            "'069500',?,5,'h','IN_PROGRESS','t','t')", (f"p{oid}", f"k{oid}", side))
        self.conn.execute(
            "INSERT INTO orders (order_id, parent_intent_id, attempt_id, kis_order_no, ticker, side, requested_qty,"
            " remaining_qty, order_type, order_price, status, strategy_reason, updated_at) VALUES (?,?,?,?,"
            "'069500',?,5,5,'LIMIT',100,'ACCEPTED','r','t')", (oid, f"p{oid}", f"at{oid}", kis, side))
        self.conn.commit()

    def test_emergency_cancels_unfilled_buys_only(self):
        self.add_order("b1", "9001", "BUY")
        self.add_order("s1", "9002", "SELL")
        broker = FakeBroker()
        self.assertEqual(cancel_active_buys(self.conn, broker, MORNING), 1)
        self.assertEqual(broker.cancelled, ["9001"])
        self.assertEqual(self.conn.execute("SELECT status FROM orders WHERE order_id = 's1'").fetchone()[0], "ACCEPTED")

    def test_guard_cancels_buys_while_emergency_mode_is_on(self):
        self.conn.execute("INSERT INTO system_state (key, value, updated_at) VALUES ('emergency_mode','true','t')")
        self.add_order("b1", "7001", "BUY")
        broker = FakeBroker(price=95.0)
        create_intraday_guard_handler(self.factory, broker, clock=lambda: MORNING)()
        self.assertEqual(broker.cancelled, ["7001"])

    def test_indefinite_training_alerts_daily_after_seven_days(self):
        self.conn.execute("INSERT INTO system_state (key, value, updated_at) VALUES ('training_mode','true',?)",
                          ((MORNING - timedelta(days=8)).isoformat(),))
        self.conn.execute("INSERT INTO system_state (key, value, updated_at) VALUES ('training_until','','t')")
        self.conn.commit()
        alerts = []
        handler = create_safety_maintenance_handler(self.factory, run_dir=None, clock=lambda: MORNING,
                                                    notify=lambda s, m: alerts.append(m))
        handler()
        handler()                       # 같은 날 두 번째는 알리지 않는다
        self.assertEqual(len([a for a in alerts if "훈련 모드" in a]), 1)


if __name__ == "__main__":
    unittest.main()
