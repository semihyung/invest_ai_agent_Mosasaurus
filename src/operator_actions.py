"""운영자 2단계 확인 후 실행되는 DB 작업(승인·해제·분류·검증)을 모았습니다(명세 6.4절).

Telegram 핸들러는 확인 코드 검증까지만 맡고, 승인이 끝난 뒤의 실제 상태 변경은 이 모듈의
함수가 합니다. 모든 함수는 `pending_actions` 에 `APPROVED` 로 기록된 `action_id` 를 요구하므로
2단계 확인을 거치지 않은 호출은 거부되고, 모든 변경은 `audit_events` 에 남습니다.
"""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import strategy_params_hash
from src.safety_flags import (
    active_codes,
    clear_flag,
    clear_halt_manually,
)

_HEX64 = re.compile(r"[0-9a-f]{64}")
_LONG_NUMBER = re.compile(r"\d{8,}")


class ApprovalError(PermissionError):
    """승인 파일·확인 이력이 조건을 만족하지 않을 때 발생합니다."""


def _iso(value: datetime) -> str:
    """시간대가 있는 시각을 UTC ISO 문자열로 바꿉니다."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc).isoformat()


def _require_approved_action(
    connection: sqlite3.Connection, action_id: str, expected_type: str
) -> None:
    """`action_id` 가 해당 유형으로 승인(APPROVED)된 2단계 확인 이력인지 확인합니다."""
    row = connection.execute(
        "SELECT action_type, approval_status FROM pending_actions WHERE id = ?",
        (action_id,),
    ).fetchone()
    if row is None or row["approval_status"] != "APPROVED" or row["action_type"] != expected_type:
        raise ApprovalError("A matching approved two-step confirmation is required.")


def _audit(
    connection: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    target: str,
    detail: dict,
    correlation_id: str,
    now: datetime,
) -> None:
    """민감정보 없는 감사 이벤트를 기록합니다."""
    connection.execute(
        """
        INSERT INTO audit_events (actor, action, target, diff_sanitized, correlation_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (actor, action, target, json.dumps(detail, sort_keys=True), correlation_id, _iso(now)),
    )


def _read_approval_file(path: Path, expected_id: str, *, now: datetime) -> tuple[dict, str]:
    """호스트 승인 파일을 읽어 (내용, SHA-256) 을 반환하고 id·만료·계좌 평문 여부를 검사합니다."""
    try:
        raw = Path(path).read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise ApprovalError("Host approval file is missing or unreadable.") from exc
    if not isinstance(payload, dict) or payload.get("approval_id") != expected_id:
        raise ApprovalError("Approval id does not match the host approval file.")
    try:
        expires_at = datetime.fromisoformat(payload["expires_at"])
        approved_at = datetime.fromisoformat(payload["approved_at"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ApprovalError("Host approval file has invalid timestamps.") from exc
    if expires_at.tzinfo is None or approved_at.tzinfo is None or expires_at <= now:
        raise ApprovalError("Host approval file is expired.")
    alias = payload.get("account_alias")
    if not isinstance(alias, str) or not alias or _LONG_NUMBER.search(alias):
        raise ApprovalError("account_alias must be a non-numeric alias, never an account number.")
    return payload, hashlib.sha256(raw).hexdigest()


def approve_real_activation(
    connection: sqlite3.Connection,
    *,
    approval_id: str,
    action_id: str,
    approvals_dir: str | Path,
    now: datetime,
    max_days: int = 30,
) -> None:
    """`/approve_real`: 호스트 승인 파일과 일치할 때만 REAL 활성화 승인 레코드를 만듭니다."""
    _require_approved_action(connection, action_id, "APPROVE_REAL")
    payload, digest = _read_approval_file(
        Path(approvals_dir) / "real_activation.json", approval_id, now=now
    )
    if datetime.fromisoformat(payload["expires_at"]) - datetime.fromisoformat(
        payload["approved_at"]
    ) > timedelta(days=max_days):
        raise ApprovalError("REAL activation validity exceeds the maximum allowed days.")
    with connection:
        connection.execute(
            """
            INSERT INTO operator_approvals (
                approval_id, approval_type, host_file_sha256, status,
                created_at, expires_at, approved_via_action_id
            ) VALUES (?, 'REAL_ACTIVATION', ?, 'ACTIVE', ?, ?, ?)
            """,
            (approval_id, digest, _iso(now), payload["expires_at"], action_id),
        )
        _audit(connection, actor="operator", action="APPROVE_REAL", target=approval_id,
               detail={"file_sha256": digest[:8]}, correlation_id=action_id, now=now)


def real_activation_valid(
    connection: sqlite3.Connection, approvals_dir: str | Path, *, now: datetime
) -> bool:
    """REAL 3중 잠금의 2·3번(승인 파일 + DB 레코드 일치·미만료)을 확인합니다(7.1절)."""
    path = Path(approvals_dir) / "real_activation.json"
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        expires_at = datetime.fromisoformat(payload["expires_at"])
        approval_id = payload["approval_id"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if expires_at.tzinfo is None or expires_at <= now:
        return False
    row = connection.execute(
        """
        SELECT host_file_sha256, expires_at FROM operator_approvals
        WHERE approval_id = ? AND approval_type = 'REAL_ACTIVATION' AND status = 'ACTIVE'
        """,
        (approval_id,),
    ).fetchone()
    return (
        row is not None
        and row["host_file_sha256"] == hashlib.sha256(raw).hexdigest()
        and datetime.fromisoformat(row["expires_at"]) > now
    )


def approve_smoke_session(
    connection: sqlite3.Connection,
    *,
    approval_id: str,
    action_id: str,
    approvals_dir: str | Path,
    now: datetime,
    max_minutes: int = 60,
) -> None:
    """`/approve_smoke`: REAL 활성 상태에서만, 최대 60분짜리 스모크 세션 승인을 만듭니다."""
    _require_approved_action(connection, action_id, "APPROVE_SMOKE")
    if not real_activation_valid(connection, approvals_dir, now=now):
        raise ApprovalError("A valid REAL activation is required for a smoke session.")
    payload, digest = _read_approval_file(
        Path(approvals_dir) / "smoke_session.json", approval_id, now=now
    )
    expires_at = min(
        datetime.fromisoformat(payload["expires_at"]), now + timedelta(minutes=max_minutes)
    )
    with connection:
        connection.execute(
            """
            INSERT INTO operator_approvals (
                approval_id, approval_type, host_file_sha256, status,
                created_at, expires_at, approved_via_action_id
            ) VALUES (?, 'SMOKE_SESSION', ?, 'ACTIVE', ?, ?, ?)
            """,
            (approval_id, digest, _iso(now), _iso(expires_at), action_id),
        )
        _audit(connection, actor="operator", action="APPROVE_SMOKE", target=approval_id,
               detail={"file_sha256": digest[:8]}, correlation_id=action_id, now=now)


def smoke_session_active(connection: sqlite3.Connection, *, now: datetime) -> str | None:
    """활성 스모크 세션의 approval_id 를 반환합니다(만료된 세션은 EXPIRED 로 닫음)."""
    with connection:
        connection.execute(
            """
            UPDATE operator_approvals SET status = 'EXPIRED'
            WHERE approval_type = 'SMOKE_SESSION' AND status = 'ACTIVE' AND expires_at <= ?
            """,
            (_iso(now),),
        )
    row = connection.execute(
        """
        SELECT approval_id FROM operator_approvals
        WHERE approval_type = 'SMOKE_SESSION' AND status = 'ACTIVE'
        """
    ).fetchone()
    return row["approval_id"] if row else None


def consume_smoke_session(
    connection: sqlite3.Connection, approval_id: str, *, now: datetime
) -> bool:
    """스모크 주문 1건이 종결되면 세션을 `CONSUMED` 로 닫고 감사 기록을 남깁니다."""
    with connection:
        cursor = connection.execute(
            """
            UPDATE operator_approvals SET status = 'CONSUMED', consumed_at = ?
            WHERE approval_id = ? AND approval_type = 'SMOKE_SESSION' AND status = 'ACTIVE'
            """,
            (_iso(now), approval_id),
        )
        if cursor.rowcount:
            _audit(connection, actor="order_manager", action="SMOKE_SESSION_CONSUMED",
                   target=approval_id, detail={}, correlation_id=approval_id, now=now)
    return cursor.rowcount == 1


def approve_backtest(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    report_sha256: str,
    code_commit_sha: str,
    strategy_params_hash: str,
    data_range: tuple[str, str],
    action_id: str,
    now: datetime,
) -> str:
    """`/approve_backtest`: 현재 전략 해시와 보고서 해시를 묶어 백테스트 승인을 기록합니다."""
    _require_approved_action(connection, action_id, "APPROVE_BACKTEST")
    for name, value in (("report_sha256", report_sha256), ("strategy_params_hash", strategy_params_hash)):
        if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
            raise ValueError(f"{name} must be a 64-character lowercase hex SHA-256.")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id is required.")
    approval_id = f"BT-{run_id}"
    with connection:
        connection.execute(
            """
            INSERT INTO backtest_approvals (
                approval_id, backtest_run_id, code_commit_sha, strategy_params_hash,
                data_range_start, data_range_end, report_sha256, status,
                approved_at, approved_via_action_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'APPROVED', ?, ?)
            """,
            (approval_id, run_id, code_commit_sha, strategy_params_hash,
             data_range[0], data_range[1], report_sha256, _iso(now), action_id),
        )
        _audit(connection, actor="operator", action="APPROVE_BACKTEST", target=approval_id,
               detail={"report": report_sha256[:8]}, correlation_id=action_id, now=now)
    return approval_id


def has_valid_backtest_approval(
    connection: sqlite3.Connection, current_strategy_params_hash: str
) -> bool:
    """현재 전략 설정 해시와 일치하는 `APPROVED` 백테스트 승인이 있는지 확인합니다(7.1절 일반 전략 게이트)."""
    return connection.execute(
        """
        SELECT 1 FROM backtest_approvals
        WHERE status = 'APPROVED' AND strategy_params_hash = ? LIMIT 1
        """,
        (current_strategy_params_hash,),
    ).fetchone() is not None


def clear_halt(
    connection: sqlite3.Connection,
    *,
    code: str,
    action_id: str,
    reconciliation_ok: bool,
    now: datetime,
) -> bool:
    """`/clear_halt`: 수동 해제 원인만, 승인 이력과 대조 성공이 있을 때 해제합니다."""
    _require_approved_action(connection, action_id, "CLEAR_HALT")
    cleared = clear_halt_manually(
        connection, code, now=now, action_id=action_id, reconciliation_ok=reconciliation_ok
    )
    with connection:
        _audit(connection, actor="operator", action="CLEAR_HALT", target=code,
               detail={"cleared": cleared}, correlation_id=action_id, now=now)
    return cleared


def classify_cash_flow(
    connection: sqlite3.Connection,
    *,
    flow_id: int,
    flow_type: str,
    action_id: str,
    now: datetime,
) -> None:
    """`/classify`: 미분류 현금흐름을 확정하거나(`DEPOSIT`·`WITHDRAWAL`·`DISTRIBUTION`·`ADJUSTMENT`)
    오탐으로 무효화(`DISMISS`)하고, 마지막 건이면 매수 차단을 해제합니다.

    `ADJUSTMENT`(부호 무관)·`DISMISS` 는 단위 수를 바꾸지 않습니다. 입출금은 단위 조정이 필요하므로
    호출자가 `performance.restate_after_late_flow` 로 발생일 직전 단위가격 기준 재계산을 이어서 수행합니다.
    """
    _require_approved_action(connection, action_id, "CLASSIFY_FLOW")
    if flow_type not in ("DEPOSIT", "WITHDRAWAL", "DISTRIBUTION", "ADJUSTMENT", "DISMISS"):
        raise ValueError(
            "flow_type must be DEPOSIT, WITHDRAWAL, DISTRIBUTION, ADJUSTMENT or DISMISS."
        )
    row = connection.execute(
        "SELECT amount, classification_status FROM cash_flows WHERE id = ?", (flow_id,)
    ).fetchone()
    if row is None or row["classification_status"] != "UNCLASSIFIED_CASH_FLOW":
        raise ValueError("Only an unclassified cash flow can be classified.")
    if flow_type in ("DEPOSIT", "WITHDRAWAL", "DISTRIBUTION") and (
        (flow_type == "WITHDRAWAL") != (row["amount"] < 0)
    ):
        raise ValueError("Cash-flow type contradicts the sign of the observed amount.")
    with connection:
        if flow_type == "DISMISS":
            connection.execute(
                """
                UPDATE cash_flows SET classification_status = 'DISMISSED',
                    confirmed_at = ?, confirmed_via_action_id = ?
                WHERE id = ?
                """,
                (_iso(now), action_id, flow_id),
            )
        else:
            connection.execute(
                """
                UPDATE cash_flows SET flow_type = ?, classification_status = 'CONFIRMED',
                    confirmed_at = ?, confirmed_via_action_id = ?
                WHERE id = ?
                """,
                (flow_type, _iso(now), action_id, flow_id),
            )
        _audit(connection, actor="operator", action="CLASSIFY_FLOW", target=str(flow_id),
               detail={"flow_type": flow_type}, correlation_id=action_id, now=now)
    remaining = connection.execute(
        "SELECT 1 FROM cash_flows WHERE classification_status = 'UNCLASSIFIED_CASH_FLOW' LIMIT 1"
    ).fetchone()
    if remaining is None:
        clear_flag(connection, "BUY_BLOCK", "UNCLASSIFIED_CASH_FLOW", now=now, cleared_by=action_id)


def confirm_corporate_action(
    connection: sqlite3.Connection,
    *,
    corporate_action_id: int,
    action_id: str,
    now: datetime,
) -> float:
    """`/ca_confirm`: 감지된 기업행위를 검증하고 PeakPrice 를 재기준화한 뒤 종목 격리를 해제합니다."""
    _require_approved_action(connection, action_id, "CA_CONFIRM")
    row = connection.execute(
        "SELECT ticker, detected_factor, verified FROM corporate_actions WHERE id = ?",
        (corporate_action_id,),
    ).fetchone()
    if row is None or row["verified"] == 1:
        raise ValueError("Corporate action is missing or already verified.")
    factor = row["detected_factor"]
    if not isinstance(factor, (int, float)) or not 0 < factor <= 1.0:
        raise ValueError("A detected adjustment factor in (0, 1] is required.")
    with connection:
        connection.execute(
            """
            UPDATE corporate_actions
            SET verified = 1, verification_source = 'OPERATOR_CONFIRMED',
                adjustment_factor = ?, verified_at = ?
            WHERE id = ?
            """,
            (factor, _iso(now), corporate_action_id),
        )
        # 같은 트랜잭션에서 PeakPrice 를 새 수정 기준으로 재기준화합니다.
        connection.execute(
            "UPDATE positions SET peak_price_adj = peak_price_adj * ? WHERE ticker = ? AND peak_price_adj IS NOT NULL",
            (factor, row["ticker"]),
        )
        _audit(connection, actor="operator", action="CA_CONFIRM", target=str(corporate_action_id),
               detail={"ticker": row["ticker"], "factor": factor},
               correlation_id=action_id, now=now)
    unverified_left = connection.execute(
        "SELECT 1 FROM corporate_actions WHERE ticker = ? AND verified = 0 LIMIT 1",
        (row["ticker"],),
    ).fetchone()
    if unverified_left is None:
        clear_flag(connection, "TICKER_BLOCK", "CORPORATE_ACTION_UNVERIFIED",
                   now=now, cleared_by=action_id, ticker=row["ticker"])
    return float(factor)


def adopt_broker_positions(
    connection: sqlite3.Connection,
    broker_quantities: dict[str, int],
    *,
    action_id: str,
    now: datetime,
) -> dict[str, tuple[int, int]]:
    """`/reconcile adopt`: KIS(SSOT) 보유 수량을 DB `positions` 에 채택하고 차이를 감사 기록합니다.

    유니버스에 없는 종목은 `UNMANAGED` 로 기록하며 자동 매매하지 않습니다. DB 에만 있는 종목은
    수량 0 으로 맞춥니다. 반환값은 {티커: (이전 수량, 새 수량)} 입니다.
    """
    _require_approved_action(connection, action_id, "RECONCILE_ADOPT")
    for ticker, quantity in broker_quantities.items():
        if (
            not isinstance(ticker, str)
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 0
        ):
            raise ValueError("Broker quantities must be non-negative integers.")
    known = {r["ticker"] for r in connection.execute("SELECT ticker FROM universe_assets")}
    existing = {r["ticker"]: r["quantity"] for r in connection.execute("SELECT ticker, quantity FROM positions")}
    changes: dict[str, tuple[int, int]] = {}
    with connection:
        for ticker in sorted(set(existing) | set(broker_quantities)):
            before, after = existing.get(ticker, 0), broker_quantities.get(ticker, 0)
            if before == after and ticker in existing:
                continue
            tracking = "MANAGED" if ticker in known else "UNMANAGED"
            connection.execute(
                """
                INSERT INTO positions (
                    ticker, tracking_status, quantity, avg_buy_price, market_value,
                    peak_price_adj, last_synced_at
                ) VALUES (?, ?, ?, 0, 0, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    quantity = excluded.quantity, last_synced_at = excluded.last_synced_at
                """,
                (ticker, tracking, after, 1.0 if tracking == "MANAGED" else None, _iso(now)),
            )
            changes[ticker] = (before, after)
        _audit(connection, actor="operator", action="RECONCILE_ADOPT", target="positions",
               detail={t: list(c) for t, c in changes.items()},
               correlation_id=action_id, now=now)
    return changes


def release_emergency(
    connection: sqlite3.Connection,
    *,
    action_id: str,
    floor_until_date: str,
    now: datetime,
) -> bool:
    """`/emergency off`: 해제하고, 낙폭 원인이면 HWM 리베이스 대기와 `WARNING` 하한을 설정합니다.

    미분류 현금흐름이 있어 HWM 이 동결된 상태에서는 리베이스 기준이 불확실하므로 거부합니다.
    반환값은 HWM 리베이스를 예약했는지(낙폭 원인이었는지) 여부입니다.
    """
    _require_approved_action(connection, action_id, "EMERGENCY_OFF")
    reason_row = connection.execute(
        'SELECT value FROM system_state WHERE "key" = ?', ("emergency_reason",)
    ).fetchone()
    drawdown_reason = reason_row is not None and reason_row["value"] == "DRAWDOWN"
    if drawdown_reason and active_codes(connection, "BUY_BLOCK"):
        raise ApprovalError("HWM is frozen by an unresolved buy block; resolve it before releasing.")
    timestamp = _iso(now)
    with connection:
        for key, value in (("emergency_mode", "false"), ("emergency_reason", "")):
            connection.execute(
                """
                INSERT INTO system_state ("key", value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT("key") DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, timestamp),
            )
        if drawdown_reason:
            for key, value in (("hwm_rebase_pending", "true"),
                               ("post_emergency_floor_until", floor_until_date)):
                connection.execute(
                    """
                    INSERT INTO system_state ("key", value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT("key") DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (key, value, timestamp),
                )
        _audit(connection, actor="operator", action="EMERGENCY_OFF", target="system_state",
               detail={"hwm_rebase": drawdown_reason}, correlation_id=action_id, now=now)
    return drawdown_reason


def real_gate_inputs(
    connection: sqlite3.Connection,
    config,
    approvals_dir: str | Path,
    *,
    now: datetime,
) -> dict[str, bool]:
    """`RealOrderGateContext` 의 승인 관련 입력 3개를 DB·호스트 파일에서 계산합니다(7.1절).

    - `operator_approval`: 환경이 REAL 이고 승인 파일 + DB 레코드가 일치할 때만 True(3중 잠금).
    - `backtest_calibrated`: 현재 전략 설정 해시와 일치하는 `APPROVED` 백테스트 승인이 있을 때 True
      (PAPER 에서 `deployment.paper_requires_backtest_approval` 가 false 이면 생략).
    - `smoke_test_approved`: REAL 활성 상태에서 활성 스모크 세션이 있을 때 True.
    """
    deployment = config["deployment"]
    environment = deployment["environment"]
    operator_approval = (
        environment == "REAL" and real_activation_valid(connection, approvals_dir, now=now)
    )
    if environment == "PAPER" and deployment.get("paper_requires_backtest_approval") is False:
        calibrated = True
    else:
        calibrated = has_valid_backtest_approval(connection, strategy_params_hash(config))
    smoke = operator_approval and smoke_session_active(connection, now=now) is not None
    return {
        "operator_approval": operator_approval,
        "backtest_calibrated": calibrated,
        "smoke_test_approved": smoke,
    }
