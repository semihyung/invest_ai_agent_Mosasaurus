"""실제 전략 함수와 SQLite를 연결하는 스케줄 작업을 검증합니다."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.db import connect_database, initialize_database
from src.scheduler_jobs import create_weekly_preview_handler


UTC = timezone.utc


class SchedulerJobTests(unittest.TestCase):
    """완료 일봉 기반 주간 프리뷰의 저장·멱등성을 검사합니다."""

    def setUp(self):
        """2개 자산의 동일한 200개 평일 이력을 DB에 준비합니다."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "mosasaurus.db"
        connection = initialize_database(self.db_path)
        self.now = datetime(2026, 11, 28, 1, 0, tzinfo=UTC)
        tickers = (
            ("423160", "SAFE_PARKING", "PARKING"),
            ("111111", "RISK_ASSET", "RISK"),
        )
        for ticker, category, index in tickers:
            connection.execute(
                """
                INSERT INTO universe_assets (
                    ticker, name, category, region, underlying_index,
                    product_type, replication_method, status, data_status, added_at
                ) VALUES (?, ?, ?, 'KR', ?, 'SPOT_ETF', 'PHYSICAL',
                          'ACTIVE', 'VALID', ?)
                """,
                (
                    ticker,
                    ticker,
                    category,
                    index,
                    self.now.isoformat(),
                ),
            )
        dates = []
        cursor = self.now.date() - timedelta(days=1)
        while len(dates) < 200:
            if cursor.weekday() < 5:
                dates.append(cursor.isoformat())
            cursor -= timedelta(days=1)
        dates.reverse()
        for ticker, _, index in tickers:
            for position, trade_date in enumerate(dates):
                price = 10000 + position * (50 if index == "RISK" else 20)
                connection.execute(
                    """
                    INSERT INTO daily_candles (
                        ticker, trade_date, raw_open, raw_high, raw_low, raw_close,
                        adjusted_close, volume, source, source_timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 600000, 'TEST', ?)
                    """,
                    (
                        ticker,
                        trade_date,
                        price,
                        price,
                        price,
                        price,
                        price,
                        self.now.isoformat(),
                    ),
                )
        connection.commit()
        connection.close()
        self.rules = {
            "portfolio": {
                "primary_parking_ticker": "423160",
                "max_order_participation_rate": 0.01,
                "concentration": {"max_slots_per_region": 2},
            },
            "momentum": {"min_volatility_floor": 0.000001},
            "universe": {
                "min_history_sessions": 200,
                "min_avg_turnover_20d_krw": 5_000_000_000,
            },
        }

    def _handler(self):
        """테스트 DB와 고정 시계를 사용하는 주간 프리뷰 콜백을 만듭니다."""
        return create_weekly_preview_handler(
            lambda: connect_database(self.db_path),
            self.rules,
            now=lambda: self.now,
        )

    def test_weekly_preview_uses_quant_and_portfolio_modules(self):
        """적격 후보 점수와 슬롯 배정을 weekly_previews에 저장합니다."""
        self._handler()()

        connection = connect_database(self.db_path)
        try:
            row = connection.execute(
                "SELECT base_candle_date, ranked_candidates_json, "
                "projected_slots_json, status FROM weekly_previews"
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(row["base_candle_date"], "2026-11-27")
        self.assertEqual(row["status"], "CREATED")
        self.assertEqual(
            json.loads(row["ranked_candidates_json"])[0]["ticker"],
            "111111",
        )
        self.assertEqual(
            json.loads(row["projected_slots_json"])[0]["ticker"],
            "111111",
        )

    def test_repeated_preview_is_idempotent_and_changed_payload_conflicts(self):
        """동일 프리뷰는 중복 저장하지 않고 다른 계산 결과는 충돌로 멈춥니다."""
        handler = self._handler()
        handler()
        handler()
        connection = connect_database(self.db_path)
        try:
            connection.execute(
                "UPDATE universe_assets SET data_status = 'DATA_INVALID' "
                "WHERE ticker = '111111'"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(RuntimeError, "WEEKLY_PREVIEW_CONFLICT"):
            handler()
        connection = connect_database(self.db_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM weekly_previews").fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_invalid_parking_asset_blocks_preview(self):
        """기준 파킹 자산 데이터가 무효면 점수 비교를 중단합니다."""
        connection = connect_database(self.db_path)
        try:
            connection.execute(
                "UPDATE universe_assets SET data_status = 'DATA_INVALID' "
                "WHERE ticker = '423160'"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(RuntimeError, "Parking asset is missing or invalid"):
            self._handler()()


if __name__ == "__main__":
    unittest.main()
