"""재부팅 직후 신규 주문 잠금을 유지하며 DB·토큰·계좌 복구를 확인합니다."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from src.kis_client import KisTokenCache
from src.reconciliation import ReconciliationResult
from src.scheduler import set_order_scheduler_lock


_UNRESOLVED_ORDER_STATUSES = (
    "CREATED",
    "SUBMITTING",
    "PENDING_CONFIRMATION",
    "ACCEPTED",
    "PARTIALLY_FILLED",
    "CANCEL_PENDING",
)


@dataclass(frozen=True)
class BootRecoveryResult:
    """부팅 검사 결과와 SAFE_HALT 유지 여부를 나타냅니다."""

    recovery_ok: bool
    order_jobs_enabled: bool
    failure_codes: tuple[str, ...]


def _record_recovery_failure(
    connection: sqlite3.Connection,
    *,
    failure_codes: tuple[str, ...],
    occurred_at: datetime,
) -> None:
    """실패 코드를 안전 상태에 저장하고 민감정보 없는 시스템 이벤트를 남깁니다."""
    timestamp = occurred_at.astimezone(timezone.utc).isoformat()
    with connection:
        connection.execute(
            """
            INSERT INTO system_state ("key", value, updated_at)
            VALUES ('is_safe_halt', 'true', ?)
            ON CONFLICT("key") DO UPDATE SET
                value = 'true', updated_at = excluded.updated_at
            """,
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO system_state ("key", value, updated_at)
            VALUES ('reconciliation_ok', 'false', ?)
            ON CONFLICT("key") DO UPDATE SET
                value = 'false', updated_at = excluded.updated_at
            """,
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO system_events (
                component, event_type, severity, message, created_at
            ) VALUES ('boot_recovery', ?, 'CRITICAL', ?, ?)
            """,
            (
                "BOOT_RECOVERY_FAILED",
                "BOOT_RECOVERY_FAILED: " + ",".join(failure_codes),
                timestamp,
            ),
        )


def boot_recovery(
    connection: sqlite3.Connection,
    token_cache: KisTokenCache,
    *,
    reconcile_account: Callable[[], ReconciliationResult] | None,
    now: datetime | None = None,
) -> BootRecoveryResult:
    """명세서 순서대로 복구 검사를 수행하며 불명확하면 SAFE_HALT를 래치합니다."""
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
    failures: list[str] = []
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        failures.append("FOREIGN_KEYS_DISABLED")
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        failures.append("DATABASE_INTEGRITY_FAILED")

    try:
        token = token_cache.load()
    except (OSError, ValueError):
        token = None
    if token is None or token.expires_at <= current_time:
        failures.append("TOKEN_MISSING_OR_EXPIRED")

    old_safe_halt = connection.execute(
        'SELECT value FROM system_state WHERE "key" = ?',
        ("is_safe_halt",),
    ).fetchone()
    if old_safe_halt is not None and old_safe_halt[0] != "false":
        failures.append("SAFE_HALT_ALREADY_LATCHED")

    if reconcile_account is None:
        failures.append("RECONCILIATION_UNAVAILABLE")
    else:
        try:
            result = reconcile_account()
        except (OSError, RuntimeError, sqlite3.Error, ValueError):
            failures.append("RECONCILIATION_FAILED")
        else:
            if not isinstance(result, ReconciliationResult):
                raise TypeError("reconcile_account must return ReconciliationResult.")
            if not result.reconciliation_ok:
                failures.append("ACCOUNT_MISMATCH")

    placeholders = ",".join("?" for _ in _UNRESOLVED_ORDER_STATUSES)
    unresolved = connection.execute(
        f"SELECT 1 FROM orders WHERE status IN ({placeholders}) LIMIT 1",
        _UNRESOLVED_ORDER_STATUSES,
    ).fetchone()
    if unresolved is not None:
        failures.append("UNRESOLVED_ORDERS")
    unclassified_flow = connection.execute(
        """
        SELECT 1 FROM cash_flows
        WHERE classification_status = 'UNCLASSIFIED_CASH_FLOW'
        LIMIT 1
        """
    ).fetchone()
    if unclassified_flow is not None:
        failures.append("UNCLASSIFIED_CASH_FLOW")

    unique_failures = tuple(dict.fromkeys(failures))
    if unique_failures:
        _record_recovery_failure(
            connection,
            failure_codes=unique_failures,
            occurred_at=current_time,
        )
        return BootRecoveryResult(False, False, unique_failures)

    timestamp = current_time.astimezone(timezone.utc).isoformat()
    with connection:
        connection.execute(
            """
            INSERT INTO system_state ("key", value, updated_at)
            VALUES ('reconciliation_ok', 'true', ?)
            ON CONFLICT("key") DO UPDATE SET
                value = 'true', updated_at = excluded.updated_at
            """,
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO system_state ("key", value, updated_at)
            VALUES ('last_boot_recovery_at', ?, ?)
            ON CONFLICT("key") DO UPDATE SET
                value = excluded.value, updated_at = excluded.updated_at
            """,
            (timestamp, timestamp),
        )
    set_order_scheduler_lock(
        connection,
        locked=False,
        reason="RECONCILIATION_REQUIRED",
        occurred_at=current_time,
    )
    return BootRecoveryResult(True, True, ())
