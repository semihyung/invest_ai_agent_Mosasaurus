from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum


KST = timezone(timedelta(hours=9), name="KST")


class ScheduleJob(str, Enum):
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
    job: ScheduleJob
    weekday: int | None = None
    run_time: time | None = None
    grace_seconds: int = 0


SCHEDULE_TRIGGERS = (
    ScheduledTrigger(ScheduleJob.WEEKLY_RANKING_PREVIEW, weekday=5, run_time=time(10, 0), grace_seconds=3600),
    ScheduledTrigger(ScheduleJob.MORNING_PREP, run_time=time(8, 30), grace_seconds=600),
    ScheduledTrigger(ScheduleJob.OPEN_CHECKPOINT, run_time=time(9, 10), grace_seconds=120),
    ScheduledTrigger(ScheduleJob.DAILY_PIPELINE, run_time=time(15, 0), grace_seconds=60),
    ScheduledTrigger(ScheduleJob.ORDER_CANCEL_SWEEP, run_time=time(15, 14, 30), grace_seconds=15),
    ScheduledTrigger(ScheduleJob.POST_CLOSE_RECONCILE, run_time=time(15, 15, 10), grace_seconds=120),
    ScheduledTrigger(ScheduleJob.DAILY_BRIEFING, run_time=time(18, 5), grace_seconds=600),
    ScheduledTrigger(ScheduleJob.WEEKLY_MAINTENANCE, weekday=6, run_time=time(3, 0), grace_seconds=3600),
)


def is_order_window(now: datetime) -> bool:
    kst_now = now.astimezone(KST) if now.tzinfo is not None else now.replace(tzinfo=KST)
    return time(8, 50) <= kst_now.time() <= time(15, 25)


def jobs_due_now(
    now: datetime,
    *,
    weekday_is_open: bool,
) -> tuple[ScheduleJob, ...]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    kst_now = now.astimezone(KST)
    due = []
    for trigger in SCHEDULE_TRIGGERS:
        if trigger.run_time is None:
            continue
        if trigger.weekday is not None and kst_now.weekday() != trigger.weekday:
            continue
        if trigger.job is ScheduleJob.MORNING_PREP and kst_now.weekday() > 4:
            continue
        if trigger.weekday is None and trigger.job in (
            ScheduleJob.MORNING_PREP,
            ScheduleJob.DAILY_BRIEFING,
        ):
            pass
        if trigger.job in (
            ScheduleJob.OPEN_CHECKPOINT,
            ScheduleJob.DAILY_PIPELINE,
            ScheduleJob.ORDER_CANCEL_SWEEP,
            ScheduleJob.POST_CLOSE_RECONCILE,
        ) and not weekday_is_open:
            continue
        if kst_now.time().replace(microsecond=0) == trigger.run_time:
            due.append(trigger.job)
    return tuple(due)
