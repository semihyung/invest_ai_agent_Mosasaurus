"""복구 게이트를 먼저 통과한 런타임만 스케줄러와 워치독을 시작합니다."""

import os
import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from src.boot_recovery import BootRecoveryResult, boot_recovery
from src.db import connect_database, initialize_database
from src.health_monitor import (
    HealthSnapshot,
    is_healthy,
    probe_database,
    record_heartbeat,
    refresh_watchdog_if_healthy,
)
from src.kis_client import KisTokenCache
from src.reconciliation import ReconciliationResult
from src.scheduler import (
    ScheduleJob,
    build_scheduler,
    set_order_scheduler_lock,
)


@dataclass(frozen=True)
class RuntimeDependencies:
    """운영 계층에서 안전하게 구현된 작업 콜백을 전달합니다."""

    reconcile_account: Callable[[], ReconciliationResult]
    job_handlers: Mapping[ScheduleJob, Callable[[], None]]
    market_is_open: Callable[[], bool]
    health_snapshot: Callable[[], HealthSnapshot]
    refresh_watchdog: Callable[[], None]


def run_agent(
    db_path: str | Path,
    token_cache: KisTokenCache,
    dependencies: RuntimeDependencies,
    *,
    stop_event: threading.Event,
    heartbeat_interval_seconds: float = 10.0,
) -> BootRecoveryResult:
    """DB·토큰·계좌 복구와 작업 콜백 검증 후에만 스케줄러를 실행합니다."""
    if not isinstance(dependencies, RuntimeDependencies):
        raise TypeError("dependencies must be a RuntimeDependencies value.")
    if not isinstance(stop_event, threading.Event):
        raise TypeError("stop_event must be a threading.Event.")
    if (
        isinstance(heartbeat_interval_seconds, bool)
        or not isinstance(heartbeat_interval_seconds, (int, float))
        or heartbeat_interval_seconds <= 0
    ):
        raise ValueError("heartbeat_interval_seconds must be positive.")
    path = Path(db_path).expanduser()
    connection = initialize_database(path)
    connection_factory = lambda: connect_database(path)
    scheduler = build_scheduler(
        dependencies.job_handlers,
        connection_factory=connection_factory,
        market_is_open=dependencies.market_is_open,
    )
    try:
        recovery = boot_recovery(
            connection,
            token_cache,
            reconcile_account=dependencies.reconcile_account,
        )
        if not recovery.recovery_ok:
            return recovery
        scheduler.start()
        health_failure_seen = False
        try:
            while not stop_event.is_set():
                snapshot = dependencies.health_snapshot()
                healthy = probe_database(connection_factory) and is_healthy(snapshot)
                if healthy:
                    heartbeat_connection = connection_factory()
                    try:
                        record_heartbeat(heartbeat_connection, now=datetime.now(timezone.utc))
                    finally:
                        heartbeat_connection.close()
                    refresh_watchdog_if_healthy(
                        snapshot,
                        refresh_watchdog=dependencies.refresh_watchdog,
                    )
                else:
                    health_failure_seen = True
                    lock_connection = connection_factory()
                    try:
                        set_order_scheduler_lock(
                            lock_connection,
                            locked=True,
                            reason="HEALTH_CHECK_FAILED",
                            occurred_at=datetime.now(timezone.utc),
                        )
                    finally:
                        lock_connection.close()
                stop_event.wait(heartbeat_interval_seconds)
        finally:
            scheduler.shutdown(wait=True)
        if health_failure_seen:
            return BootRecoveryResult(False, False, ("HEALTH_CHECK_FAILED",))
        return recovery
    finally:
        connection.close()


def main(
    dependencies: RuntimeDependencies | None = None,
    *,
    db_path: str | Path | None = None,
    token_cache: KisTokenCache | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """필수 콜백이 없으면 SAFE_HALT를 기록하고 실수로 운용을 시작하지 않습니다."""
    database_path = db_path or os.environ.get("DB_PATH", "/app/data/mosasaurus.db")
    cache = token_cache or KisTokenCache()
    if dependencies is None:
        connection = initialize_database(database_path)
        try:
            boot_recovery(
                connection,
                cache,
                reconcile_account=None,
            )
        finally:
            connection.close()
        print(
            "SAFE_HALT: operational callbacks are not configured; scheduler was not started.",
            file=sys.stderr,
        )
        return 2
    result = run_agent(
        database_path,
        cache,
        dependencies,
        stop_event=stop_event or threading.Event(),
    )
    return 0 if result.recovery_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
