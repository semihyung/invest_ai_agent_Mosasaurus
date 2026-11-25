"""복구 게이트를 먼저 통과한 런타임만 스케줄러와 워치독을 시작합니다."""

import os
import signal
import sqlite3
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from src.boot_recovery import BootRecoveryResult, boot_recovery
from src.config import load_config
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
from src.universe import load_universe_allowlist, sync_universe_allowlist


def _sync_configured_allowlist(connection: sqlite3.Connection) -> None:
    """설정된 운영자 승인 종목을 부팅 시 DB와 대조합니다."""
    config = load_config()
    universe = config.get("universe")
    if not isinstance(universe, Mapping):
        raise ValueError("universe 설정이 필요합니다.")
    allowlist_value = universe.get("allowlist_file")
    synthetic_tickers = universe.get("allowlisted_synthetic_parking_tickers")
    if not isinstance(allowlist_value, str) or not allowlist_value:
        raise ValueError("universe.allowlist_file 설정이 필요합니다.")
    if not isinstance(synthetic_tickers, list):
        raise ValueError("합성복제 파킹 티커 설정은 목록이어야 합니다.")
    allowlist_path = Path(allowlist_value).expanduser()
    if not allowlist_path.is_absolute():
        allowlist_path = Path(__file__).resolve().parent.parent / allowlist_path
    assets = load_universe_allowlist(
        allowlist_path,
        allowed_synthetic_parking_tickers=synthetic_tickers,
    )
    sync_universe_allowlist(connection, assets)


@dataclass(frozen=True)
class RuntimeDependencies:
    """운영 계층에서 안전하게 구현된 작업 콜백을 전달합니다."""

    reconcile_account: Callable[[], ReconciliationResult]
    job_handlers: Mapping[ScheduleJob, Callable[[], None]]
    market_is_open: Callable[[], bool]
    health_snapshot: Callable[[], HealthSnapshot]
    refresh_watchdog: Callable[[], None]
    # Telegram 명령이 쓰는 맥락(broker·token_expiry·run_dir·parking_ticker). 없으면 해당 명령은 DB 정보만 보여 준다.
    command_context: Mapping[str, object] = field(default_factory=dict)


def run_agent(
    db_path: str | Path,
    token_cache: KisTokenCache,
    dependencies: RuntimeDependencies,
    *,
    stop_event: threading.Event,
    heartbeat_interval_seconds: float = 10.0,
    recovery_retry_seconds: float | None = 600.0,
    full_db_check_interval_seconds: float = 3600.0,
) -> BootRecoveryResult:
    """DB·토큰·계좌 복구와 작업 콜백 검증 후에만 스케줄러를 실행합니다.

    복구가 실패하면 `recovery_retry_seconds`(명세 9.1절 10분)마다 다시 시도합니다: 자동 해제 원인(토큰 갱신 전
    부팅 등)은 시간이 지나면 풀리는데, 재시도가 없으면 스케줄러가 영영 시작되지 않습니다. `None` 이면 한 번만 시도합니다.
    """
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
    try:
        _sync_configured_allowlist(connection)
        connection_factory = lambda: connect_database(path)
        scheduler = build_scheduler(
            dependencies.job_handlers,
            connection_factory=connection_factory,
            market_is_open=dependencies.market_is_open,
        )
        while True:
            recovery = boot_recovery(
                connection,
                token_cache,
                reconcile_account=dependencies.reconcile_account,
                config=load_config(),
                run_dir=os.environ.get("HOST_RUN_DIR"),
            )
            if recovery.recovery_ok:
                break
            # 중지 요청이 오거나 재시도가 꺼져 있으면 실패 결과를 그대로 돌려준다(스케줄러는 시작하지 않음).
            if recovery_retry_seconds is None or stop_event.wait(recovery_retry_seconds):
                return recovery
        scheduler.start()
        health_failure_seen = False
        last_full_check = time.monotonic()  # 부팅 복구가 방금 전체 무결성 검사를 했다
        try:
            while not stop_event.is_set():
                snapshot = dependencies.health_snapshot()
                full_check = time.monotonic() - last_full_check >= full_db_check_interval_seconds
                if full_check:
                    last_full_check = time.monotonic()
                healthy = probe_database(connection_factory, full_check=full_check) and is_healthy(snapshot)
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
        try:
            # 지연 import(순환 방지). 비밀·설정이 갖춰지지 않으면 아래 SAFE_HALT 경로로 안전하게 멈춘다.
            from src.runtime import build_dependencies

            dependencies = build_dependencies(database_path, cache)
        except (ValueError, KeyError, OSError):
            dependencies = None
    if dependencies is None:
        connection = initialize_database(database_path)
        try:
            _sync_configured_allowlist(connection)
            boot_recovery(
                connection,
                cache,
                reconcile_account=None,
                config=load_config(),
                run_dir=os.environ.get("HOST_RUN_DIR"),
            )
        finally:
            connection.close()
        print(
            "SAFE_HALT: operational callbacks are not configured; scheduler was not started.",
            file=sys.stderr,
        )
        return 2
    stop = stop_event or threading.Event()
    telegram = _telegram_settings(os.environ) if stop_event is None else None
    if telegram is None:
        result = run_agent(database_path, cache, dependencies, stop_event=stop)
        return 0 if result.recovery_ok else 2
    # Telegram 폴링은 메인 스레드(시그널 처리)에서, 에이전트(스케줄러·감시 루프)는 작업 스레드에서 실행한다.
    outcome: list[BootRecoveryResult] = []

    def run_worker() -> None:
        """에이전트를 실행한다. 예외로 죽으면 프로세스 전체를 종료시켜 컨테이너가 재시작되게 한다.

        그냥 두면 텔레그램 수신만 계속 돌아 `/status` 는 정상으로 답하지만 스케줄러·손절 감시는 멈춘 채 방치된다.
        """
        try:
            outcome.append(run_agent(database_path, cache, dependencies, stop_event=stop))
        except BaseException:  # noqa: BLE001 - 어떤 종료든 원인을 남기고 프로세스를 내린다
            traceback.print_exc()
            if not stop.is_set():
                os.kill(os.getpid(), signal.SIGTERM)  # 메인 스레드의 텔레그램 폴링이 정상 종료 경로를 탄다

    worker = threading.Thread(target=run_worker, name="mosasaurus-agent", daemon=False)
    worker.start()
    try:
        _run_telegram(database_path, telegram, dependencies.command_context)
    finally:
        stop.set()
        worker.join()
    return 0 if outcome and outcome[0].recovery_ok else 2


def _telegram_settings(environ: Mapping[str, str]) -> dict | None:
    """토큰·허용 채팅·허용 사용자가 모두 있을 때만 명령 수신을 켠다(없으면 알림·스케줄러만 동작).

    명세 6.4절: 채팅 ID **와** 사용자 ID 를 모두 검증해야 하므로 사용자 ID 가 없으면 명령을 받지 않는다(fail-closed).
    """
    token, chat = environ.get("TELEGRAM_BOT_TOKEN", "").strip(), environ.get("ALLOWED_CHAT_ID", "").strip()
    user = environ.get("ALLOWED_USER_ID", "").strip()
    if not token or not chat:
        return None
    if not user:
        print("Telegram commands disabled: ALLOWED_USER_ID is required (spec 6.4).", file=sys.stderr)
        return None
    return {"token": token, "chat_ids": {chat}, "user_ids": {user},
            "approvals_dir": environ.get("APPROVALS_DIR", "/run/approvals"),
            "backtest_dir": environ.get("BACKTEST_DIR", "/run/backtests")}


def _run_telegram(database_path: str | Path, settings: dict, context: Mapping[str, object] | None = None) -> None:
    """명령 콜백을 연결해 Telegram 폴링을 시작한다(블로킹)."""
    from src.telegram_actions import build_command_actions, build_summary_provider
    from src.telegram_bot import PendingActionStore, run_telegram_polling

    connection_factory = lambda: connect_database(database_path)
    run_telegram_polling(
        token=settings["token"], allowed_chat_ids=settings["chat_ids"],
        pending_store=PendingActionStore(connection_factory),
        command_actions=build_command_actions(
            connection_factory, approvals_dir=settings["approvals_dir"], backtest_dir=settings["backtest_dir"],
            **(context or {})),
        summary_provider=build_summary_provider(connection_factory, settings["approvals_dir"]),
        connection_factory=connection_factory, allowed_user_ids=settings["user_ids"])


if __name__ == "__main__":
    raise SystemExit(main())
