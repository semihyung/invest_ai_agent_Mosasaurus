import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA_VERSION = 2
_MIGRATIONS = {
    1: (
        "001_init_phase1",
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "001_init_phase1.sql",
    ),
    2: (
        "002_verified_universe_classification",
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "002_verified_universe_classification.sql",
    ),
}
_PHASE1_TABLES = {
    "system_state",
    "credentials_state",
    "universe_assets",
    "universe_history",
    "daily_candles",
    "weekly_previews",
    "positions",
    "portfolio_slots",
    "order_intents",
    "orders",
    "order_events",
    "trade_fills",
    "daily_portfolio",
    "cash_flows",
    "corporate_actions",
    "system_events",
    "schema_migrations",
    "deployment_events",
    "audit_events",
    "exit_obligations",
    "llm_logs",
    "risk_events",
    "pending_actions",
}
_CLASSIFICATION_COLUMNS = {
    "leverage_multiplier",
    "is_inverse",
    "is_futures_based",
    "classification_verified",
    "classification_source",
    "verified_by",
    "verified_at",
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
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(universe_assets)")
    }
    if not _CLASSIFICATION_COLUMNS.issubset(columns):
        missing = sorted(_CLASSIFICATION_COLUMNS - columns)
        raise RuntimeError(f"Universe classification schema is incomplete: {missing}.")
    for name, path in _MIGRATIONS.values():
        row = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = ?",
            (name,),
        ).fetchone()
        if row is None or row["checksum"] != _migration_checksum(path):
            raise RuntimeError(f"Migration record is missing or invalid: {name}.")


def _migration_checksum(path: Path) -> str:
    """마이그레이션 파일 내용의 SHA-256 지문을 계산합니다."""
    contents = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(contents.encode("utf-8")).hexdigest()


def _record_initial_migration(connection: sqlite3.Connection) -> None:
    """기존 v1 DB의 임시 체크섬을 현재 초기 마이그레이션 지문으로 교체합니다."""
    name, path = _MIGRATIONS[1]
    checksum = _migration_checksum(path)
    row = connection.execute(
        "SELECT checksum FROM schema_migrations WHERE version = ?",
        (name,),
    ).fetchone()
    if row is None:
        with connection:
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at, checksum) "
                "VALUES (?, ?, ?)",
                (name, datetime.now(timezone.utc).isoformat(), checksum),
            )
    elif row["checksum"] == "pending":
        with connection:
            connection.execute(
                "UPDATE schema_migrations SET checksum = ?, applied_at = ? "
                "WHERE version = ?",
                (checksum, datetime.now(timezone.utc).isoformat(), name),
            )
    elif row["checksum"] != checksum:
        raise RuntimeError(f"Initial migration checksum mismatch: {name}.")


def _migration_statements(script: str) -> list[str]:
    """SQL 파일을 SQLite가 완성된 구문으로 인식하는 단위로 나눕니다."""
    statements = []
    current = ""
    for line in script.splitlines():
        current += line + "\n"
        if sqlite3.complete_statement(current):
            statement = current.strip()
            if statement:
                statements.append(statement)
            current = ""
    if current.strip():
        raise RuntimeError("Migration contains an incomplete SQL statement.")
    return statements


def _apply_migration(
    connection: sqlite3.Connection,
    *,
    version: int,
) -> None:
    """DDL, 버전 번호와 체크섬 기록을 하나의 트랜잭션으로 적용합니다."""
    name, path = _MIGRATIONS[version]
    statements = _migration_statements(path.read_text(encoding="utf-8"))
    checksum = _migration_checksum(path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at, checksum) "
            "VALUES (?, ?, ?)",
            (name, datetime.now(timezone.utc).isoformat(), checksum),
        )
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise


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
                _MIGRATIONS[1][1].read_text(encoding="utf-8")
            )
            _record_initial_migration(connection)
            version = 1
        elif version > _SCHEMA_VERSION:
            raise RuntimeError(f"Unsupported database schema version: {version}.")

        if version == 1:
            _record_initial_migration(connection)
            _apply_migration(connection, version=2)

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
