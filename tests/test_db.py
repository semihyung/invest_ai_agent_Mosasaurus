import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.db import connect_database, initialize_database


PHASE1_TABLES = {
    "system_state",
    "credentials_state",
    "universe_assets",
    "universe_history",
    "daily_candles",
    "weekly_previews",
    "positions",
    "portfolio_slots",
    "orders",
    "order_events",
    "trade_fills",
    "daily_portfolio",
    "cash_flows",
    "corporate_actions",
    "system_events",
}


class DatabaseInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "nested" / "mosasaurus.db"

    def test_initializes_phase1_schema_in_wal_mode(self):
        connection = initialize_database(self.db_path)
        self.addCleanup(connection.close)

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

        self.assertSetEqual(tables, PHASE1_TABLES)
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_reinitializing_a_valid_database_preserves_schema(self):
        first_connection = initialize_database(self.db_path)
        first_connection.close()

        connection = initialize_database(self.db_path)
        self.addCleanup(connection.close)

        self.assertEqual(
            connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )
        self.assertSetEqual(
            {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            },
            PHASE1_TABLES,
        )

    def test_refuses_market_orders(self):
        connection = initialize_database(self.db_path)
        self.addCleanup(connection.close)

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO orders (
                    order_id, idempotency_key, ticker, side, requested_qty,
                    filled_qty, remaining_qty, order_type, order_price, status,
                    strategy_reason, submitted_at, updated_at
                ) VALUES (
                    'order-1', 'key-1', '000001', 'BUY', 1,
                    0, 1, 'MARKET', 100, 'PENDING',
                    'test', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                )
                """
            )

    def test_refuses_unversioned_database_with_existing_tables(self):
        self.db_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("CREATE TABLE legacy_data (value TEXT)")
            connection.commit()

        with self.assertRaisesRegex(RuntimeError, "unversioned"):
            initialize_database(self.db_path)

    def test_refuses_unsupported_schema_version(self):
        self.db_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA user_version = 2")
            connection.commit()

        with self.assertRaisesRegex(RuntimeError, "Unsupported database schema version"):
            initialize_database(self.db_path)

    def test_connect_requires_an_initialized_database(self):
        with self.assertRaises(FileNotFoundError):
            connect_database(self.db_path)


if __name__ == "__main__":
    unittest.main()
