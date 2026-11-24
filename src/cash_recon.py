"""예수금 대사(Cash Reconciliation): 설명되지 않는 예수금 변동을 찾아 현금흐름 후보로 만듭니다.

KIS 입출금 조회 TR이 확인되지 않았으므로(명세 2.2절) Phase 1은 D+2 정산 예수금 하나로
"직전 마감 예수금 + 당일 체결 현금 변동 = 기대 예수금" 을 계산하고, 실제 값과의
차이(설명되지 않는 변동)를 미분류 현금흐름으로 격리합니다. 모두 순수 함수입니다.
"""

from datetime import date


def _require_int(name: str, value: int) -> None:
    """금액이 bool 이 아닌 정수(원 단위)인지 확인합니다."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer KRW amount.")


def unexplained_cash_delta(
    previous_d2_cash: int,
    trade_cash_delta: int,
    current_d2_cash: int,
    *,
    tolerance_krw: int = 10,
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


def in_learning_period(first_run: date, today: date, *, days: int = 14) -> bool:
    """첫 운용 후 `days`일 동안은 불일치를 경보만 하는 학습 기간인지 반환합니다."""
    if not isinstance(first_run, date) or not isinstance(today, date):
        raise ValueError("Dates are required.")
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise ValueError("days must be a non-negative integer.")
    return (today - first_run).days < days
