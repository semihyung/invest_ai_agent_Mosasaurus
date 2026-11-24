"""재부팅 직후 신규 주문 잠금을 유지하며 명세 3.3절 Step 0~6 복구 시퀀스를 수행합니다.

v3.7.4: 복구 실패를 단일 `is_safe_halt` 래치가 아니라 원인 코드별 `safety_flags` 로 기록합니다.
자동 해제 원인(토큰·시각·대기 주문 등)은 이번 복구에서 조건이 풀렸으면 스스로 해제되므로
군 복무 중에도 사람이 SSH 로 접속하지 않고 복구될 수 있고, 수동 해제 원인(계좌 불일치·DB 손상)만
운영자 확인을 기다립니다.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from src.config import ConfigError, validate_config
from src.kis_client import KisTokenCache
from src.order_manager import OrderStatus, persist_order_transition
from src.reconciliation import ReconciliationResult
from src.safety_flags import (
    active_halt_reasons,
    evaluate_auto_clear,
    raise_flag,
    sync_host_state_flags,
)
from src.scheduler import set_order_scheduler_lock


@dataclass(frozen=True)
class BootRecoveryResult:
    """부팅 검사 결과와 SAFE_HALT 유지 여부를 나타냅니다."""

    recovery_ok: bool
    order_jobs_enabled: bool
    failure_codes: tuple[str, ...]


def _upsert_state(connection: sqlite3.Connection, key: str, value: str, timestamp: str) -> None:
    """system_state 한 행을 UPSERT 합니다."""
    connection.execute(
        """
        INSERT INTO system_state ("key", value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT("key") DO UPDATE SET
            value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, timestamp),
    )


def _recover_in_flight_orders(
    connection: sqlite3.Connection,
    *,
    now: datetime,
) -> None:
    """크래시로 남은 주문을 명세 7.3절 규칙대로 정리합니다(Step 4).

    - `SUBMITTING`: 전송됐는지 알 수 없으므로 `PENDING_CONFIRMATION` 으로 전이합니다.
    - `CREATED`: write-ahead 규칙상 전송된 적이 없음이 보장되므로 `EXPIRED` 로 닫습니다.
    """
    for status, target, reason in (
        ("SUBMITTING", OrderStatus.PENDING_CONFIRMATION, "BOOT_CRASH_RECOVERY"),
        ("CREATED", OrderStatus.EXPIRED, "NEVER_SUBMITTED"),
    ):
        rows = connection.execute(
            "SELECT order_id FROM orders WHERE status = ?", (status,)
        ).fetchall()
        for row in rows:
            persist_order_transition(
                connection,
                order_id=row["order_id"],
                new_status=target,
                actor="boot_recovery",
                reason_code=reason,
                correlation_id=f"boot:{row['order_id']}",
                occurred_at=now,
            )


def boot_recovery(
    connection: sqlite3.Connection,
    token_cache: KisTokenCache,
    *,
    reconcile_account: Callable[[], ReconciliationResult] | None,
    now: datetime | None = None,
    config: Mapping[str, object] | None = None,
    run_dir: str | None = None,
) -> BootRecoveryResult:
    """명세서 순서대로 복구 검사를 수행하고 활성 SAFE_HALT 원인이 남으면 주문을 잠급니다."""
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection.")
    if not isinstance(token_cache, KisTokenCache):
        raise TypeError("token_cache must be a KisTokenCache.")
    if reconcile_account is not None and not callable(reconcile_account):
        raise TypeError("reconcile_account must be callable.")
    current_time = now or datetime.now(timezone.utc)
    if not isinstance(current_time, datetime) or current_time.tzinfo is None:
        raise ValueError("now must be timezone-aware.")

    set_order_scheduler_lock(
        connection,
        locked=True,
        reason="BOOT_RECOVERY_REQUIRED",
        occurred_at=current_time,
    )
    timestamp = current_time.astimezone(timezone.utc).isoformat()
    # 이번 복구에서 "문제 없음"으로 확인된 항목(자동 해제 조건)을 모읍니다.
    resolved: dict[str, bool] = {}

    # Step 0: 시각 신뢰(호스트가 clock.json 을 기록하는 환경에서만 검사).
    if run_dir is not None:
        sync_host_state_flags(connection, run_dir, now=current_time)

    # Step 1: SQLite 무결성.
    connection.execute("PRAGMA foreign_keys = ON")
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if (
        connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
        or integrity is None
        or integrity[0] != "ok"
    ):
        raise_flag(connection, "HALT", "DB_INTEGRITY", now=current_time,
                   detail="integrity_check 실패 또는 FK 비활성")

    # Step 2: 설정 정합성(10.5절).
    if config is not None:
        try:
            validate_config(config)
            resolved["CONFIG_INVALID"] = True
        except ConfigError:
            raise_flag(connection, "HALT", "CONFIG_INVALID", now=current_time,
                       detail="설정 정합성 검사 실패")
    else:
        resolved["CONFIG_INVALID"] = True

    # Step 3: 토큰.
    try:
        token = token_cache.load()
    except (OSError, ValueError):
        token = None
    if token is None or token.expires_at <= current_time:
        raise_flag(connection, "HALT", "CREDENTIALS_UNAVAILABLE", now=current_time,
                   detail="토큰 없음 또는 만료(호스트 갱신 대기)")
    else:
        resolved["CREDENTIALS_UNAVAILABLE"] = True

    # Step 4: 크래시로 남은 SUBMITTING/CREATED 주문 정리.
    _recover_in_flight_orders(connection, now=current_time)

    # Step 5: 계좌 대조.
    reconciliation_ok = False
    if reconcile_account is None:
        raise_flag(connection, "HALT", "BOOT_RECOVERY_FAILED", now=current_time,
                   detail="RECONCILIATION_UNAVAILABLE")
    else:
        try:
            result = reconcile_account()
        except (OSError, RuntimeError, sqlite3.Error, ValueError):
            raise_flag(connection, "HALT", "BOOT_RECOVERY_FAILED", now=current_time,
                       detail="RECONCILIATION_FAILED")
        else:
            if not isinstance(result, ReconciliationResult):
                raise TypeError("reconcile_account must return ReconciliationResult.")
            reconciliation_ok = result.reconciliation_ok
            if reconciliation_ok:
                resolved["BOOT_RECOVERY_FAILED"] = True
            else:
                raise_flag(connection, "HALT", "ACCOUNT_MISMATCH", now=current_time,
                           detail=",".join(result.failure_codes))

    # Step 6: 판정. 해소되지 않은 PENDING_CONFIRMATION 은 HALT, 미분류 현금흐름은 매수 차단만.
    pending = connection.execute(
        "SELECT 1 FROM orders WHERE status = 'PENDING_CONFIRMATION' LIMIT 1"
    ).fetchone()
    if pending is not None:
        raise_flag(connection, "HALT", "PENDING_CONFIRMATION", now=current_time,
                   detail="전송 여부 불명 주문이 남아 있음")
    else:
        resolved["PENDING_CONFIRMATION"] = True
    unclassified = connection.execute(
        "SELECT 1 FROM cash_flows WHERE classification_status = 'UNCLASSIFIED_CASH_FLOW' LIMIT 1"
    ).fetchone()
    if unclassified is not None:
        raise_flag(connection, "BUY_BLOCK", "UNCLASSIFIED_CASH_FLOW", now=current_time)

    evaluate_auto_clear(connection, resolved, now=current_time)
    remaining = active_halt_reasons(connection)
    with connection:
        _upsert_state(connection, "reconciliation_ok",
                      "true" if reconciliation_ok and not remaining else "false", timestamp)
        if not remaining:
            _upsert_state(connection, "last_boot_recovery_at", timestamp, timestamp)
        else:
            connection.execute(
                """
                INSERT INTO system_events (component, event_type, severity, message, created_at)
                VALUES ('boot_recovery', 'BOOT_RECOVERY_FAILED', 'CRITICAL', ?, ?)
                """,
                ("BOOT_RECOVERY_FAILED: " + ",".join(remaining), timestamp),
            )
    if remaining:
        return BootRecoveryResult(False, False, remaining)

    set_order_scheduler_lock(
        connection,
        locked=False,
        reason="RECONCILIATION_REQUIRED",
        occurred_at=current_time,
    )
    return BootRecoveryResult(True, True, ())
