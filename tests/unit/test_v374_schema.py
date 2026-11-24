"""마이그레이션 003·004(명세 v3.7.4·v3.7.5 스키마)의 제약·트리거·partial index 검증입니다."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.db import initialize_database

NOW = "2026-01-05T06:00:00Z"
ROOT = Path(__file__).resolve().parents[2]


class SchemaV3Tests(unittest.TestCase):
    """새 스키마가 런타임 상태 전이와 충돌 없이 불변식을 강제하는지 봅니다."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.connection = initialize_database(Path(self.directory.name) / "t.db")
        self.addCleanup(self.connection.close)
        self.connection.execute(
            "INSERT INTO universe_assets (ticker, name, category, region, underlying_index,"
            " product_type, replication_method, status, data_status, added_at)"
            " VALUES ('000001','ETF','RISK_ASSET','KR','K200','SPOT_ETF','PHYSICAL','ACTIVE','VALID',?)",
            (NOW,),
        )

    def _intent(self, key="k1", ticker="000001", side="SELL", qty=10, intent_type="RISK_REDUCTION",
                obligation_id=None):
        self.connection.execute(
            "INSERT INTO order_intents (parent_intent_id, idempotency_key, cycle_id, intent_type,"
            " ticker, side, requested_qty, obligation_id, intent_payload_hash, status, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"p-{key}", key, "W2026-02", intent_type, ticker, side, qty, obligation_id, "h",
             "CREATED", NOW, NOW),
        )
        return f"p-{key}"

    def _order(self, order_id, parent, status="SUBMITTING", ticker="000001", side="SELL"):
        self.connection.execute(
            "INSERT INTO orders (order_id, parent_intent_id, attempt_id, ticker,"
            " side, requested_qty, filled_qty, remaining_qty, order_type, order_price, status,"
            " strategy_reason, updated_at) VALUES (?,?,?,?,?,10,0,10,'LIMIT',1000,?,?,?)",
            (order_id, parent, f"a-{order_id}", ticker, side, status, "t", NOW),
        )

    def test_foreign_keys_are_clean_and_orders_have_no_replacement_chain(self):
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        targets = {row["table"] for row in self.connection.execute("PRAGMA foreign_key_list(orders)")}
        self.assertEqual(targets, {"order_intents", "universe_assets", "portfolio_slots"})
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(orders)")}
        self.assertNotIn("replaces_order_id", columns)  # v3.7.5: 정정 체인 삭제

    def test_only_one_active_original_attempt_per_intent(self):
        parent = self._intent()
        self._order("o1", parent)
        with self.assertRaises(sqlite3.IntegrityError):
            self._order("o2", parent, status="PENDING_CONFIRMATION")

    def test_new_attempt_allowed_only_after_previous_one_is_terminal(self):
        parent = self._intent()
        self._order("o1", parent, status="CONFIRMED_NOT_ACCEPTED")
        self._order("o2", parent)
        with self.assertRaises(sqlite3.IntegrityError):
            self._order("o3", parent, status="ACCEPTED")  # 활성 attempt 는 의도당 1건(단일 인덱스)
        indexes = {row[1] for row in self.connection.execute("PRAGMA index_list(orders)")}
        self.assertIn("ux_orders_one_active_attempt", indexes)
        self.assertNotIn("ux_orders_one_active_replacement", indexes)

    def test_order_must_match_intent_ticker_and_side(self):
        parent = self._intent(side="SELL")
        with self.assertRaises(sqlite3.DatabaseError):
            self._order("o1", parent, side="BUY")

    def test_terminal_status_is_immutable_but_late_fill_may_increase_quantity(self):
        parent = self._intent()
        self._order("o1", parent, status="CANCEL_PENDING")
        self.connection.execute("UPDATE orders SET status = 'CANCELED' WHERE order_id = 'o1'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.connection.execute("UPDATE orders SET status = 'FILLED' WHERE order_id = 'o1'")
        # LATE_FILL_CORRECTION: 체결 수량은 늘리되 잔량은 0 으로 고정해야 한다.
        with self.assertRaises(sqlite3.DatabaseError):
            self.connection.execute(
                "UPDATE orders SET filled_qty = 3, remaining_qty = 7 WHERE order_id = 'o1'")
        self.connection.execute(
            "UPDATE orders SET filled_qty = 3, remaining_qty = 0 WHERE order_id = 'o1'")
        with self.assertRaises(sqlite3.DatabaseError):  # 체결 수량 감소 거부
            self.connection.execute(
                "UPDATE orders SET filled_qty = 1, remaining_qty = 0 WHERE order_id = 'o1'")
        with self.assertRaises(sqlite3.DatabaseError):  # 잔량 되살리기 거부
            self.connection.execute("UPDATE orders SET remaining_qty = 5 WHERE order_id = 'o1'")
        # 수량과 무관한 갱신(조회 시각 기록 등)은 잔량이 남은 종결 주문에도 허용된다.
        self._order("o2", self._intent("k2"), status="CANCEL_PENDING")
        self.connection.execute("UPDATE orders SET status = 'CANCELED' WHERE order_id = 'o2'")
        self.connection.execute(
            "UPDATE orders SET last_broker_checked_at = ? WHERE order_id = 'o2'", (NOW,))

    def test_created_order_has_no_submitted_at_and_status_enum_is_enforced(self):
        parent = self._intent()
        self._order("o1", parent, status="CREATED")
        row = self.connection.execute("SELECT submitted_at, created_at FROM orders").fetchone()
        self.assertIsNone(row["submitted_at"])
        self.assertIsNotNone(row["created_at"])
        with self.assertRaises(sqlite3.IntegrityError):
            self._order("o9", parent, status="UNRESOLVED_EXIT")

    def test_duplicate_fill_and_cumulative_quantity_are_rejected(self):
        parent = self._intent()
        self._order("o1", parent, status="PARTIALLY_FILLED")
        insert = ("INSERT INTO trade_fills (order_id, kis_execution_id, ticker, side, qty,"
                  " cumulative_qty, price, executed_at) VALUES ('o1',?,'000001','SELL',?,?,?,?)")
        self.connection.execute(insert, ("o1:4", 4, 4, 1000.5, NOW))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert, ("o1:4", 4, 4, 1000.5, NOW))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert, ("o1:4b", 4, 4, 1000.5, NOW))

    def test_safety_flag_is_unique_while_active_and_halt_view_tracks_it(self):
        insert = ("INSERT INTO safety_flags (flag_type, code, ticker, auto_clearable,"
                  " created_at, last_seen_at) VALUES (?,?,?,?,?,?)")
        self.connection.execute(insert, ("HALT", "ACCOUNT_MISMATCH", None, 0, NOW, NOW))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert, ("HALT", "ACCOUNT_MISMATCH", None, 0, NOW, NOW))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM halt_reasons").fetchone()[0], 1)
        self.connection.execute("UPDATE safety_flags SET cleared_at = ?", (NOW,))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM halt_reasons").fetchone()[0], 0)
        self.connection.execute(insert, ("HALT", "ACCOUNT_MISMATCH", None, 0, NOW, NOW))  # 해제 후 재발생 가능

    def test_ticker_block_requires_ticker_and_other_flags_forbid_it(self):
        insert = ("INSERT INTO safety_flags (flag_type, code, ticker, auto_clearable,"
                  " created_at, last_seen_at) VALUES (?,?,?,?,?,?)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert, ("TICKER_BLOCK", "QUOTE_INVALID", None, 1, NOW, NOW))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert, ("HALT", "UPS_LOW", "000001", 1, NOW, NOW))
        self.connection.execute(insert, ("TICKER_BLOCK", "QUOTE_INVALID", "000001", 1, NOW, NOW))

    def test_verified_assets_need_provenance_and_equity_class_rule(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE universe_assets SET classification_verified = 1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE universe_assets SET asset_class = 'BOND'")
        self.connection.execute(
            "UPDATE universe_assets SET category = 'SAFE_PARKING', asset_class = 'MONEY_MARKET'")

    def test_positions_accept_unmanaged_holdings_outside_the_universe(self):
        self.connection.execute(
            "INSERT INTO positions (ticker, tracking_status, quantity, avg_buy_price, market_value,"
            " last_synced_at) VALUES ('999999','UNMANAGED',3,100,300,?)", (NOW,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO positions (ticker, quantity, avg_buy_price, market_value, last_synced_at)"
                " VALUES ('000001',1,100,100,?)", (NOW,))  # MANAGED 는 peak_price_adj 필수

    def test_cash_flow_sign_and_classification_consistency(self):
        insert = ("INSERT INTO cash_flows (broker_transaction_id, flow_date, occurred_at, amount,"
                  " flow_type, classification_status, source, observed_at, first_observed_at)"
                  " VALUES (?,?,?,?,?,?,'CASH_RECON',?,?)")
        self.connection.execute(
            insert, ("RECON:1", "2026-01-05", NOW, 500000, "DEPOSIT", "CONFIRMED", NOW, NOW))
        # v3.7.5: 새 유형·상태는 허용한다(부호 무관 ADJUSTMENT, 1회 관측 PROVISIONAL, 무효화 DISMISSED).
        self.connection.execute(
            insert, ("RECON:A", "2026-01-05", NOW, -15, "ADJUSTMENT", "CONFIRMED", NOW, NOW))
        self.connection.execute(
            insert, ("RECON:P", "2026-01-05", NOW, 700, "UNCLASSIFIED", "PROVISIONAL", NOW, NOW))
        self.connection.execute(
            insert, ("RECON:D", "2026-01-05", NOW, 700, "UNCLASSIFIED", "DISMISSED", NOW, NOW))
        for tx, amount, flow_type, status in (
            ("RECON:2", 500000, "WITHDRAWAL", "CONFIRMED"),
            ("RECON:3", 500000, "UNCLASSIFIED", "CONFIRMED"),
            ("RECON:4", 0, "DEPOSIT", "CONFIRMED"),
            ("RECON:5", 500000, "DEPOSIT", "PROVISIONAL"),
            ("RECON:6", 500000, "DEPOSIT", "UNCLASSIFIED_CASH_FLOW"),
        ):
            with self.assertRaises(sqlite3.IntegrityError, msg=tx):
                self.connection.execute(
                    insert, (tx, "2026-01-05", NOW, amount, flow_type, status, NOW, NOW))

    def test_weekly_preview_revisions_share_a_target_week(self):
        insert = ("INSERT INTO weekly_previews (preview_id, preview_revision, base_candle_date,"
                  " ranked_candidates_json, projected_slots_json, projected_trades_json, status,"
                  " created_at) VALUES ('2026-W02',?,'2026-01-02','[]','[]','[]',?,?)")
        self.connection.execute(insert, (1, "SENT", NOW))
        self.connection.execute(insert, (2, "CREATED", NOW))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert, (2, "CREATED", NOW))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert, (3, "OVERRIDDEN", NOW))

    def test_slot_cannot_share_a_ticker(self):
        insert = ("INSERT INTO portfolio_slots (slot_id, target_weight, ticker, assigned_at,"
                  " assignment_reason) VALUES (?,?,?,?,'t')")
        self.connection.execute(insert, ("S1", 0.45, "000001", NOW))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert, ("S2", 0.30, "000001", NOW))

    def test_pending_action_code_hash_is_unique_and_action_type_is_enumerated(self):
        insert = ("INSERT INTO pending_actions (id, action_type, code_hash, reason, approval_status,"
                  " created_at, expires_at) VALUES (?,?,?,'r','PENDING',?,?)")
        self.connection.execute(insert, ("a1", "CLEAR_HALT", "h1", NOW, NOW))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert, ("a2", "CLEAR_HALT", "h1", NOW, NOW))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert, ("a3", "FORMAT_DISK", "h3", NOW, NOW))

    def test_exit_obligation_allows_one_open_per_ticker_and_new_statuses(self):
        insert = ("INSERT INTO exit_obligations (obligation_id, ticker, source_event_id, required_qty,"
                  " status, reason_code, created_at) VALUES (?,'000001',?,5,?,'STOP',?)")
        self.connection.execute(insert, ("x1", "e1", "OPEN", NOW))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert, ("x2", "e2", "OPEN", NOW))
        self.connection.execute("UPDATE exit_obligations SET status = 'SUPERSEDED'")
        self.connection.execute(insert, ("x2", "e2", "OPEN", NOW))
        self.connection.execute("UPDATE exit_obligations SET superseded_by = 'x2' WHERE obligation_id = 'x1'")

    def test_retry_intent_requires_obligation_and_price_policy_has_no_reprice(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._intent("r1", intent_type="EXIT_OBLIGATION_RETRY")  # obligation_id 필수
        self.connection.execute(
            "INSERT INTO exit_obligations (obligation_id, ticker, source_event_id, required_qty,"
            " status, reason_code, created_at) VALUES ('x1','000001','e1',5,'OPEN','STOP',?)", (NOW,))
        self._intent("r2", intent_type="EXIT_OBLIGATION_RETRY", obligation_id="x1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE order_intents SET price_policy = 'EMERGENCY_REPRICE'")

    def test_obligation_required_qty_is_immutable_but_filled_qty_can_grow(self):
        self.connection.execute(
            "INSERT INTO exit_obligations (obligation_id, ticker, source_event_id, required_qty,"
            " status, reason_code, created_at) VALUES ('x1','000001','e1',100,'OPEN','STOP',?)", (NOW,))
        with self.assertRaises(sqlite3.DatabaseError):
            self.connection.execute("UPDATE exit_obligations SET required_qty = 40")
        self.connection.execute("UPDATE exit_obligations SET filled_qty = 60")
        columns = {r["name"] for r in self.connection.execute("PRAGMA table_info(exit_obligations)")}
        self.assertNotIn("last_order_id", columns)

    def test_daily_portfolio_has_restatement_columns(self):
        columns = {r["name"] for r in self.connection.execute("PRAGMA table_info(daily_portfolio)")}
        self.assertTrue({"unclassified_excluded_amount", "restated"} <= columns)

    def test_upgrade_from_v2_preserves_rows(self):
        path = Path(self.directory.name) / "v2.db"
        with closing(sqlite3.connect(path)) as legacy:
            legacy.executescript((ROOT / "migrations" / "001_init_phase1.sql").read_text(encoding="utf-8"))
            legacy.executescript(
                (ROOT / "migrations" / "002_verified_universe_classification.sql").read_text(encoding="utf-8"))
            # 실제 v2 DB 는 001·002 적용 기록이 모두 있습니다(002 는 db.py 가 기록).
            from src.db import _migration_checksum, _MIGRATIONS
            for index in (1, 2):
                name, mpath = _MIGRATIONS[index]
                legacy.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version, applied_at, checksum) VALUES (?,?,?)",
                    (name, NOW, _migration_checksum(mpath)))
            legacy.execute("PRAGMA user_version = 2")
            legacy.execute(
                "INSERT INTO universe_assets (ticker, name, category, region, underlying_index,"
                " product_type, replication_method, status, data_status, added_at)"
                " VALUES ('000002','Old','RISK_ASSET','US','SP500','SPOT_ETF','PHYSICAL','ACTIVE','VALID',?)",
                (NOW,))
            legacy.commit()
        with closing(initialize_database(path)) as upgraded:
            row = upgraded.execute(
                "SELECT underlying_index_family, asset_class, classification_verified FROM universe_assets"
            ).fetchone()
            self.assertEqual(tuple(row), ("SP500", "EQUITY", 0))
            self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
            flags = {r[0]: r[1] for r in upgraded.execute(
                "SELECT version, backward_compatible FROM schema_migrations")}
            self.assertEqual(flags["003_spec_v374_schema"], 0)
            self.assertEqual(flags["004_spec_v375_schema"], 0)


if __name__ == "__main__":
    unittest.main()
