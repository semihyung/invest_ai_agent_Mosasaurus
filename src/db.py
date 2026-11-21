import sqlite3
from pathlib import Path


_SCHEMA_VERSION = 1
_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "001_init_phase1.sql"
)
_PHASE1_TABLES = {
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


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _validate_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != _SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported database schema version: {version}.")

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if tables != _PHASE1_TABLES:
        missing = sorted(_PHASE1_TABLES - tables)
        unexpected = sorted(tables - _PHASE1_TABLES)
        raise RuntimeError(
            f"Database schema mismatch; missing={missing}, unexpected={unexpected}."
        )


def initialize_database(db_path: str | Path) -> sqlite3.Connection:
    """Create the Phase 1 schema and return an open WAL-mode connection."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(path)

    try:
        journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if journal_mode.lower() != "wal":
            raise RuntimeError(f"SQLite WAL mode unavailable: {journal_mode}.")

        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            existing_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if existing_tables:
                raise RuntimeError(
                    "Refusing to migrate an unversioned database with existing tables."
                )
            connection.executescript(
                _MIGRATION_PATH.read_text(encoding="utf-8")
            )
        elif version != _SCHEMA_VERSION:
            raise RuntimeError(f"Unsupported database schema version: {version}.")

        _validate_schema(connection)
        return connection
    except (OSError, sqlite3.Error, RuntimeError):
        if connection.in_transaction:
            connection.rollback()
        connection.close()
        raise


def connect_database(db_path: str | Path) -> sqlite3.Connection:
    """Open an initialized WAL database and validate its Phase 1 schema."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Database file does not exist: {path}.")

    connection = _connect(path)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        if journal_mode.lower() != "wal":
            raise RuntimeError(f"SQLite WAL mode required, found: {journal_mode}.")
        _validate_schema(connection)
        return connection
    except (sqlite3.Error, RuntimeError):
        connection.close()
        raise
