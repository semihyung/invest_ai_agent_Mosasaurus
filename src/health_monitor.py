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
) -> bool:
    """SQLite 무결성·FK·임시 쓰기/읽기를 검사한 뒤 테스트 흔적을 롤백합니다."""
    if not callable(connection_factory):
        raise TypeError("connection_factory must be callable.")
    connection = None
    try:
        connection = connection_factory()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
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
