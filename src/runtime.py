"""운영 조립부(composition root): 설정·비밀·증권사 클라이언트로 `RuntimeDependencies` 를 만듭니다.

- 비밀(앱키·계좌번호)은 환경변수(컨테이너 주입)에서만 읽고 로그·예외 메시지에 남기지 않습니다.
- 현재는 **PAPER 전용**입니다. `deployment.environment` 가 PAPER 가 아니면 만들지 않습니다
  (REAL 은 3중 승인 연결 전까지 금지).
- 아직 구현되지 않은 잡(`morning_prep`·`daily_pipeline`·`daily_briefing`·`weekly_maintenance`)은
  주문을 내지 않는 "미구현 기록" 핸들러로 채웁니다. 일부 기능만 켠 채 매매하는 일이 없도록,
  신규 매수는 `daily_pipeline` 이 구현되기 전까지 발생하지 않습니다.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from src.config import load_config
from src.db import connect_database
from src.health_monitor import HealthSnapshot
from src.job_handlers import (
    create_intraday_guard_handler, create_open_checkpoint_handler, create_order_cancel_sweep_handler,
    create_pending_confirmation_poller, create_post_close_reconcile_handler, create_safety_maintenance_handler,
)
from src.daily_pipeline import PipelineConfig, create_daily_pipeline_handler
from src.kis_broker import KisBroker
from src.kis_client import KisEnvironment, KisReadOnlyClient, KisTokenCache
from src.main import RuntimeDependencies
from src.morning_prep import create_morning_prep_handler
from src.ops_jobs import build_notifier, create_daily_briefing_handler, create_weekly_maintenance_handler
from src.order_manager import create_paper_order_transport
from src.rate_limit import PriorityRateLimiter, RateLimitedBroker
from src.reconciliation import OrderSnapshot, Position, ReconciliationResult, reconcile_account_state
from src.scheduler import KST, ScheduleJob
from src.universe import load_universe_allowlist, scheduled_ex_dates
from src.scheduler_jobs import create_weekly_preview_handler

NOT_IMPLEMENTED_JOBS: tuple = ()  # 모든 필수 잡이 구현되어 자리표시 핸들러가 필요 없다


def _require_env(environ: Mapping[str, str], name: str) -> str:
    """필수 환경변수를 읽되 값은 오류 메시지에 쓰지 않는다."""
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Environment variable {name} is required.")
    return value


def not_implemented_handler(
    connection_factory: Callable[[], sqlite3.Connection], job: ScheduleJob
) -> Callable[[], None]:
    """주문 없이 `JOB_NOT_IMPLEMENTED` 경고만 남기는 안전한 자리표시 핸들러."""

    def handler() -> None:
        connection = connection_factory()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO system_events (component, event_type, severity, message, created_at) "
                    "VALUES ('runtime', 'JOB_NOT_IMPLEMENTED', 'WARN', ?, ?)",
                    (f"{job.value} 핸들러가 아직 구현되지 않아 실행하지 않았습니다.",
                     datetime.now(timezone.utc).isoformat()))
        finally:
            connection.close()

    return handler


def build_dependencies(
    db_path: str | Path,
    token_cache: KisTokenCache,
    *,
    environ: Mapping[str, str] | None = None,
    config: Mapping[str, object] | None = None,
) -> RuntimeDependencies:
    """PAPER 증권사 포트와 잡 핸들러를 조립합니다."""
    env = os.environ if environ is None else environ
    cfg = config if config is not None else load_config()
    if cfg["deployment"]["environment"] != "PAPER":  # type: ignore[index]
        raise ValueError("Only the PAPER environment can be wired until REAL approval is implemented.")
    app_key, app_secret = _require_env(env, "KIS_APP_KEY"), _require_env(env, "KIS_APP_SECRET")
    account = _require_env(env, "KIS_ACCOUNT_NO")
    connection_factory = lambda: connect_database(db_path)

    def token() -> str:
        cached = token_cache.load()
        if cached is None or cached.expires_at <= datetime.now(timezone.utc):
            raise RuntimeError("KIS token is missing or expired.")
        return cached.access_token

    client = KisReadOnlyClient(app_key, app_secret, token_cache, environment=KisEnvironment.PAPER)
    transport = create_paper_order_transport(
        app_key=app_key, app_secret=app_secret, account_number=account, token_provider=token)

    def order_lookup(kis_order_no: str):
        """취소 요청에 필요한 (조직번호, 잔량, 주문가격)을 DB 에서 읽는다."""
        connection = connection_factory()
        try:
            row = connection.execute(
                "SELECT kis_org_no, remaining_qty, order_price FROM orders WHERE kis_order_no = ?",
                (kis_order_no,)).fetchone()
            return None if row is None or not row[0] else (row[0], row[1], row[2])
        finally:
            connection.close()

    rate = float(cfg["kis"]["rate_limit_per_second"]["PAPER"])  # type: ignore[index]
    # 모든 KIS 호출은 프로세스 단일 우선순위 한도기를 지난다(주문·취소·대조 > 스탑 감시 > 기타).
    broker = RateLimitedBroker(
        KisBroker(client, transport, account_number=account, order_lookup=order_lookup),
        PriorityRateLimiter(rate))
    protective = bool(cfg["risk"]["safe_halt"]["protective_exit"]["enabled"])  # type: ignore[index]

    def reconcile_account() -> ReconciliationResult:
        """부팅 복구용 대조: KIS 잔고·주문과 DB 를 비교한다(조회 실패는 예외로 올려 부팅 복구가 처리)."""
        connection = connection_factory()
        try:
            sqlite_positions = [Position(r[0], r[1]) for r in connection.execute(
                "SELECT ticker, quantity FROM positions WHERE quantity > 0 AND tracking_status = 'MANAGED'")]
            sqlite_orders = [OrderSnapshot(r["kis_order_no"], r["ticker"], r["side"], r["requested_qty"],
                                           r["filled_qty"], r["remaining_qty"], r["status"])
                             for r in connection.execute("SELECT * FROM orders WHERE kis_order_no IS NOT NULL")]
            return reconcile_account_state(
                broker_positions=broker.positions(), sqlite_positions=sqlite_positions,
                broker_orders=broker.orders(datetime.now(timezone.utc).date()),
                sqlite_orders=sqlite_orders, account_data_fresh=True)
        finally:
            connection.close()

    notify = build_notifier(env)
    def scheduled_dates(ticker: str) -> set[str]:
        """운영자 승인 파일에 등록된 분배 일정(매 호출 시 다시 읽어 변경을 반영, 읽기 실패 시 빈 집합 = 자동 검증 안 함)."""
        try:
            universe = cfg["universe"]  # type: ignore[index]
            path = Path(universe["allowlist_file"]).expanduser()  # type: ignore[index]
            if not path.is_absolute():
                path = Path(__file__).resolve().parent.parent / path
            assets = load_universe_allowlist(
                path, allowed_synthetic_parking_tickers=universe["allowlisted_synthetic_parking_tickers"])  # type: ignore[index]
            return scheduled_ex_dates(assets).get(ticker, set())
        except (ValueError, KeyError, OSError):
            return set()

    guard = create_intraday_guard_handler(
        connection_factory, broker, notify=notify, protective_enabled=protective, rate_per_second=rate)
    safety = create_safety_maintenance_handler(connection_factory, run_dir=env.get("HOST_RUN_DIR"), notify=notify)
    ticks = {"n": 0}

    def guard_with_safety() -> None:
        """10초 감시 60회(≈10분)마다 안전 점검(UPS·시각 플래그, 24h 재경보)을 함께 실행한다."""
        ticks["n"] += 1
        if ticks["n"] % 60 == 1:
            safety()
        guard()

    handlers: dict[ScheduleJob, Callable[[], None]] = {
        ScheduleJob.INTRADAY_GUARD: guard_with_safety,
        ScheduleJob.OPEN_CHECKPOINT: create_open_checkpoint_handler(
            connection_factory, broker, notify=notify, protective_enabled=protective),
        ScheduleJob.ORDER_CANCEL_SWEEP: create_order_cancel_sweep_handler(connection_factory, broker),
        ScheduleJob.POST_CLOSE_RECONCILE: create_post_close_reconcile_handler(connection_factory, broker, notify=notify),
        ScheduleJob.DAILY_PIPELINE: create_daily_pipeline_handler(
            connection_factory, broker, PipelineConfig.from_rules(cfg), notify=notify, protective_enabled=protective),  # type: ignore[arg-type]
        ScheduleJob.DAILY_BRIEFING: create_daily_briefing_handler(connection_factory, notify=notify),
        ScheduleJob.WEEKLY_MAINTENANCE: create_weekly_maintenance_handler(
            connection_factory, env.get("BACKUP_DIR", "/app/data/backups"), notify=notify),
        ScheduleJob.MORNING_PREP: create_morning_prep_handler(
            connection_factory, lambda ticker: broker.candles(ticker, datetime.now(KST).date()),
            ratio_change_tolerance=float(cfg["corporate_action"]["ratio_change_tolerance"]),  # type: ignore[index]
            min_cash_factor=float(cfg["corporate_action"]["min_cash_factor"]),
            scheduled_ex_dates=scheduled_dates),  # type: ignore[index]
        ScheduleJob.PENDING_CONFIRMATION_POLLER: create_pending_confirmation_poller(connection_factory, broker),
        ScheduleJob.WEEKLY_RANKING_PREVIEW: create_weekly_preview_handler(
            connection_factory, cfg),  # type: ignore[arg-type]
    }
    for job in NOT_IMPLEMENTED_JOBS:
        handlers[job] = not_implemented_handler(connection_factory, job)

    def health_snapshot() -> HealthSnapshot:
        """DB·토큰·대조 상태만 실측한다(CPU·디스크·온도는 라즈베리파이에서 연결, 지금은 정상 기본값)."""
        now = datetime.now(timezone.utc)
        cached = token_cache.load()
        connection = connection_factory()
        try:
            ok = connection.execute(
                "SELECT value FROM system_state WHERE \"key\" = 'reconciliation_ok'").fetchone()
        finally:
            connection.close()
        return HealthSnapshot(True, True, cached is not None and cached.expires_at > now,
                              ok is not None and ok[0] == "true", 0.0, 0.0, 0.0, now)

    return RuntimeDependencies(
        reconcile_account=reconcile_account,
        job_handlers=handlers,
        market_is_open=lambda: client.is_open_day(datetime.now(KST).date()),
        health_snapshot=health_snapshot,
        refresh_watchdog=lambda: None,  # 하드웨어 워치독은 라즈베리파이에서 연결
    )
