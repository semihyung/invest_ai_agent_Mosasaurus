from collections.abc import Sequence


def walk_forward_windows(
    trading_dates: Sequence[str],
    *,
    training_sessions: int = 756,
    test_sessions: int = 252,
) -> list[tuple[Sequence[str], Sequence[str]]]:
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
