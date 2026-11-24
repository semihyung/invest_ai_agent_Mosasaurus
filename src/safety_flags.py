"""SAFE_HALT 원인·매수 차단·종목 격리 플래그의 등록·자동 해제·수동 해제를 담당합니다(명세 0.5·6.3절).

`SAFE_HALT` 는 하나의 스위치가 아니라 `safety_flags` 의 활성 `HALT` 행(뷰 `halt_reasons`)이
1건 이상 존재하는지로 유도됩니다. 자동 해제 원인은 조건이 풀리면 시스템이 스스로 해제하고,
수동 해제 원인은 운영자 2단계 확인(`/clear_halt`)과 대조 성공이 있어야만 해제됩니다.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from src.risk_manager import RiskFlags

HALT_CODES = frozenset(
    {
        "ACCOUNT_MISMATCH", "PENDING_CONFIRMATION", "DB_INTEGRITY", "CALENDAR_MISMATCH",
        "CREDENTIALS_UNAVAILABLE", "UPS_LOW", "CLOCK_UNSYNCED", "BOOT_RECOVERY_FAILED",
        "DEPLOY_ROLLBACK", "CONFIG_INVALID",
    }
)
BUY_BLOCK_CODES = frozenset(
    {"UNCLASSIFIED_CASH_FLOW", "ACCOUNTING_INVALID", "ACCOUNT_DATA_STALE"}
)
TICKER_BLOCK_CODES = frozenset(
    {"CORPORATE_ACTION_UNVERIFIED", "QUOTE_INVALID", "PRICE_SERIES_INVALID", "TRADING_HALTED"}
)
# 6.3절 원인별 해제표: 수동 해제 필수 원인(나머지는 조건 충족 시 자동 해제).
MANUAL_HALT_CODES = frozenset({"ACCOUNT_MISMATCH", "DB_INTEGRITY", "DEPLOY_ROLLBACK"})
_CODES_BY_TYPE = {
    "HALT": HALT_CODES,
    "BUY_BLOCK": BUY_BLOCK_CODES,
    "TICKER_BLOCK": TICKER_BLOCK_CODES,
}


def _iso(value: datetime) -> str:
    """시간대가 있는 시각을 UTC ISO 문자열로 바꿉니다."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc).isoformat()


def _validate_flag(flag_type: str, code: str, ticker: str | None) -> None:
    """플래그 종류·코드·종목 지정이 명세 enum 과 일치하는지 확인합니다."""
    if flag_type not in _CODES_BY_TYPE:
        raise ValueError("flag_type must be HALT, BUY_BLOCK or TICKER_BLOCK.")
    if code not in _CODES_BY_TYPE[flag_type]:
        raise ValueError(f"{code} is not a valid {flag_type} code.")
    if (flag_type == "TICKER_BLOCK") != (ticker is not None):
        raise ValueError("Only TICKER_BLOCK flags carry a ticker.")


def raise_flag(
    connection: sqlite3.Connection,
    flag_type: str,
    code: str,
    *,
    now: datetime,
    ticker: str | None = None,
    detail: str | None = None,
) -> bool:
    """플래그를 등록하고 새로 생겼으면 True, 이미 활성이면 `last_seen_at` 만 갱신해 False 입니다."""
    _validate_flag(flag_type, code, ticker)
    timestamp = _iso(now)
    auto_clearable = 0 if (flag_type == "HALT" and code in MANUAL_HALT_CODES) else 1
    with connection:
        cursor = connection.execute(
            """
            UPDATE safety_flags SET last_seen_at = ?
            WHERE flag_type = ? AND code = ? AND IFNULL(ticker, '') = IFNULL(?, '')
              AND cleared_at IS NULL
            """,
            (timestamp, flag_type, code, ticker),
        )
        if cursor.rowcount:
            return False
        connection.execute(
            """
            INSERT INTO safety_flags (
                flag_type, code, ticker, auto_clearable, detail_sanitized,
                created_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (flag_type, code, ticker, auto_clearable, detail, timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO system_events (component, event_type, severity, message, created_at)
            VALUES ('safety_flags', ?, ?, ?, ?)
            """,
            (
                f"{flag_type}_RAISED",
                "CRITICAL" if flag_type == "HALT" else "WARN",
                f"{code} 활성화" + (f" ({ticker})" if ticker else ""),
                timestamp,
            ),
        )
    return True


def clear_flag(
    connection: sqlite3.Connection,
    flag_type: str,
    code: str,
    *,
    now: datetime,
    cleared_by: str,
    ticker: str | None = None,
) -> bool:
    """활성 플래그 1건을 해제하고 해제됐으면 True 를 반환합니다(수동 원인 권한 검사 없음)."""
    _validate_flag(flag_type, code, ticker)
    if not isinstance(cleared_by, str) or not cleared_by.strip():
        raise ValueError("cleared_by must be a non-empty string.")
    timestamp = _iso(now)
    with connection:
        cursor = connection.execute(
            """
            UPDATE safety_flags SET cleared_at = ?, cleared_by = ?
            WHERE flag_type = ? AND code = ? AND IFNULL(ticker, '') = IFNULL(?, '')
              AND cleared_at IS NULL
            """,
            (timestamp, cleared_by, flag_type, code, ticker),
        )
        if cursor.rowcount:
            connection.execute(
                """
                INSERT INTO system_events (component, event_type, severity, message, created_at)
                VALUES ('safety_flags', ?, 'INFO', ?, ?)
                """,
                (f"{flag_type}_CLEARED", f"{code} 해제({cleared_by})", timestamp),
            )
    return cursor.rowcount == 1


def active_codes(
    connection: sqlite3.Connection,
    flag_type: str,
    *,
    ticker: str | None = None,
) -> tuple[str, ...]:
    """활성 플래그 코드를 정렬해 반환합니다(`TICKER_BLOCK` 은 종목으로 좁힐 수 있음)."""
    if flag_type not in _CODES_BY_TYPE:
        raise ValueError("Unknown flag type.")
    query = "SELECT code FROM safety_flags WHERE flag_type = ? AND cleared_at IS NULL"
    params: list[object] = [flag_type]
    if ticker is not None:
        query += " AND ticker = ?"
        params.append(ticker)
    return tuple(sorted(row[0] for row in connection.execute(query, params)))


def active_halt_reasons(connection: sqlite3.Connection) -> tuple[str, ...]:
    """활성 SAFE_HALT 원인 목록입니다."""
    return active_codes(connection, "HALT")


def is_safe_halt(connection: sqlite3.Connection) -> bool:
    """활성 HALT 원인이 1건 이상이면 SAFE_HALT 입니다."""
    return bool(active_halt_reasons(connection))


def has_buy_block(connection: sqlite3.Connection) -> bool:
    """계좌 범위 신규 매수 차단 플래그가 있는지 반환합니다."""
    return bool(active_codes(connection, "BUY_BLOCK"))


def ticker_is_blocked(connection: sqlite3.Connection, ticker: str) -> bool:
    """해당 종목에 격리 플래그가 있는지 반환합니다."""
    return bool(active_codes(connection, "TICKER_BLOCK", ticker=ticker))


def evaluate_auto_clear(
    connection: sqlite3.Connection,
    conditions: Mapping[str, bool],
    *,
    now: datetime,
) -> tuple[str, ...]:
    """해제 조건이 충족된 자동 해제 HALT 원인을 해제하고 해제된 코드를 반환합니다.

    `conditions[code]` 가 True 일 때만 해제하며, 수동 해제 원인은 조건이 True 여도 건드리지 않습니다.
    """
    cleared = []
    for code in active_halt_reasons(connection):
        if code in MANUAL_HALT_CODES:
            continue
        if conditions.get(code) is True and clear_flag(
            connection, "HALT", code, now=now, cleared_by="SYSTEM"
        ):
            cleared.append(code)
    return tuple(cleared)


def clear_halt_manually(
    connection: sqlite3.Connection,
    code: str,
    *,
    now: datetime,
    action_id: str,
    reconciliation_ok: bool,
) -> bool:
    """`/clear_halt` 처리: 수동 원인만, 대조 성공이 확인된 경우에만 해제합니다.

    자동 해제 원인을 강제로 해제하려는 요청은 PermissionError 로 거부합니다
    (조건이 해소되지 않았는데 사람이 덮어쓰는 것을 막기 위함).
    """
    if code not in MANUAL_HALT_CODES:
        raise PermissionError("Only manual-clear halt reasons can be cleared by the operator.")
    if reconciliation_ok is not True:
        raise PermissionError("A successful reconciliation is required before clearing a halt.")
    return clear_flag(connection, "HALT", code, now=now, cleared_by=action_id)


def read_host_state(path: str | Path, *, now: datetime, ttl_seconds: int) -> dict | None:
    """호스트가 기록한 상태 파일(`ups.json`·`clock.json`)을 읽습니다.

    파일이 없거나 JSON 이 아니거나 `updated_at` 이 TTL 보다 오래되면 None 을 반환합니다
    (감시 데몬 사망 = 상태 불명 = fail-closed 로 처리하라는 신호).
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        updated_at = datetime.fromisoformat(payload["updated_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if updated_at.tzinfo is None:
        return None
    if (now - updated_at).total_seconds() > ttl_seconds:
        return None
    return payload if isinstance(payload, dict) else None


def sync_host_state_flags(
    connection: sqlite3.Connection,
    run_dir: str | Path,
    *,
    now: datetime,
    ttl_seconds: int = 120,
    ups_halt_ratio: float = 0.20,
    ups_clear_margin: float = 0.10,
    clock_max_offset_seconds: float = 1.0,
) -> tuple[str, ...]:
    """UPS·시각 동기화 상태 파일로 `UPS_LOW`·`CLOCK_UNSYNCED` 를 등록하거나 자동 해제합니다."""
    ups = read_host_state(Path(run_dir) / "ups.json", now=now, ttl_seconds=ttl_seconds)
    clock = read_host_state(Path(run_dir) / "clock.json", now=now, ttl_seconds=ttl_seconds)

    ups_low = (
        ups is None
        or not isinstance(ups.get("battery_ratio"), (int, float))
        or ups["battery_ratio"] <= ups_halt_ratio
    )
    ups_recovered = (
        ups is not None
        and isinstance(ups.get("battery_ratio"), (int, float))
        and ups["battery_ratio"] >= ups_halt_ratio + ups_clear_margin
        and ups.get("on_external_power") is True
    )
    clock_bad = (
        clock is None
        or clock.get("synchronized") is not True
        or not isinstance(clock.get("offset_seconds"), (int, float))
        or abs(clock["offset_seconds"]) > clock_max_offset_seconds
    )
    if ups_low:
        raise_flag(connection, "HALT", "UPS_LOW", now=now, detail="UPS 상태 불명 또는 저전력")
    if clock_bad:
        raise_flag(connection, "HALT", "CLOCK_UNSYNCED", now=now, detail="시각 동기화 상태 불명")
    return evaluate_auto_clear(
        connection,
        {"UPS_LOW": ups_recovered, "CLOCK_UNSYNCED": not clock_bad},
        now=now,
    )


def load_risk_flags(connection: sqlite3.Connection, *, drawdown_mode=None) -> RiskFlags:
    """DB 의 독립 플래그와 활성 HALT 원인으로 `RiskFlags` 를 구성합니다."""
    def flag(key: str) -> bool:
        row = connection.execute(
            'SELECT value FROM system_state WHERE "key" = ?', (key,)
        ).fetchone()
        return row is not None and row[0] == "true"

    kwargs = {}
    if drawdown_mode is not None:
        kwargs["drawdown_mode"] = drawdown_mode
    return RiskFlags(
        is_safe_halt=is_safe_halt(connection),
        emergency_mode=flag("emergency_mode"),
        is_paused=flag("is_paused"),
        training_mode=flag("training_mode"),
        **kwargs,
    )
