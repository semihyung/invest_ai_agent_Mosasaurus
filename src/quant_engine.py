"""수정주가 기반 모멘텀 점수와 위험자산 후보 적격성을 계산합니다."""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


_LOOKBACK_WEIGHTS = ((20, 0.4), (60, 0.4), (120, 0.2))
_MIN_VOLATILITY = 0.005  # 일간 초과변동성 하한(명세 5.1절, 0.5%)
_MIN_HISTORY_SESSIONS = 200
_MAX_DAILY_RETURN = 0.30


@dataclass(frozen=True)
class RankedCandidate:
    """순위 계산에 필요한 후보 종목의 점수와 보조 지표입니다."""

    ticker: str
    score: float
    sma_gap_ratio: float
    er120: float = 0.0
    avg_turnover_20d: float = 0.0


@dataclass(frozen=True)
class CandidateProfile:
    """6단계 후보 필터에 필요한 종목 상태와 수정주가 데이터를 담습니다."""

    ticker: str
    category: str
    status: str
    data_status: str
    product_type: str
    is_leveraged: bool
    is_inverse: bool
    is_futures: bool
    is_etn: bool
    adjusted_closes: Sequence[float]
    parking_adjusted_closes: Sequence[float]
    avg_turnover_20d: float
    estimated_order_amount: int
    currently_held: bool = False
    blocked_until_next_rotation: bool = False
    classification_verified: bool = False
    leverage_multiplier: float = 1.0
    replication_method: str = "PHYSICAL"


@dataclass(frozen=True)
class CandidateAssessment:
    """후보의 순위 산입 여부와 신규 진입 허용 결과입니다."""

    candidate: RankedCandidate | None
    eligible_for_new_entry: bool
    failed_filter: int | None = None
    rejection_code: str | None = None


def _validate_prices(prices: Sequence[float]) -> None:
    """가격 시계열의 유한 양수 조건을 검사합니다."""
    for price in prices:
        if (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(price)
            or price <= 0
        ):
            raise ValueError("Prices must be finite positive numbers.")


def _daily_returns(adjusted_closes: Sequence[float]) -> list[float]:
    """완료된 수정 종가로부터 일간 수익률을 계산합니다."""
    returns = [
        (current - previous) / previous
        for previous, current in zip(adjusted_closes, adjusted_closes[1:])
    ]
    if any(not math.isfinite(value) for value in returns):
        raise ValueError("Daily returns must be finite.")
    return returns


def _compounded_excess_return(
    asset_returns: Sequence[float],
    parking_returns: Sequence[float],
) -> float:
    """자산과 파킹 자산의 복리 누적 수익률 차이를 계산합니다."""
    asset_compound = math.prod(1 + value for value in asset_returns)
    parking_compound = math.prod(1 + value for value in parking_returns)
    result = asset_compound - parking_compound
    if not math.isfinite(result):
        raise ValueError("Compounded excess return must be finite.")
    return result


def validate_daily_history(adjusted_closes: Sequence[float]) -> None:
    """최근 200개 수정 종가가 데이터 품질 기준에 맞는지 확인합니다."""
    if len(adjusted_closes) < _MIN_HISTORY_SESSIONS:
        raise ValueError("At least 200 adjusted closes are required.")

    recent_closes = adjusted_closes[-_MIN_HISTORY_SESSIONS:]
    _validate_prices(recent_closes)
    for daily_return in _daily_returns(recent_closes):
        if abs(daily_return) > _MAX_DAILY_RETURN:
            raise ValueError("Daily return exceeds the 30% data-quality limit.")


def calculate_momentum_score(
    adjusted_closes: Sequence[float],
    parking_adjusted_closes: Sequence[float],
    *,
    min_daily_excess_volatility: float = _MIN_VOLATILITY,
) -> float:
    """복리 초과수익률과 ddof=1 초과변동성으로 최종 모멘텀 점수를 구합니다."""
    if len(adjusted_closes) != len(parking_adjusted_closes):
        raise ValueError("Asset and parking price histories must have equal lengths.")
    if len(adjusted_closes) < 121:
        raise ValueError("At least 121 adjusted closes are required.")
    if (
        isinstance(min_daily_excess_volatility, bool)
        or not isinstance(min_daily_excess_volatility, (int, float))
        or not math.isfinite(min_daily_excess_volatility)
        or min_daily_excess_volatility <= 0
    ):
        raise ValueError("Minimum volatility floor must be a positive number.")

    _validate_prices(adjusted_closes)
    _validate_prices(parking_adjusted_closes)

    asset_returns = _daily_returns(adjusted_closes)
    parking_returns = _daily_returns(parking_adjusted_closes)

    weighted_scores = []
    for lookback, weight in _LOOKBACK_WEIGHTS:
        asset_window = asset_returns[-lookback:]
        parking_window = parking_returns[-lookback:]
        compounded_excess = _compounded_excess_return(asset_window, parking_window)
        excess_window = [
            asset_return - parking_return
            for asset_return, parking_return in zip(asset_window, parking_window)
        ]
        mean_return = math.fsum(excess_window) / lookback
        variance = math.fsum(
            (daily_return - mean_return) ** 2 for daily_return in excess_window
        ) / (lookback - 1)
        volatility = math.sqrt(variance)

        # v3.7.4: 하한은 일간 σ에 적용한 뒤 √k 를 곱합니다(저변동 자산 점수 폭주 방지).
        score = compounded_excess / (
            max(volatility, min_daily_excess_volatility) * math.sqrt(lookback)
        )
        if not math.isfinite(score):
            raise ValueError("Momentum score must be finite.")
        weighted_scores.append(weight * score)

    final_score = math.fsum(weighted_scores)
    if not math.isfinite(final_score):
        raise ValueError("Momentum score must be finite.")
    return final_score


def is_above_sma120(
    current_price: float,
    completed_adjusted_closes: Sequence[float],
) -> bool:
    """현재 수정 기준 가격이 최근 완료 수정 종가의 120일 평균보다 높은지 봅니다."""
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
    """양수 모멘텀과 120일 이동평균 상회 조건을 함께 적용합니다."""
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
    """점수·ER120·거래대금·티커 순으로 후보를 결정론적으로 정렬합니다."""
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
        if (
            isinstance(candidate.er120, bool)
            or not isinstance(candidate.er120, (int, float))
            or not math.isfinite(candidate.er120)
        ):
            raise ValueError("Candidate ER120 must be finite.")
        if (
            isinstance(candidate.avg_turnover_20d, bool)
            or not isinstance(candidate.avg_turnover_20d, (int, float))
            or not math.isfinite(candidate.avg_turnover_20d)
            or candidate.avg_turnover_20d < 0
        ):
            raise ValueError("Candidate average turnover must be non-negative.")
        if (
            isinstance(candidate.sma_gap_ratio, bool)
            or not isinstance(candidate.sma_gap_ratio, (int, float))
            or not math.isfinite(candidate.sma_gap_ratio)
        ):
            raise ValueError("Candidate SMA gap ratio must be finite.")
        if not isinstance(candidate.ticker, str) or not candidate.ticker:
            raise ValueError("Candidate ticker must be a non-empty string.")
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


def assess_risk_candidate(
    profile: CandidateProfile,
    *,
    min_history_sessions: int = _MIN_HISTORY_SESSIONS,
    min_avg_turnover_20d: float = 5_000_000_000,
    max_order_participation_rate: float = 0.01,
    min_daily_excess_volatility: float = _MIN_VOLATILITY,
) -> CandidateAssessment:
    """명세서 순서대로 6단계 필터를 적용하고 신규 매수 가능 여부를 반환합니다."""
    if not isinstance(profile, CandidateProfile):
        raise TypeError("profile must be a CandidateProfile.")
    if (
        isinstance(min_history_sessions, bool)
        or not isinstance(min_history_sessions, int)
        or min_history_sessions < _MIN_HISTORY_SESSIONS
    ):
        raise ValueError(
            "Minimum history sessions must be an integer of at least 200."
        )
    for name, value in (
        ("min_avg_turnover_20d", min_avg_turnover_20d),
        ("max_order_participation_rate", max_order_participation_rate),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a positive number.")
    if max_order_participation_rate > 1:
        raise ValueError("Order participation rate cannot exceed 100%.")
    if (
        isinstance(profile.estimated_order_amount, bool)
        or not isinstance(profile.estimated_order_amount, int)
        or profile.estimated_order_amount < 0
    ):
        raise ValueError("Estimated order amount must be a non-negative integer.")
    if (
        not isinstance(profile.ticker, str)
        or not profile.ticker
        or not isinstance(profile.currently_held, bool)
        or not isinstance(profile.blocked_until_next_rotation, bool)
        or not isinstance(profile.classification_verified, bool)
    ):
        raise ValueError("Candidate identity and state flags are invalid.")
    if (
        isinstance(profile.leverage_multiplier, bool)
        or not isinstance(profile.leverage_multiplier, (int, float))
        or not math.isfinite(profile.leverage_multiplier)
    ):
        raise ValueError("Candidate leverage multiplier must be finite.")
    if (
        isinstance(profile.avg_turnover_20d, bool)
        or not isinstance(profile.avg_turnover_20d, (int, float))
        or not math.isfinite(profile.avg_turnover_20d)
        or profile.avg_turnover_20d < 0
    ):
        raise ValueError("Average turnover must be a non-negative finite number.")

    def rejected(filter_number: int, code: str) -> CandidateAssessment:
        """필터 탈락 정보를 일정한 결과 형태로 만듭니다."""
        return CandidateAssessment(None, False, filter_number, code)

    # 1. 상품·상장 상태와 가격 데이터 상태를 우선 확인합니다.
    if profile.status != "ACTIVE":
        return rejected(1, "INACTIVE")
    if profile.data_status != "VALID":
        return rejected(1, "DATA_INVALID")
    if not profile.classification_verified:
        return rejected(1, "UNVERIFIED_CLASSIFICATION")
    if (
        profile.category != "RISK_ASSET"
        or profile.product_type != "SPOT_ETF"
        or profile.leverage_multiplier != 1.0
        or profile.replication_method != "PHYSICAL"
        or any(
            not isinstance(flag, bool) or flag
            for flag in (
                profile.is_leveraged,
                profile.is_inverse,
                profile.is_futures,
                profile.is_etn,
            )
        )
    ):
        return rejected(1, "INELIGIBLE_PRODUCT")

    # 2. 최근 200일 가격 데이터가 없으면 점수를 계산하지 않습니다.
    if len(profile.adjusted_closes) < min_history_sessions:
        return rejected(2, "INSUFFICIENT_HISTORY")
    validate_daily_history(profile.adjusted_closes)
    if len(profile.parking_adjusted_closes) < min_history_sessions:
        return rejected(2, "INSUFFICIENT_PARKING_HISTORY")
    validate_daily_history(profile.parking_adjusted_closes)

    score = calculate_momentum_score(
        profile.adjusted_closes,
        profile.parking_adjusted_closes,
        min_daily_excess_volatility=min_daily_excess_volatility,
    )

    # 3~4. 절대 모멘텀과 완료된 수정 종가 기준 SMA120을 확인합니다.
    if score <= 0:
        return rejected(3, "NON_POSITIVE_MOMENTUM")
    if not is_above_sma120(
        profile.adjusted_closes[-1],
        profile.adjusted_closes,
    ):
        return rejected(4, "BELOW_SMA120")

    asset_returns = _daily_returns(profile.adjusted_closes)
    parking_returns = _daily_returns(profile.parking_adjusted_closes)
    er120 = _compounded_excess_return(asset_returns[-120:], parking_returns[-120:])
    average_close = math.fsum(profile.adjusted_closes[-120:]) / 120
    candidate = RankedCandidate(
        ticker=profile.ticker,
        score=score,
        sma_gap_ratio=profile.adjusted_closes[-1] / average_close - 1,
        er120=er120,
        avg_turnover_20d=profile.avg_turnover_20d,
    )

    # 5. 보유 종목은 유동성 저하만으로 순위에서 제거해 강제 매도하지 않습니다.
    within_turnover_limit = (
        profile.avg_turnover_20d >= min_avg_turnover_20d
        and profile.estimated_order_amount
        <= profile.avg_turnover_20d * max_order_participation_rate
    )
    if not within_turnover_limit and not profile.currently_held:
        return rejected(5, "LOW_LIQUIDITY")

    # 6. 손절 후 회전 전까지 재진입을 막습니다.
    if profile.blocked_until_next_rotation:
        return rejected(6, "BLOCKED_UNTIL_NEXT_ROTATION")

    return CandidateAssessment(
        candidate=candidate,
        eligible_for_new_entry=within_turnover_limit,
        failed_filter=5 if not within_turnover_limit else None,
        rejection_code="LOW_LIQUIDITY" if not within_turnover_limit else None,
    )
