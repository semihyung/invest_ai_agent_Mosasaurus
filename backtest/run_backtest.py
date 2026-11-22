from dataclasses import dataclass
from collections.abc import Sequence


@dataclass(frozen=True)
class BacktestResult:
    cagr: float
    mdd: float
    turnover_ratio: float
    trades: int


def calculate_cagr(
    starting_equity: float,
    ending_equity: float,
    years: float,
) -> float:
    if starting_equity <= 0 or ending_equity < 0 or years <= 0:
        raise ValueError("Invalid backtest CAGR inputs.")
    if ending_equity == 0:
        return -1.0
    return (ending_equity / starting_equity) ** (1 / years) - 1


def calculate_mdd(equity_curve: Sequence[float]) -> float:
    if not equity_curve:
        raise ValueError("Equity curve must not be empty.")
    peak = equity_curve[0]
    mdd = 0.0
    for equity in equity_curve:
        if equity <= 0:
            raise ValueError("Equity values must be positive.")
        peak = max(peak, equity)
        mdd = min(mdd, equity / peak - 1)
    return mdd


def calculate_turnover(
    traded_notional: float,
    average_equity: float,
) -> float:
    if traded_notional < 0 or average_equity <= 0:
        raise ValueError("Invalid turnover inputs.")
    return traded_notional / average_equity


def run_synthetic_backtest(
    starting_equity: float,
    annual_price_returns: Sequence[float],
) -> BacktestResult:
    if starting_equity <= 0:
        raise ValueError("starting_equity must be positive.")
    equity = starting_equity
    curve = [equity]
    traded_notional = 0.0
    trades = 0
    for annual_return in annual_price_returns:
        previous = equity
        equity *= 1 + annual_return
        if annual_return != 0:
            traded_notional += previous * abs(annual_return)
            trades += 1
        curve.append(equity)

    years = len(annual_price_returns) / 252
    if years <= 0:
        raise ValueError("At least one trading period is required.")
    average_equity = sum(curve) / len(curve)
    return BacktestResult(
        cagr=calculate_cagr(starting_equity, equity, years),
        mdd=calculate_mdd(curve),
        turnover_ratio=calculate_turnover(traded_notional, average_equity),
        trades=trades,
    )
