"""명세서의 KST 작업을 등록하고 지연 실행을 안전하게 차단합니다."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Callable, Mapping

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger


KST = timezone(timedelta(hours=9), name="KST")


class ScheduleJob(str, Enum):
    """명세서에 등록된 예약 작업의 식별자입니다."""

    BOOT_RECOVERY = "boot_recovery"
    WEEKLY_RANKING_PREVIEW = "weekly_ranking_preview"
    MORNING_PREP = "morning_prep"
    INTRADAY_GUARD = "intraday_guard"
    OPEN_CHECKPOINT = "open_checkpoint"
    DAILY_PIPELINE = "daily_pipeline"
    ORDER_CANCEL_SWEEP = "order_cancel_sweep"
    POST_CLOSE_RECONCILE = "post_close_reconcile"
    DAILY_BRIEFING = "daily_briefing"
    WEEKLY_MAINTENANCE = "weekly_maintenance"


@dataclass(frozen=True)
class ScheduledTrigger:
    """시간과 거래일 조건을 포함하는 예약 정의입니다."""

    job: ScheduleJob
    weekday: int | None = None
    run_time: time | None = None
    grace_seconds: int = 0


SCHEDULE_TRIGGERS = (
    ScheduledTrigger(
        ScheduleJob.WEEKLY_RANKING_PREVIEW,
        weekday=5,
        run_time=time(10, 0),
        grace_seconds=3600,
    ),
    ScheduledTrigger(
        ScheduleJob.MORNING_PREP,
        run_time=time(8, 30),
        grace_seconds=600,
    ),
    ScheduledTrigger(
        ScheduleJob.OPEN_CHECKPOINT,
        run_time=time(9, 10),
        grace_seconds=120,
    ),
    ScheduledTrigger(
        ScheduleJob.DAILY_PIPELINE,
        run_time=time(15, 0),
        grace_seconds=60,
    ),
    ScheduledTrigger(
        ScheduleJob.ORDER_CANCEL_SWEEP,
        run_time=time(15, 14, 30),
        grace_seconds=15,
    ),
    ScheduledTrigger(
        ScheduleJob.POST_CLOSE_RECONCILE,
        run_time=time(15, 15, 10),
        grace_seconds=120,
    ),
    ScheduledTrigger(
        ScheduleJob.DAILY_BRIEFING,
        run_time=time(18, 5),
        grace_seconds=600,
    ),
    ScheduledTrigger(
        ScheduleJob.WEEKLY_MAINTENANCE,
        weekday=6,
        run_time=time(3, 0),
        grace_seconds=3600,
    ),
)

ORDER_RELATED_JOBS = frozenset(
    {
        ScheduleJob.INTRADAY_GUARD,
        ScheduleJob.OPEN_CHECKPOINT,
        ScheduleJob.DAILY_PIPELINE,
        ScheduleJob.ORDER_CANCEL_SWEEP,
        ScheduleJob.POST_CLOSE_RECONCILE,
    }
)

_REQUIRED_HANDLERS = frozenset(
    {
        ScheduleJob.WEEKLY_RANKING_PREVIEW,
        ScheduleJob.MORNING_PREP,
        ScheduleJob.INTRADAY_GUARD,
        ScheduleJob.OPEN_CHECKPOINT,
        ScheduleJob.DAILY_PIPELINE,
        ScheduleJob.ORDER_CANCEL_SWEEP,
        ScheduleJob.POST_CLOSE_RECONCILE,
        ScheduleJob.DAILY_BRIEFING,
        ScheduleJob.WEEKLY_MAINTENANCE,
    }
)


def is_order_window(now: datetime) -> bool:
    """KST 배포 차단 시간대인지 판정합니다."""
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime.")
    kst_now = now.astimezone(KST) if now.tzinfo is not None else now.replace(tzinfo=KST)
    return time(8, 50) <= kst_now.time() <= time(15, 25)


def jobs_due_now(
    now: datetime,
    *,
    weekday_is_open: bool,
) -> tuple[ScheduleJob, ...]:
    """명세서 기준 시각에 실행 가능한 작업만 결정론적으로 반환합니다."""
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime.")
    if not isinstance(weekday_is_open, bool):
        raise ValueError("weekday_is_open must be a boolean.")
    kst_now = now.astimezone(KST)
    due = []
    for trigger in SCHEDULE_TRIGGERS:
        if trigger.run_time is None:
            continue
        if trigger.weekday is not None and kst_now.weekday() != trigger.weekday:
            continue
        if trigger.job is ScheduleJob.MORNING_PREP and kst_now.weekday() > 4:
            continue
        if trigger.job in ORDER_RELATED_JOBS and not weekday_is_open:
            continue
        if kst_now.time().replace(microsecond=0) == trigger.run_time:
            due.append(trigger.job)
    return tuple(due)


def is_order_scheduler_locked(connection: sqlite3.Connection) -> bool:
    """대조 복구가 끝나지 않아 주문 관련 작업이 잠겼는지 확인합니다."""
    row = connection.execute(
        'SELECT value FROM system_state WHERE "key" = ?',
        ("scheduler_order_lock",),
    ).fetchone()
    return row is None or row[0] != "false"


def set_order_scheduler_lock(
    connection: sqlite3.Connection,
    *,
    locked: bool,
    reason: str,
    occurred_at: datetime,
) -> None:
    """스케줄러 잠금을 저장하고 민감정보 없는 감사 이벤트를 기록합니다."""
    if not isinstance(locked, bool):
        raise ValueError("locked must be a boolean.")
    if not isinstance(reason, str) or not reason:
        raise ValueError("reason must be a non-empty string.")
    if reason not in {
        "MISFIRE_SKIPPED",
        "JOB_FAILED",
        "BOOT_RECOVERY_REQUIRED",
        "RECONCILIATION_REQUIRED",
        "HEALTH_CHECK_FAILED",
    }:
        raise ValueError("reason must be a known scheduler event code.")
    if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware.")
    timestamp = occurred_at.astimezone(KST).isoformat()
    if not locked:
        reconciliation = connection.execute(
            'SELECT value FROM system_state WHERE "key" = ?',
            ("reconciliation_ok",),
        ).fetchone()
        safe_halt = connection.execute(
            'SELECT value FROM system_state WHERE "key" = ?',
            ("is_safe_halt",),
        ).fetchone()
        if (
            reconciliation is None
            or reconciliation[0] != "true"
            or (safe_halt is not None and safe_halt[0] != "false")
        ):
            raise PermissionError(
                "Order scheduler lock requires successful reconciliation and no SAFE_HALT."
            )
    previous_lock = connection.execute(
        'SELECT value FROM system_state WHERE "key" = ?',
        ("scheduler_order_lock",),
    ).fetchone()
    was_known_locked = previous_lock is not None and previous_lock[0] == "true"
    with connection:
        connection.execute(
            """
            INSERT INTO system_state ("key", value, updated_at)
            VALUES ('scheduler_order_lock', ?, ?)
            ON CONFLICT("key") DO UPDATE SET
                value = excluded.value, updated_at = excluded.updated_at
            """,
            ("true" if locked else "false", timestamp),
        )
        if locked and (
            not was_known_locked
            or reason in {"MISFIRE_SKIPPED", "JOB_FAILED", "BOOT_RECOVERY_REQUIRED"}
        ):
            connection.execute(
                """
                INSERT INTO system_events (
                    component, event_type, severity, message, created_at
                ) VALUES ('scheduler', ?, 'CRITICAL', ?, ?)
                """,
                (reason, "주문 관련 예약 작업이 안전 잠금 상태입니다.", timestamp),
            )
        else:
            connection.execute(
                """
                INSERT INTO system_events (
                    component, event_type, severity, message, created_at
                ) VALUES (
                    'scheduler', 'ORDER_JOBS_REENABLED', 'INFO', ?, ?
                )
                """,
                ("대조 성공 후 주문 관련 예약 작업이 다시 활성화되었습니다.", timestamp),
            )


def _record_scheduler_event(
    connection_factory: Callable[[], sqlite3.Connection],
    *,
    event_type: str,
    is_order_job: bool,
) -> None:
    """미스파이어 또는 작업 오류를 기록하고 주문 작업을 잠급니다."""
    connection = connection_factory()
    try:
        timestamp = datetime.now(KST)
        if is_order_job:
            set_order_scheduler_lock(
                connection,
                locked=True,
                reason=event_type,
                occurred_at=timestamp,
            )
        else:
            with connection:
                connection.execute(
                    """
                    INSERT INTO system_events (
                        component, event_type, severity, message, created_at
                    ) VALUES ('scheduler', ?, 'WARN', ?, ?)
                    """,
                    (
                        event_type,
                        "예약 작업의 지연 또는 오류가 감지되었습니다.",
                        timestamp.isoformat(),
                    ),
                )
    finally:
        connection.close()


def _make_event_listener(
    connection_factory: Callable[[], sqlite3.Connection],
) -> Callable[[object], None]:
    """APScheduler 이벤트를 최소 정보만 DB에 기록하는 리스너로 변환합니다."""

    def listener(event: object) -> None:
        """작업 누락·오류를 기록하고 주문 관련 작업을 잠급니다."""
        job_id = getattr(event, "job_id", None)
        if not isinstance(job_id, str):
            return
        job = next(
            (candidate for candidate in ScheduleJob if candidate.value == job_id),
            None,
        )
        if job is None:
            return
        code = getattr(event, "code", None)
        if code == EVENT_JOB_MISSED:
            event_type = "MISFIRE_SKIPPED"
        elif code == EVENT_JOB_ERROR:
            event_type = "JOB_FAILED"
        else:
            return
        _record_scheduler_event(
            connection_factory,
            event_type=event_type,
            is_order_job=job in ORDER_RELATED_JOBS,
        )

    return listener


def _guarded_job(
    job: ScheduleJob,
    handler: Callable[[], None],
    connection_factory: Callable[[], sqlite3.Connection],
    market_is_open: Callable[[], bool],
) -> None:
    """잠금·거래일 조건을 통과한 경우에만 실제 작업 콜백을 실행합니다."""
    connection = connection_factory()
    try:
        if job in ORDER_RELATED_JOBS and is_order_scheduler_locked(connection):
            return
    finally:
        connection.close()
    if job in ORDER_RELATED_JOBS:
        if market_is_open() is not True:
            return
        if job is ScheduleJob.INTRADAY_GUARD:
            now = datetime.now(KST).time()
            if not time(9, 0) <= now <= time(15, 25):
                return
    handler()


def build_scheduler(
    handlers: Mapping[ScheduleJob, Callable[[], None]],
    *,
    connection_factory: Callable[[], sqlite3.Connection],
    market_is_open: Callable[[], bool],
) -> BackgroundScheduler:
    """명세서의 예약 작업을 APScheduler에 등록하고 미스파이어 리스너를 연결합니다."""
    if not isinstance(handlers, Mapping):
        raise TypeError("handlers must be a mapping.")
    if not callable(connection_factory) or not callable(market_is_open):
        raise TypeError("connection_factory and market_is_open must be callable.")
    missing = _REQUIRED_HANDLERS - set(handlers)
    if missing:
        names = ", ".join(sorted(job.value for job in missing))
        raise ValueError(f"Missing scheduler handlers: {names}.")
    if any(not callable(handlers[job]) for job in _REQUIRED_HANDLERS):
        raise TypeError("Every scheduler handler must be callable.")

    scheduler = BackgroundScheduler(timezone=KST)
    scheduler.add_listener(
        _make_event_listener(connection_factory),
        EVENT_JOB_MISSED | EVENT_JOB_ERROR,
    )
    schedules = (
        (
            ScheduleJob.WEEKLY_RANKING_PREVIEW,
            CronTrigger(day_of_week="sat", hour=10, minute=0, timezone=KST),
            3600,
        ),
        (
            ScheduleJob.MORNING_PREP,
            CronTrigger(day_of_week="mon-fri", hour=8, minute=30, timezone=KST),
            600,
        ),
        (
            ScheduleJob.INTRADAY_GUARD,
            IntervalTrigger(seconds=10, timezone=KST),
            10,
        ),
        (
            ScheduleJob.OPEN_CHECKPOINT,
            CronTrigger(day_of_week="mon-fri", hour=9, minute=10, timezone=KST),
            120,
        ),
        (
            ScheduleJob.DAILY_PIPELINE,
            CronTrigger(day_of_week="mon-fri", hour=15, minute=0, timezone=KST),
            60,
        ),
        (
            ScheduleJob.ORDER_CANCEL_SWEEP,
            CronTrigger(
                day_of_week="mon-fri",
                hour=15,
                minute=14,
                second=30,
                timezone=KST,
            ),
            15,
        ),
        (
            ScheduleJob.POST_CLOSE_RECONCILE,
            CronTrigger(
                day_of_week="mon-fri",
                hour=15,
                minute=15,
                second=10,
                timezone=KST,
            ),
            120,
        ),
        (
            ScheduleJob.DAILY_BRIEFING,
            CronTrigger(hour=18, minute=5, timezone=KST),
            600,
        ),
        (
            ScheduleJob.WEEKLY_MAINTENANCE,
            CronTrigger(day_of_week="sun", hour=3, minute=0, timezone=KST),
            3600,
        ),
    )
    for job, trigger, grace_seconds in schedules:
        scheduler.add_job(
            _guarded_job,
            trigger=trigger,
            args=(job, handlers[job], connection_factory, market_is_open),
            id=job.value,
            name=job.value,
            misfire_grace_time=grace_seconds,
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
    return scheduler
