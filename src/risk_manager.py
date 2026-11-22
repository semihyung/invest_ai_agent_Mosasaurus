from dataclasses import dataclass
from enum import Enum


class DrawdownMode(str, Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class OperatingMode(str, Enum):
    SAFE_HALT = "SAFE_HALT"
    EMERGENCY = "EMERGENCY"
    PAUSED = "PAUSED"
    TRAINING = "TRAINING"
    NORMAL = "NORMAL"


@dataclass(frozen=True)
class RiskFlags:
    is_safe_halt: bool = False
    emergency_mode: bool = False
    is_paused: bool = False
    training_mode: bool = False


def trailing_stop_ratio(training_mode: bool = False) -> float:
    return -0.07 if training_mode else -0.10


def should_trailing_sell(
    current_price: float,
    peak_price: float,
    *,
    training_mode: bool = False,
) -> bool:
    if (
        isinstance(current_price, bool)
        or isinstance(peak_price, bool)
        or current_price <= 0
        or peak_price <= 0
    ):
        raise ValueError("Prices must be positive numbers.")
    return current_price / peak_price - 1 <= trailing_stop_ratio(training_mode)


def calculate_drawdown(current_equity: float, hwm_equity: float) -> float:
    if (
        isinstance(current_equity, bool)
        or isinstance(hwm_equity, bool)
        or current_equity < 0
        or hwm_equity <= 0
    ):
        raise ValueError("Equity values must be valid non-negative numbers.")
    return current_equity / hwm_equity - 1


def get_drawdown_mode(drawdown: float) -> DrawdownMode:
    if not isinstance(drawdown, (int, float)) or isinstance(drawdown, bool):
        raise ValueError("Drawdown must be numeric.")
    if drawdown <= -0.15:
        return DrawdownMode.CRITICAL
    if drawdown <= -0.10:
        return DrawdownMode.WARNING
    if drawdown <= -0.07:
        return DrawdownMode.CAUTION
    return DrawdownMode.NORMAL


def resolve_operating_mode(flags: RiskFlags) -> OperatingMode:
    if not isinstance(flags, RiskFlags):
        raise TypeError("flags must be a RiskFlags value.")
    if flags.is_safe_halt:
        return OperatingMode.SAFE_HALT
    if flags.emergency_mode:
        return OperatingMode.EMERGENCY
    if flags.is_paused:
        return OperatingMode.PAUSED
    if flags.training_mode:
        return OperatingMode.TRAINING
    return OperatingMode.NORMAL


def allows_risk_asset_buy(flags: RiskFlags) -> bool:
    mode = resolve_operating_mode(flags)
    return mode is OperatingMode.NORMAL
