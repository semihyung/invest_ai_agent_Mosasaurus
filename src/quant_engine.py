import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


_LOOKBACK_WEIGHTS = ((20, 0.4), (60, 0.4), (120, 0.2))
_MIN_VOLATILITY = 1e-6
_MIN_HISTORY_SESSIONS = 200
_MAX_DAILY_RETURN = 0.30


@dataclass(frozen=True)
class RankedCandidate:
    ticker: str
    score: float
    sma_gap_ratio: float
    er120: float = 0.0
    avg_turnover_20d: float = 0.0


def _validate_prices(prices: Sequence[float]) -> None:
    for price in prices:
        if (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(price)
            or price <= 0
        ):
            raise ValueError("Prices must be finite positive numbers.")


def validate_daily_history(adjusted_closes: Sequence[float]) -> None:
    """Raise ValueError when the latest 200 adjusted closes fail quality checks."""
    if len(adjusted_closes) < _MIN_HISTORY_SESSIONS:
        raise ValueError("At least 200 adjusted closes are required.")

    recent_closes = adjusted_closes[-_MIN_HISTORY_SESSIONS:]
    _validate_prices(recent_closes)
    for previous, current in zip(recent_closes, recent_closes[1:]):
        daily_return = (current - previous) / previous
        if abs(daily_return) > _MAX_DAILY_RETURN:
            raise ValueError("Daily return exceeds the 30% data-quality limit.")


def calculate_momentum_score(
    adjusted_closes: Sequence[float],
    parking_adjusted_closes: Sequence[float],
) -> float | None:
    """Calculate the risk-adjusted excess momentum score for one asset."""
    if len(adjusted_closes) != len(parking_adjusted_closes):
        raise ValueError("Asset and parking price histories must have equal lengths.")
    if len(adjusted_closes) < 121:
        raise ValueError("At least 121 adjusted closes are required.")

    _validate_prices(adjusted_closes)
    _validate_prices(parking_adjusted_closes)

    asset_returns = [
        (current - previous) / previous
        for previous, current in zip(adjusted_closes, adjusted_closes[1:])
    ]
    parking_returns = [
        (current - previous) / previous
        for previous, current in zip(
            parking_adjusted_closes, parking_adjusted_closes[1:]
        )
    ]

    weighted_scores = []
    for lookback, weight in _LOOKBACK_WEIGHTS:
        asset_window = asset_returns[-lookback:]
        parking_window = parking_returns[-lookback:]
        asset_compound = math.prod(1 + daily_return for daily_return in asset_window)
        parking_compound = math.prod(1 + daily_return for daily_return in parking_window)
        compounded_excess = asset_compound - parking_compound
        excess_window = [
            asset_return - parking_return
            for asset_return, parking_return in zip(asset_window, parking_window)
        ]
        mean_return = math.fsum(excess_window) / lookback
        variance = math.fsum(
            (daily_return - mean_return) ** 2 for daily_return in excess_window
        ) / (lookback - 1)
        volatility = math.sqrt(variance)
        if volatility < _MIN_VOLATILITY:
            return None

        score = compounded_excess / max(volatility * math.sqrt(lookback), _MIN_VOLATILITY)
        weighted_scores.append(weight * score)

    return math.fsum(weighted_scores)


def is_above_sma120(
    current_price: float,
    completed_adjusted_closes: Sequence[float],
) -> bool:
    """Return whether the current price is strictly above the latest 120-day SMA."""
    if len(completed_adjusted_closes) < 120:
        raise ValueError("At least 120 completed adjusted closes are required.")

    _validate_prices((current_price,))
    _validate_prices(completed_adjusted_closes)
    sma120 = math.fsum(completed_adjusted_closes[-120:]) / 120
    return current_price > sma120


def passes_momentum_trend_filter(
    score: float | None,
    current_price: float,
    completed_adjusted_closes: Sequence[float],
) -> bool:
    """Apply the positive-momentum and strict SMA120 candidate filters."""
    above_sma120 = is_above_sma120(current_price, completed_adjusted_closes)
    if score is None:
        return False
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
    ):
        raise ValueError("Momentum score must be a finite number or None.")
    return score > 0 and above_sma120


def rank_candidates(
    candidates: Iterable[RankedCandidate],
) -> list[RankedCandidate]:
    """Sort positive, SMA-filtered candidates from highest to lowest score."""
    ranked = []
    for candidate in candidates:
        if not isinstance(candidate, RankedCandidate):
            raise TypeError("Candidates must be RankedCandidate values.")
        if (
            isinstance(candidate.score, bool)
            or not isinstance(candidate.score, (int, float))
            or not math.isfinite(candidate.score)
        ):
            raise ValueError("Candidate score must be finite.")
        if candidate.score <= 0:
            continue
        ranked.append(candidate)
    return sorted(
        ranked,
        key=lambda candidate: (
            -candidate.score,
            -candidate.er120,
            -candidate.avg_turnover_20d,
            candidate.ticker,
        ),
    )
