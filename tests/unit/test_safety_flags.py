"""safety_flags: 원인별 등록·자동/수동 해제·호스트 상태 파일(TTL) 테스트입니다."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.db import initialize_database
from src.risk_manager import OperatingMode, resolve_operating_mode
from src.safety_flags import (
    active_codes,
    active_halt_reasons,
    clear_halt_manually,
    evaluate_auto_clear,
    has_buy_block,
    is_safe_halt,
    load_risk_flags,
    raise_flag,
    read_host_state,
    sync_host_state_flags,
    ticker_is_blocked,
)

NOW = datetime(2026, 11, 24, 6, 0, tzinfo=timezone.utc)


class SafetyFlagTests(unittest.TestCase):
    """SAFE_HALT 가 원인 목록으로 유도되는지 확인합니다."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = initialize_database(Path(self.temp.name) / "t.db")
        self.addCleanup(self.connection.close)

    def test_raise_is_idempotent_and_derives_safe_halt(self):
        self.assertFalse(is_safe_halt(self.connection))
        self.assertTrue(raise_flag(self.connection, "HALT", "CALENDAR_MISMATCH", now=NOW))
        self.assertFalse(raise_flag(self.connection, "HALT", "CALENDAR_MISMATCH", now=NOW))
        self.assertTrue(is_safe_halt(self.connection))
        self.assertEqual(active_halt_reasons(self.connection), ("CALENDAR_MISMATCH",))

    def test_multiple_reasons_must_all_clear(self):
        raise_flag(self.connection, "HALT", "CALENDAR_MISMATCH", now=NOW)
        raise_flag(self.connection, "HALT", "CLOCK_UNSYNCED", now=NOW)
        cleared = evaluate_auto_clear(self.connection, {"CALENDAR_MISMATCH": True}, now=NOW)
        self.assertEqual(cleared, ("CALENDAR_MISMATCH",))
        self.assertTrue(is_safe_halt(self.connection))
        evaluate_auto_clear(self.connection, {"CLOCK_UNSYNCED": True}, now=NOW)
        self.assertFalse(is_safe_halt(self.connection))

    def test_auto_clear_never_touches_manual_reasons(self):
        raise_flag(self.connection, "HALT", "ACCOUNT_MISMATCH", now=NOW)
        evaluate_auto_clear(self.connection, {"ACCOUNT_MISMATCH": True}, now=NOW)
        self.assertTrue(is_safe_halt(self.connection))

    def test_manual_clear_requires_manual_reason_and_reconciliation(self):
        raise_flag(self.connection, "HALT", "ACCOUNT_MISMATCH", now=NOW)
        with self.assertRaises(PermissionError):
            clear_halt_manually(self.connection, "ACCOUNT_MISMATCH", now=NOW,
                                action_id="a1", reconciliation_ok=False)
        self.assertTrue(clear_halt_manually(self.connection, "ACCOUNT_MISMATCH", now=NOW,
                                            action_id="a1", reconciliation_ok=True))
        self.assertFalse(is_safe_halt(self.connection))

    def test_operator_cannot_force_clear_an_automatic_reason(self):
        raise_flag(self.connection, "HALT", "CREDENTIALS_UNAVAILABLE", now=NOW)
        with self.assertRaises(PermissionError):
            clear_halt_manually(self.connection, "CREDENTIALS_UNAVAILABLE", now=NOW,
                                action_id="a1", reconciliation_ok=True)

    def test_buy_block_and_ticker_block_are_not_safe_halt(self):
        raise_flag(self.connection, "BUY_BLOCK", "UNCLASSIFIED_CASH_FLOW", now=NOW)
        self.connection.execute(
            "INSERT INTO universe_assets (ticker, name, category, region, underlying_index,"
            " product_type, replication_method, status, data_status, added_at)"
            " VALUES ('000001','E','RISK_ASSET','KR','K','SPOT_ETF','PHYSICAL','ACTIVE','VALID','t')")
        raise_flag(self.connection, "TICKER_BLOCK", "CORPORATE_ACTION_UNVERIFIED",
                   now=NOW, ticker="000001")
        self.assertFalse(is_safe_halt(self.connection))
        self.assertTrue(has_buy_block(self.connection))
        self.assertTrue(ticker_is_blocked(self.connection, "000001"))
        self.assertFalse(ticker_is_blocked(self.connection, "000002"))

    def test_invalid_flag_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            raise_flag(self.connection, "HALT", "NOT_A_CODE", now=NOW)
        with self.assertRaises(ValueError):
            raise_flag(self.connection, "TICKER_BLOCK", "QUOTE_INVALID", now=NOW)
        with self.assertRaises(ValueError):
            raise_flag(self.connection, "HALT", "UPS_LOW", now=NOW, ticker="000001")

    def test_risk_flags_follow_halt_reasons(self):
        self.assertIs(resolve_operating_mode(load_risk_flags(self.connection)), OperatingMode.NORMAL)
        raise_flag(self.connection, "HALT", "UPS_LOW", now=NOW)
        self.assertIs(resolve_operating_mode(load_risk_flags(self.connection)), OperatingMode.SAFE_HALT)


class HostStateTests(unittest.TestCase):
    """호스트 상태 파일(UPS·NTP)의 TTL·fail-closed 동작입니다."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name) / "run"
        self.run_dir.mkdir()
        self.connection = initialize_database(Path(self.temp.name) / "t.db")
        self.addCleanup(self.connection.close)

    def _write(self, name, payload, *, age_seconds=0):
        payload = {"updated_at": (NOW - timedelta(seconds=age_seconds)).isoformat(), **payload}
        (self.run_dir / name).write_text(json.dumps(payload), encoding="utf-8")

    def test_stale_or_missing_file_is_unknown(self):
        self.assertIsNone(read_host_state(self.run_dir / "none.json", now=NOW, ttl_seconds=120))
        self._write("ups.json", {"battery_ratio": 0.9}, age_seconds=500)
        self.assertIsNone(read_host_state(self.run_dir / "ups.json", now=NOW, ttl_seconds=120))

    def test_missing_files_fail_closed_and_healthy_files_clear_after_recovery(self):
        sync_host_state_flags(self.connection, self.run_dir, now=NOW)
        self.assertEqual(active_halt_reasons(self.connection), ("CLOCK_UNSYNCED", "UPS_LOW"))

        self._write("ups.json", {"battery_ratio": 0.95, "on_external_power": True})
        self._write("clock.json", {"synchronized": True, "offset_seconds": 0.2})
        cleared = sync_host_state_flags(self.connection, self.run_dir, now=NOW)
        self.assertEqual(set(cleared), {"UPS_LOW", "CLOCK_UNSYNCED"})
        self.assertFalse(is_safe_halt(self.connection))

    def test_ups_does_not_clear_until_margin_and_external_power(self):
        self._write("clock.json", {"synchronized": True, "offset_seconds": 0.0})
        self._write("ups.json", {"battery_ratio": 0.15, "on_external_power": False})
        sync_host_state_flags(self.connection, self.run_dir, now=NOW)
        self.assertEqual(active_halt_reasons(self.connection), ("UPS_LOW",))
        self._write("ups.json", {"battery_ratio": 0.25, "on_external_power": True})  # 0.20+0.10 미만
        sync_host_state_flags(self.connection, self.run_dir, now=NOW)
        self.assertEqual(active_halt_reasons(self.connection), ("UPS_LOW",))
        self._write("ups.json", {"battery_ratio": 0.31, "on_external_power": True})
        sync_host_state_flags(self.connection, self.run_dir, now=NOW)
        self.assertEqual(active_halt_reasons(self.connection), ())

    def test_large_clock_offset_raises_clock_unsynced(self):
        self._write("ups.json", {"battery_ratio": 0.9, "on_external_power": True})
        self._write("clock.json", {"synchronized": True, "offset_seconds": 5.0})
        sync_host_state_flags(self.connection, self.run_dir, now=NOW)
        self.assertEqual(active_codes(self.connection, "HALT"), ("CLOCK_UNSYNCED",))


if __name__ == "__main__":
    unittest.main()
