import hashlib
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA_VERSION = 4
_BUSY_TIMEOUT_MS = 5000
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
    3: (
        "003_spec_v374_schema",
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "003_spec_v374_schema.sql",
    ),
    4: (
        "004_spec_v375_schema",
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "004_spec_v375_schema.sql",
    ),
}
# 테이블을 재구성하는 마이그레이션은 FK 검사를 잠시 끄고(SQLite 표준 절차) 커밋 전에 재검증합니다.
_REBUILD_MIGRATIONS = {3, 4}
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
    "safety_flags",
    "backtest_approvals",
    "operator_approvals",
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


_WRITE_LOCK = threading.RLock()  # 프로세스 안의 모든 쓰기 트랜잭션을 한 번에 하나씩 통과시킨다(단일 writer)
_MAX_WRITE_TX_SECONDS = 1.0  # db.max_write_tx_ms(1000)
_LOG = logging.getLogger("mosasaurus.db")
SLOW_WRITE_TRANSACTIONS = [0]  # 1초를 넘긴 쓰기 트랜잭션 수(점검용 카운터)


class _SerializedConnection(sqlite3.Connection):
    """`with connection:` 블록을 단일 writer 쓰기 트랜잭션으로 만든다(명세 9.2절).

    블록에 들어갈 때 프로세스 전역 락을 잡고 `BEGIN IMMEDIATE` 로 쓰기 락을 먼저 확보하며, 나갈 때 커밋·롤백 후 락을 푼다.
    KIS 네트워크 호출은 이 블록 밖에서 하므로(write-ahead 규칙) 락 점유 시간은 짧다. 중첩 블록은 같은 스레드가 재진입한다.
    """

    _tx_started = 0.0

    def __enter__(self):
        """쓰기 락을 잡고 즉시 쓰기 트랜잭션을 시작한다."""
        _WRITE_LOCK.acquire()
        try:
            result = super().__enter__()
            if not self.in_transaction:
                self.execute("BEGIN IMMEDIATE")
                self._tx_started = time.monotonic()
            return result
        except BaseException:
            _WRITE_LOCK.release()
            raise

    def __exit__(self, *exc_info):
        """커밋·롤백 후 락을 풀고, 1초를 넘긴 쓰기 트랜잭션을 기록한다."""
        started = self._tx_started
        try:
            return super().__exit__(*exc_info)
        finally:
            if started and time.monotonic() - started > _MAX_WRITE_TX_SECONDS:
                SLOW_WRITE_TRANSACTIONS[0] += 1
                _LOG.warning("write transaction exceeded %.0f ms", _MAX_WRITE_TX_SECONDS * 1000)
            self._tx_started = 0.0
            _WRITE_LOCK.release()


def _connect(db_path: Path) -> sqlite3.Connection:
    """단일 writer 연결(`_SerializedConnection`)을 열고 외래키·내구성·대기 시간 PRAGMA 를 적용한다."""
    connection = sqlite3.connect(db_path, factory=_SerializedConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # 명세 9.2절(v3.7.5): WAL + NORMAL 은 정전 시 마지막 커밋(write-ahead 주문 기록)이 사라질 수 있어 FULL 을 강제한다.
    connection.execute("PRAGMA synchronous = FULL")
    # 명세 9.2절: 여러 스레드(스케줄러·Telegram·감시)가 쓰기 경합할 때 즉시 실패하지 않고 5초까지 기다린다.
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return connection


def _validate_durability(connection: sqlite3.Connection) -> None:
    """연결이 기대한 내구성 설정(WAL·FULL)인지 확인하고 아니면 DB_INTEGRITY 원인으로 처리하게 합니다."""
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
    if journal_mode.lower() != "wal":
        raise RuntimeError(f"SQLite WAL mode required, found: {journal_mode}.")
    if synchronous != 2:  # 2 = FULL
        raise RuntimeError(f"SQLite synchronous=FULL required, found: {synchronous}.")
    busy = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    if busy != _BUSY_TIMEOUT_MS:
        raise RuntimeError(f"SQLite busy_timeout={_BUSY_TIMEOUT_MS} required, found: {busy}.")


def _validate_schema(connection: sqlite3.Connection) -> None:
    """스키마 버전과 필수 테이블·분류 컬럼이 기대와 같은지 확인한다(다르면 RuntimeError)."""
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


def _migration_backward_compatible(path: Path) -> int:
    """파일 첫 줄의 `-- backward_compatible: true|false` 헤더를 읽습니다(없으면 0=false)."""
    first_line = path.read_text(encoding="utf-8").splitlines()[0].strip().lower()
    return 1 if first_line == "-- backward_compatible: true" else 0


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
    rebuild = version in _REBUILD_MIGRATIONS
    if rebuild:
        # 트랜잭션 밖에서만 바꿀 수 있는 PRAGMA 입니다.
        connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
            if rebuild:
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise sqlite3.IntegrityError(
                        "Foreign key violations after table rebuild."
                    )
            if version >= 3:
                connection.execute(
                    "INSERT INTO schema_migrations("
                    "version, applied_at, checksum, backward_compatible) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        name,
                        datetime.now(timezone.utc).isoformat(),
                        checksum,
                        _migration_backward_compatible(path),
                    ),
                )
            else:
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
    finally:
        if rebuild:
            connection.execute("PRAGMA foreign_keys = ON")


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
        # 현재 버전보다 높은 마이그레이션을 순서대로 적용합니다(1→2→3→4).
        for next_version in range(version + 1, _SCHEMA_VERSION + 1):
            _apply_migration(connection, version=next_version)

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
        _validate_durability(connection)
        _validate_schema(connection)
        return connection
    except (sqlite3.Error, RuntimeError):
        connection.close()
        raise
