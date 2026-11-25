"""헬스 상태를 검증하고 정상일 때만 heartbeat·워치독을 갱신합니다."""

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


@dataclass(frozen=True)
class HealthSnapshot:
    """DB·토큰·계좌·하드웨어 상태의 단일 검사 결과입니다."""

    database_ok: bool
    heartbeat_ok: bool
    token_valid: bool
    reconciliation_ok: bool
    cpu_percent: float
    disk_percent: float
    temperature_c: float
    checked_at: datetime


def is_healthy(snapshot: HealthSnapshot) -> bool:
    """모든 필수 의존성이 정상일 때만 건강 상태로 판정합니다."""
    if not isinstance(snapshot, HealthSnapshot):
        raise TypeError("snapshot must be a HealthSnapshot.")
    if any(
        not isinstance(value, bool)
        for value in (
            snapshot.database_ok,
            snapshot.heartbeat_ok,
            snapshot.token_valid,
            snapshot.reconciliation_ok,
        )
    ):
        raise ValueError("Health status flags must be booleans.")
    if (
        any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in (
                snapshot.cpu_percent,
                snapshot.disk_percent,
                snapshot.temperature_c,
            )
        )
        or snapshot.cpu_percent < 0
        or snapshot.disk_percent < 0
        or snapshot.disk_percent > 100
        or snapshot.temperature_c < -273
        or not isinstance(snapshot.checked_at, datetime)
        or snapshot.checked_at.tzinfo is None
    ):
        raise ValueError("Health values are outside valid ranges.")
    return (
        snapshot.database_ok
        and snapshot.heartbeat_ok
        and snapshot.token_valid
        and snapshot.reconciliation_ok
        and snapshot.cpu_percent < 95
        and snapshot.disk_percent < 90
        and snapshot.temperature_c < 85
    )


def probe_database(
    connection_factory: Callable[[], sqlite3.Connection],
    *,
    full_check: bool = True,
) -> bool:
    """SQLite 무결성·FK·임시 쓰기/읽기를 검사한 뒤 테스트 흔적을 롤백합니다.

    `integrity_check` 는 DB 전체를 읽으므로 DB 가 커지면 SD카드에서 몇 초씩 걸린다. 10초 주기 검사는
    `full_check=False`(FK·임시 쓰기/읽기만)로 하고, 전체 검사는 호출자가 드문 주기로만 요청한다.
    """
    if not callable(connection_factory):
        raise TypeError("connection_factory must be callable.")
    connection = None
    try:
        connection = connection_factory()
        if full_check and connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return False
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            return False
        connection.execute("SAVEPOINT health_probe")
        connection.execute("CREATE TEMP TABLE IF NOT EXISTS health_probe (value INTEGER)")
        connection.execute("DELETE FROM health_probe")
        connection.execute("INSERT INTO health_probe(value) VALUES (1)")
        healthy = connection.execute(
            "SELECT value FROM health_probe"
        ).fetchone()[0] == 1
        connection.execute("ROLLBACK TO health_probe")
        connection.execute("RELEASE health_probe")
        return healthy
    except sqlite3.Error:
        return False
    finally:
        if connection is not None:
            connection.close()


def record_heartbeat(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> None:
    """현재 heartbeat 시각을 기록해 감시 프로세스가 정지 여부를 판단하게 합니다."""
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection.")
    timestamp = now or datetime.now(timezone.utc)
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    with connection:
        connection.execute(
            """
            INSERT INTO system_state ("key", value, updated_at)
            VALUES ('scheduler_heartbeat', ?, ?)
            ON CONFLICT("key") DO UPDATE SET
                value = excluded.value, updated_at = excluded.updated_at
            """,
            (
                timestamp.astimezone(timezone.utc).isoformat(),
                timestamp.astimezone(timezone.utc).isoformat(),
            ),
        )


def heartbeat_is_fresh(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    max_age_seconds: float = 60.0,
) -> bool:
    """마지막 heartbeat 가 `max_age_seconds` 이내인지 반환합니다(없거나 형식이 틀리면 False)."""
    row = connection.execute(
        'SELECT value FROM system_state WHERE "key" = ?', ("scheduler_heartbeat",)
    ).fetchone()
    if row is None:
        return False
    try:
        beat = datetime.fromisoformat(row[0])
    except (TypeError, ValueError):
        return False
    if beat.tzinfo is None:
        return False
    return 0 <= ((now or datetime.now(timezone.utc)) - beat).total_seconds() <= max_age_seconds


def _container_healthcheck(db_path: str) -> int:
    """도커 헬스체크용: DB 를 열 수 있고 heartbeat 가 신선하면 0, 아니면 1 을 반환합니다."""
    from src.db import connect_database  # 지연 import: 이 모듈의 다른 함수는 DB 모듈이 필요 없다

    try:
        connection = connect_database(db_path)
    except Exception:  # noqa: BLE001 - 어떤 실패든 비정상으로 본다
        return 1
    try:
        return 0 if heartbeat_is_fresh(connection) else 1
    finally:
        connection.close()


def refresh_watchdog_if_healthy(
    snapshot: HealthSnapshot,
    *,
    refresh_watchdog: Callable[[], None],
) -> bool:
    """정상 검사 결과에 한해서만 운영체제 워치독을 갱신합니다."""
    if not callable(refresh_watchdog):
        raise TypeError("refresh_watchdog must be callable.")
    if not is_healthy(snapshot):
        return False
    refresh_watchdog()
    return True


if __name__ == "__main__":
    import sys

    sys.exit(_container_healthcheck(sys.argv[1] if len(sys.argv) > 1 else "/app/data/mosasaurus.db"))
