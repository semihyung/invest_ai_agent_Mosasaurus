"""Walk-Forward / Out-of-Sample 평가(명세 11.1절)."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass


def walk_forward_windows(
    trading_dates: Sequence[str],
    *,
    training_sessions: int = 756,
    test_sessions: int = 252,
) -> list[tuple[Sequence[str], Sequence[str]]]:
    """학습 구간 뒤에 검증 구간이 이어지도록 겹치지 않는 창을 만듭니다."""
    if training_sessions <= 0 or test_sessions <= 0:
        raise ValueError("Walk-forward window sizes must be positive.")
    if len(trading_dates) < training_sessions + test_sessions:
        raise ValueError("Insufficient trading dates for walk-forward.")

    windows = []
    start = 0
    while start + training_sessions + test_sessions <= len(trading_dates):
        windows.append(
            (
                trading_dates[start:start + training_sessions],
                trading_dates[start + training_sessions:start + training_sessions + test_sessions],
            )
        )
        start += test_sessions
    return windows


@dataclass(frozen=True)
class WalkForwardWindowResult:
    """한 창의 선택 파라미터와 학습·검증 성과입니다."""

    train_range: tuple[int, int]
    test_range: tuple[int, int]
    chosen_params: Mapping[str, float]
    train_score: float
    test_cagr: float
    test_mdd: float
    test_equity: tuple[float, ...]


def score_run(cagr: float, mdd: float) -> float:
    """파라미터 선택 점수: 낙폭 대비 수익률(분모 하한 1%)입니다."""
    return cagr / max(abs(mdd), 0.01)


def walk_forward_evaluate(
    run: Callable[[Mapping[str, float], int, int], tuple[float, float, Sequence[float]]],
    param_grid: Sequence[Mapping[str, float]],
    *,
    history_sessions: int,
    total_sessions: int,
    training_sessions: int,
    test_sessions: int,
) -> list[WalkForwardWindowResult]:
    """각 창에서 학습 구간 데이터만으로 파라미터를 고르고 검증 구간에서 성과를 측정합니다.

    `run(params, start_index, end_index)` 는 (cagr, mdd, 자산곡선) 을 돌려주는 콜백이며,
    `start_index` 앞의 `history_sessions` 개 일봉은 신호 계산용 이력으로만 쓰입니다.
    파라미터 선택은 해당 창의 학습 구간 결과만 사용하므로 미래 데이터가 유입되지 않습니다.
    """
    if not param_grid:
        raise ValueError("param_grid must not be empty.")
    results = []
    first = history_sessions
    while first + training_sessions + test_sessions <= total_sessions:
        train = (first, first + training_sessions)
        test = (train[1], train[1] + test_sessions)
        scored = []
        for params in param_grid:
            cagr, mdd, _ = run(params, *train)
            scored.append((score_run(cagr, mdd), dict(params)))
        # 동점이면 파라미터 사전순으로 결정론적으로 고릅니다.
        scored.sort(key=lambda item: (-item[0], sorted(item[1].items())))
        best_score, best_params = scored[0]
        cagr, mdd, curve = run(best_params, *test)
        results.append(
            WalkForwardWindowResult(
                train_range=train,
                test_range=test,
                chosen_params=best_params,
                train_score=best_score,
                test_cagr=cagr,
                test_mdd=mdd,
                test_equity=tuple(curve),
            )
        )
        first += test_sessions
    if not results:
        raise ValueError("Insufficient sessions for even one walk-forward window.")
    return results
