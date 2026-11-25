"""백테스트 성과 지표(CAGR·MDD·회전율)의 단일 구현입니다.

`BacktestRun`(backtest/engine.py)의 속성이 이 함수들을 그대로 부릅니다. 예전에는 "합성 수익률" 실행기와 엔진이
같은 지표를 따로 구현했고, 테스트는 실제 엔진이 아닌 쪽을 검사했습니다(감사 9번).
"""

from collections.abc import Sequence


def calculate_cagr(
    starting_equity: float,
    ending_equity: float,
    years: float,
) -> float:
    """연복리 수익률. 기말 평가액이 0 이면 -100% 입니다."""
    if starting_equity <= 0 or ending_equity < 0 or years <= 0:
        raise ValueError("Invalid backtest CAGR inputs.")
    if ending_equity == 0:
        return -1.0
    return (ending_equity / starting_equity) ** (1 / years) - 1


def calculate_mdd(equity_curve: Sequence[float]) -> float:
    """최대 낙폭(음수). 평가액이 0 까지 떨어진 곡선도 -100% 로 계산합니다(음수 평가액만 거부)."""
    if not equity_curve:
        raise ValueError("Equity curve must not be empty.")
    peak = equity_curve[0]
    if peak <= 0:
        raise ValueError("Equity values must be positive.")
    mdd = 0.0
    for equity in equity_curve:
        if equity < 0:
            raise ValueError("Equity values must not be negative.")
        peak = max(peak, equity)
        mdd = min(mdd, equity / peak - 1)
    return mdd


def calculate_turnover(
    traded_notional: float,
    average_equity: float,
) -> float:
    """평균 평가액 대비 매매대금 비율(연환산은 호출자가 기간으로 나눈다)."""
    if traded_notional < 0 or average_equity <= 0:
        raise ValueError("Invalid turnover inputs.")
    return traded_notional / average_equity
