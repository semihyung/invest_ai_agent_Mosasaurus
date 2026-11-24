"""예수금 대사(Cash Reconciliation): 설명되지 않는 예수금 변동을 찾아 현금흐름 후보로 만듭니다.

KIS 입출금 조회 TR이 확인되지 않았으므로(명세 2.2절) Phase 1은 D+2 정산 예수금 하나로
"직전 마감 예수금 + 당일 체결 현금 변동 = 기대 예수금" 을 계산하고, 실제 값과의
차이(설명되지 않는 변동)를 미분류 현금흐름으로 격리합니다. 모두 순수 함수입니다.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from enum import Enum


def _require_int(name: str, value: int) -> None:
    """금액이 bool 이 아닌 정수(원 단위)인지 확인합니다."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer KRW amount.")


def unexplained_cash_delta(
    previous_d2_cash: int,
    trade_cash_delta: int,
    current_d2_cash: int,
    *,
    tolerance_krw: int = 1000,
) -> int:
    """설명되지 않는 D+2 예수금 변동을 반환합니다(허용 오차 이내면 0).

    `trade_cash_delta` 는 당일 체결로 인한 현금 변동(매도 수령액 − 매수 지급액 − 수수료·세금)이다.
    """
    for name, value in (
        ("previous_d2_cash", previous_d2_cash),
        ("trade_cash_delta", trade_cash_delta),
        ("current_d2_cash", current_d2_cash),
        ("tolerance_krw", tolerance_krw),
    ):
        _require_int(name, value)
    if tolerance_krw < 0:
        raise ValueError("tolerance_krw must be non-negative.")
    delta = current_d2_cash - (previous_d2_cash + trade_cash_delta)
    return 0 if abs(delta) <= tolerance_krw else delta


def matches_expected_distribution(
    delta: int,
    expected_cash_amount: int,
    *,
    tolerance_ratio: float = 0.05,
) -> bool:
    """입금 변동이 검증된 분배 이벤트의 기대 금액과 허용 오차 안에서 일치하는지 봅니다."""
    _require_int("delta", delta)
    _require_int("expected_cash_amount", expected_cash_amount)
    if isinstance(tolerance_ratio, bool) or not 0 <= tolerance_ratio < 1:
        raise ValueError("tolerance_ratio must be in [0, 1).")
    if delta <= 0 or expected_cash_amount <= 0:
        return False
    return abs(delta - expected_cash_amount) <= expected_cash_amount * tolerance_ratio


def reconciliation_transaction_id(trade_date: date, sequence: int) -> str:
    """브로커 거래 ID가 없을 때 쓰는 합성 키 `RECON:{거래일}:{순번}` 을 만듭니다."""
    if not isinstance(trade_date, date):
        raise ValueError("trade_date must be a date.")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ValueError("sequence must be a positive integer.")
    return f"RECON:{trade_date.isoformat()}:{sequence}"


def reconciliation_allowed(
    *,
    active_order_count: int,
    last_fill_at: datetime | None,
    now: datetime,
    grace_seconds: int = 60,
) -> bool:
    """대사·수량 대조를 판정해도 되는 "정지 상태"인지 반환합니다(명세 2.2절 v3.7.5).

    ① 계좌 전체에 활성 주문이 0건이고 ② 마지막 체결 반영 후 `grace_seconds` 이상 지났을 때만 True 입니다.
    체결이 진행되는 중에는 `trade_fills` 와 KIS 잔고·예수금의 반영 시차 때문에 가짜 불일치가 생기므로,
    False 이면 판정을 건너뛰고(불일치로 보지 않고) 다음 주기에 다시 시도합니다.
    """
    if isinstance(active_order_count, bool) or not isinstance(active_order_count, int) or active_order_count < 0:
        raise ValueError("active_order_count must be a non-negative integer.")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    if last_fill_at is not None and (not isinstance(last_fill_at, datetime) or last_fill_at.tzinfo is None):
        raise ValueError("last_fill_at must be timezone-aware or None.")
    if active_order_count > 0:
        return False
    return last_fill_at is None or (now - last_fill_at).total_seconds() >= grace_seconds


class DiscrepancyDecision(str, Enum):
    """정지 상태 대사 한 번의 판정 결과입니다."""

    NONE = "NONE"                    # 차이 없음(기존 PROVISIONAL 이 없으면 아무 일도 없음)
    DISMISSED = "DISMISSED"          # 다음 대사가 차이를 설명 → PROVISIONAL 자동 무효화
    PROVISIONAL = "PROVISIONAL"      # 첫 관측: 매수만 대기
    WAIT = "WAIT"                    # 재관측 최소 간격 전: 상태 유지
    UNCLASSIFIED = "UNCLASSIFIED"    # 간격 이상 뒤 같은 부호로 재현 → 미분류 확정


def judge_discrepancy(
    delta: int,
    *,
    provisional_amount: int | None,
    first_observed_at: datetime | None,
    now: datetime,
    confirm_interval_seconds: int = 60,
    tolerance_krw: int = 1000,
) -> DiscrepancyDecision:
    """설명되지 않는 변동(`delta`, 허용 오차 이내면 0)을 2단계(관측→재관측)로 판정합니다.

    - 차이 없음: 기존 `PROVISIONAL` 이 있으면 `DISMISSED`(자동 무효화), 없으면 `NONE`.
    - 차이 있음 + 기존 `PROVISIONAL` 없음: `PROVISIONAL`(첫 관측, 신규 매수만 대기).
    - 차이 있음 + 기존 `PROVISIONAL`: 재관측 간격 전이면 `WAIT`, 간격 이상이고 같은 부호이며
      금액이 허용 오차 안에서 같으면 `UNCLASSIFIED`, 금액·부호가 다르면 새 관측으로 보고 `WAIT`.
    """
    for name, value in (("delta", delta), ("tolerance_krw", tolerance_krw)):
        _require_int(name, value)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    if delta == 0:
        return DiscrepancyDecision.DISMISSED if provisional_amount is not None else DiscrepancyDecision.NONE
    if provisional_amount is None or first_observed_at is None:
        return DiscrepancyDecision.PROVISIONAL
    if (now - first_observed_at).total_seconds() < confirm_interval_seconds:
        return DiscrepancyDecision.WAIT
    same_sign = (delta > 0) == (provisional_amount > 0)
    if same_sign and abs(delta - provisional_amount) <= tolerance_krw:
        return DiscrepancyDecision.UNCLASSIFIED
    return DiscrepancyDecision.WAIT


def _iso(value: datetime) -> str:
    """시간대가 있는 시각을 UTC ISO 문자열로 바꿉니다."""
    return value.astimezone(timezone.utc).isoformat()


def record_discrepancy(
    connection: sqlite3.Connection,
    delta: int,
    *,
    trade_date: date,
    now: datetime,
    confirm_interval_seconds: int = 60,
    tolerance_krw: int = 1000,
    learning_period: bool = False,
) -> DiscrepancyDecision:
    """정지 상태 대사 결과를 `cash_flows` 에 반영합니다(PROVISIONAL → UNCLASSIFIED_CASH_FLOW / DISMISSED).

    확정(`UNCLASSIFIED_CASH_FLOW`) 시 신규 매수 차단 플래그를 올립니다. 학습 기간(`learning_period`)
    에는 매수 차단만 생략하고 경보(`system_events` WARN)만 남깁니다(HWM 상향 동결은 호출자가 유지).
    """
    # 지연 import: safety_flags 가 risk_manager 를 쓰고 이 모듈은 순수 함수 중심이라 순환을 피한다.
    from src.safety_flags import raise_flag

    row = connection.execute(
        """
        SELECT id, amount, first_observed_at FROM cash_flows
        WHERE classification_status = 'PROVISIONAL' AND detection_method = 'CASH_RECON'
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    provisional_amount = row["amount"] if row else None
    first_seen = datetime.fromisoformat(row["first_observed_at"]) if row else None
    decision = judge_discrepancy(
        delta,
        provisional_amount=provisional_amount,
        first_observed_at=first_seen,
        now=now,
        confirm_interval_seconds=confirm_interval_seconds,
        tolerance_krw=tolerance_krw,
    )
    stamp = _iso(now)
    with connection:
        if decision is DiscrepancyDecision.DISMISSED:
            connection.execute(
                "UPDATE cash_flows SET classification_status = 'DISMISSED', confirmed_at = ?, "
                "memo = 'AUTO_DISMISSED_BY_RECON' WHERE id = ?",
                (stamp, row["id"]),
            )
        elif decision is DiscrepancyDecision.PROVISIONAL:
            sequence = connection.execute(
                "SELECT COUNT(*) FROM cash_flows WHERE broker_transaction_id LIKE ?",
                (f"RECON:{trade_date.isoformat()}:%",),
            ).fetchone()[0] + 1
            connection.execute(
                """
                INSERT INTO cash_flows (
                    broker_transaction_id, detection_method, flow_date, occurred_at, amount,
                    flow_type, classification_status, source, observed_at, first_observed_at
                ) VALUES (?, 'CASH_RECON', ?, ?, ?, 'UNCLASSIFIED', 'PROVISIONAL', 'CASH_RECON', ?, ?)
                """,
                (reconciliation_transaction_id(trade_date, sequence), trade_date.isoformat(),
                 stamp, delta, stamp, stamp),
            )
        elif decision is DiscrepancyDecision.UNCLASSIFIED:
            connection.execute(
                "UPDATE cash_flows SET classification_status = 'UNCLASSIFIED_CASH_FLOW', "
                "reconfirmed_at = ?, observed_at = ?, amount = ? WHERE id = ?",
                (stamp, stamp, delta, row["id"]),
            )
    if decision is DiscrepancyDecision.UNCLASSIFIED:
        if learning_period:
            with connection:
                connection.execute(
                    "INSERT INTO system_events (component, event_type, severity, message, created_at) "
                    "VALUES ('cash_recon', 'LEARNING_PERIOD_UNCLASSIFIED', 'WARN', ?, ?)",
                    (f"학습 기간 미분류 현금흐름 {delta}원(매수 차단 생략, HWM 동결 유지)", stamp),
                )
        else:
            raise_flag(connection, "BUY_BLOCK", "UNCLASSIFIED_CASH_FLOW", now=now)
    return decision


def buy_wait_for_cash_flow(connection: sqlite3.Connection) -> bool:
    """`PROVISIONAL` 또는 `UNCLASSIFIED_CASH_FLOW` 행이 있어 신규 매수를 기다려야 하는지 반환합니다."""
    return connection.execute(
        "SELECT 1 FROM cash_flows WHERE classification_status IN "
        "('PROVISIONAL', 'UNCLASSIFIED_CASH_FLOW') LIMIT 1"
    ).fetchone() is not None


def hwm_frozen_for_cash_flow(connection: sqlite3.Connection) -> bool:
    """HWM 상향 갱신을 동결해야 하는지 반환합니다(`PROVISIONAL` 은 해당 없음, 6.2절 v3.7.5)."""
    return connection.execute(
        "SELECT 1 FROM cash_flows WHERE classification_status = 'UNCLASSIFIED_CASH_FLOW' LIMIT 1"
    ).fetchone() is not None


def excluded_unclassified_amount(connection: sqlite3.Connection) -> int:
    """오염 제거 단위가격 계산에서 평가액에서 빼야 하는 미분류 금액의 합(입금 +, 출금 −)입니다."""
    return connection.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM cash_flows "
        "WHERE classification_status = 'UNCLASSIFIED_CASH_FLOW'"
    ).fetchone()[0]


def in_learning_period(first_run: date, today: date, *, days: int = 14) -> bool:
    """첫 운용 후 `days`일 동안은 매수 차단만 생략하고 경보만 보내는 학습 기간인지 반환합니다."""
    if not isinstance(first_run, date) or not isinstance(today, date):
        raise ValueError("Dates are required.")
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise ValueError("days must be a non-negative integer.")
    return (today - first_run).days < days
