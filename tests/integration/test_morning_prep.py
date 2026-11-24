"""morning_prep: 일봉 Upsert·소급 조정(분배락) 감지·종목 격리를 검증합니다."""

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.db import connect_database, initialize_database
from src.market_data import DailyCandle
from src.morning_prep import create_morning_prep_handler, detect_retroactive_adjustment, series_has_outlier
from src.safety_flags import active_codes

NOW = datetime(2026, 11, 25, 8, 30, tzinfo=timezone(timedelta(hours=9)))


def series(adj_factor=1.0, n=200, start=date(2026, 1, 1)):
    """원주가 100, 수정주가 100×factor 인 n일 시계열(마지막 날은 항상 A=1.0)."""
    out = []
    for i in range(n):
        d = (start + timedelta(days=i)).isoformat()
        f = adj_factor if i < n - 1 else 1.0
        out.append(DailyCandle("069500", d, 100.0 * f, 1000, 100, 101, 99, 100.0))
    return out


class DetectionTests(unittest.TestCase):
    def test_no_change(self):
        fresh = series()
        stored = {c.trade_date: c.adjusted_close / c.raw_close for c in fresh}
        self.assertEqual(detect_retroactive_adjustment(stored, fresh, tolerance=1e-6)[0], False)

    def test_retroactive_adjustment_gives_factor_and_ex_date(self):
        old = series()
        stored = {c.trade_date: 1.0 for c in old}
        fresh = series(adj_factor=0.98)
        retro, factor, ex_date = detect_retroactive_adjustment(stored, fresh, tolerance=1e-6)
        self.assertTrue(retro)
        self.assertAlmostEqual(factor, 0.98)
        self.assertEqual(ex_date, fresh[-1].trade_date)

    def test_outlier(self):
        bad = series()
        bad[50] = DailyCandle("069500", bad[50].trade_date, 150.0, 1, 100, 101, 99, 100.0)
        self.assertTrue(series_has_outlier(bad))
        self.assertFalse(series_has_outlier(series()))


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        self.conn = initialize_database(self.path)
        self.conn.execute(
            "INSERT INTO universe_assets (ticker, name, category, region, underlying_index, product_type,"
            " replication_method, status, data_status, added_at, asset_class) VALUES ('069500','E','RISK_ASSET','KR',"
            "'K','SPOT_ETF','PHYSICAL','ACTIVE','DATA_INVALID','t','EQUITY')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_prep(self, source):
        create_morning_prep_handler(lambda: connect_database(self.path), source, clock=lambda: NOW)()

    def count(self, sql):
        return self.conn.execute(sql).fetchone()[0]

    def test_first_run_upserts_200_and_marks_valid(self):
        self.run_prep(lambda t: series())
        self.assertEqual(self.count("SELECT COUNT(*) FROM daily_candles"), 200)
        self.assertEqual(self.conn.execute("SELECT data_status FROM universe_assets").fetchone()[0], "VALID")

    def test_retroactive_adjustment_quarantines_only_that_ticker_with_candidate(self):
        self.run_prep(lambda t: series())
        self.run_prep(lambda t: series(adj_factor=0.98))
        self.assertIn("CORPORATE_ACTION_UNVERIFIED", active_codes(self.conn, "TICKER_BLOCK", ticker="069500"))
        self.assertEqual(active_codes(self.conn, "HALT"), ())
        row = self.conn.execute("SELECT verified, action_type, detected_factor FROM corporate_actions").fetchone()
        self.assertEqual((row[0], row[1]), (0, "CASH_DISTRIBUTION"))
        self.assertEqual(self.conn.execute("SELECT data_status FROM universe_assets").fetchone()[0], "DATA_INVALID")
        self.run_prep(lambda t: series(adj_factor=0.98))  # 같은 상태 재실행은 후보를 늘리지 않는다
        self.assertEqual(self.count("SELECT COUNT(*) FROM corporate_actions"), 1)

    def test_short_history_or_fetch_failure_isolates_ticker(self):
        self.run_prep(lambda t: series(n=100))
        self.assertIn("PRICE_SERIES_INVALID", active_codes(self.conn, "TICKER_BLOCK", ticker="069500"))
        def boom(t):
            raise TimeoutError("down")
        self.run_prep(boom)
        self.assertEqual(self.count("SELECT COUNT(*) FROM daily_candles"), 0)


if __name__ == "__main__":
    unittest.main()
