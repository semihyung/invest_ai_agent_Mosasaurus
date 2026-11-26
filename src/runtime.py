"""운영 조립부(composition root): 설정·비밀·증권사 클라이언트로 `RuntimeDependencies` 를 만듭니다.

- 비밀(앱키·계좌번호)은 환경변수(컨테이너 주입)에서만 읽고 로그·예외 메시지에 남기지 않습니다.
- 현재는 **PAPER 전용**입니다. `deployment.environment` 가 PAPER 가 아니면 만들지 않습니다
  (REAL 은 3중 승인 연결 전까지 금지).
- 아직 구현되지 않은 잡(`morning_prep`·`daily_pipeline`·`daily_briefing`·`weekly_maintenance`)은
  주문을 내지 않는 "미구현 기록" 핸들러로 채웁니다. 일부 기능만 켠 채 매매하는 일이 없도록,
  신규 매수는 `daily_pipeline` 이 구현되기 전까지 발생하지 않습니다.
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from src.config import load_config
from src.db import connect_database
from src.health_monitor import HealthSnapshot
from src.job_handlers import (
    compare_account, create_intraday_guard_handler, create_open_checkpoint_handler, create_order_cancel_sweep_handler,
    create_pending_confirmation_poller, create_post_close_reconcile_handler, create_safety_maintenance_handler,
)
from src.daily_pipeline import PipelineConfig, create_daily_pipeline_handler
from src.calendar_check import verify_calendar
from src.kis_broker import KisBroker
from src.kis_client import KisEnvironment, KisReadOnlyClient, KisTokenCache
from src.main import RuntimeDependencies
from src.morning_prep import create_morning_prep_handler
from src.ops_jobs import build_notifier, create_daily_briefing_handler, create_weekly_maintenance_handler
from src.order_manager import create_paper_order_transport
from src.rate_limit import PriorityRateLimiter, RateLimitedBroker
from src.reconciliation import ReconciliationResult
from src.scheduler import KST, ScheduleJob
from src.universe import load_universe_allowlist, scheduled_ex_dates
from src.scheduler_jobs import create_weekly_preview_handler



def _require_env(environ: Mapping[str, str], name: str) -> str:
    """필수 환경변수를 읽되 값은 오류 메시지에 쓰지 않는다."""
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Environment variable {name} is required.")
    return value


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
        """유효한 KIS 접근 토큰을 돌려준다(없거나 만료면 예외)."""
        cached = token_cache.load()
        if cached is None or cached.expires_at <= datetime.now(timezone.utc):
            raise RuntimeError("KIS token is missing or expired.")
        return cached.access_token

    def token_is_valid() -> bool:
        """캐시된 KIS 토큰이 있고 만료 전이면 True."""
        cached = token_cache.load()
        return cached is not None and cached.expires_at > datetime.now(timezone.utc)

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
    limiter = PriorityRateLimiter(rate)
    # 모든 KIS 호출은 프로세스 단일 우선순위 한도기를 지난다(주문·취소·대조 > 스탑 감시 > 기타).
    broker = RateLimitedBroker(
        KisBroker(client, transport, account_number=account, order_lookup=order_lookup),
        limiter)
    protective = bool(cfg["risk"]["safe_halt"]["protective_exit"]["enabled"])  # type: ignore[index]

    def reconcile_account() -> ReconciliationResult:
        """부팅 복구용 대조: KIS 잔고·오늘(KST) 주문과 DB 의 오늘 주문을 비교한다(조회 실패는 예외로 올려 부팅 복구가 처리).

        장중 대조와 같은 `compare_account` 를 쓴다: DB 의 과거 주문 전체를 증권사의 당일 주문과 비교하면
        전날 주문이 모두 불일치가 되어 첫 거래일 이후 부팅이 항상 실패한다.
        """
        connection = connection_factory()
        try:
            trade_day = datetime.now(KST).date()
            return compare_account(connection, broker.positions(), broker.orders(trade_day), trade_day)
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

    def calendar_verify(connection, now):
        """오늘이 개장일인지 XKRX·KIS 두 달력으로 이중 검증한다(불일치면 None + CALENDAR_MISMATCH)."""
        return verify_calendar(connection, now.astimezone(KST).date(), now, kis_is_open=client.is_open_day)

    def market_is_open() -> bool:
        """주문 관련 잡의 개장일 판정: 두 달력이 모두 개장일로 일치할 때만 True(불일치·조회 불가는 fail-closed)."""
        connection = connection_factory()
        try:
            return verify_calendar(connection, datetime.now(KST).date(), datetime.now(timezone.utc),
                                   kis_is_open=client.is_open_day) is True
        finally:
            connection.close()

    guard = create_intraday_guard_handler(
        connection_factory, broker, notify=notify, protective_enabled=protective, rate_per_second=rate)
    # 안전 점검(UPS·시각 플래그, 토큰 자동 해제, 24h 재경보, 프리뷰 재발송)은 자체 10분 잡으로 돈다:
    # 스탑 감시 안에 두면 장 시간·개장일에만 돌아 주말·야간 경보가 늦는다(명세 9.1절 `safety_flag_auto_clear`).
    safety = create_safety_maintenance_handler(
        connection_factory, run_dir=env.get("HOST_RUN_DIR"), notify=notify, token_valid=token_is_valid,
        ups_enabled=cfg.get("ups", {}).get("enabled", True) is not False)

    handlers: dict[ScheduleJob, Callable[[], None]] = {
        ScheduleJob.INTRADAY_GUARD: guard,
        ScheduleJob.SAFETY_FLAG_AUTO_CLEAR: safety,
        ScheduleJob.OPEN_CHECKPOINT: create_open_checkpoint_handler(
            connection_factory, broker, notify=notify, protective_enabled=protective),
        ScheduleJob.ORDER_CANCEL_SWEEP: create_order_cancel_sweep_handler(connection_factory, broker),
        ScheduleJob.POST_CLOSE_RECONCILE: create_post_close_reconcile_handler(connection_factory, broker, notify=notify),
        ScheduleJob.DAILY_PIPELINE: create_daily_pipeline_handler(
            connection_factory, broker, PipelineConfig.from_rules(cfg), notify=notify, protective_enabled=protective),  # type: ignore[arg-type]
        ScheduleJob.DAILY_BRIEFING: create_daily_briefing_handler(
            connection_factory, notify=notify,
            token_expiry=lambda: (token_cache.load().expires_at if token_cache.load() else None),
            api_calls_today=limiter.calls_today),
        ScheduleJob.WEEKLY_MAINTENANCE: create_weekly_maintenance_handler(
            connection_factory, env.get("BACKUP_DIR", "/app/data/backups"), notify=notify,
            keep=int(cfg["backup"]["keep_weekly"]), keep_predeploy=int(cfg["backup"]["keep_predeploy"])),  # type: ignore[index]
        ScheduleJob.MORNING_PREP: create_morning_prep_handler(
            connection_factory, lambda ticker: broker.candles(ticker, datetime.now(KST).date()),
            ratio_change_tolerance=float(cfg["corporate_action"]["ratio_change_tolerance"]),  # type: ignore[index]
            min_cash_factor=float(cfg["corporate_action"]["min_cash_factor"]),
            scheduled_ex_dates=scheduled_dates, calendar_verify=calendar_verify, token_valid=token_is_valid,
            sync_broker=broker),  # type: ignore[index]
        ScheduleJob.PENDING_CONFIRMATION_POLLER: create_pending_confirmation_poller(connection_factory, broker),
        ScheduleJob.WEEKLY_RANKING_PREVIEW: create_weekly_preview_handler(
            connection_factory, cfg, notify=notify),  # type: ignore[arg-type]
    }

    def health_snapshot() -> HealthSnapshot:
        """DB·토큰·대조 상태만 실측한다(CPU·디스크·온도는 라즈베리파이에서 연결, 지금은 정상 기본값)."""
        now = datetime.now(timezone.utc)
        cached = token_cache.load()
        # 대조 상태(reconciliation_ok)는 건강 판정에 넣지 않는다: 매수 차단 수준의 사유로도 스탑 감시까지 잠기고,
        # 잠금을 푸는 대조 잡마저 잠겨 재시작 전에는 복구되지 않기 때문이다. 대조 결과는 주문 게이트가 직접 본다.
        return HealthSnapshot(True, True, cached is not None and cached.expires_at > now, True, 0.0, 0.0, 0.0, now)

    return RuntimeDependencies(
        reconcile_account=reconcile_account,
        job_handlers=handlers,
        market_is_open=market_is_open,
        health_snapshot=health_snapshot,
        refresh_watchdog=lambda: None,  # 하드웨어 워치독은 라즈베리파이에서 연결
        command_context={
            "broker": broker, "run_dir": env.get("HOST_RUN_DIR"), "ups_enabled": cfg.get("ups", {}).get("enabled", True) is not False,
            "parking_ticker": cfg["portfolio"]["primary_parking_ticker"],  # type: ignore[index]
            "token_expiry": lambda: (token_cache.load().expires_at if token_cache.load() else None),
        },
    )
