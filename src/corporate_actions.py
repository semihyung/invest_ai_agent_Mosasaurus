from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class CorporateAction:
    ticker: str
    action_type: str
    ex_date: date
    adjustment_factor: float = 1.0
    verified: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.ticker, str) or not self.ticker.strip():
            raise ValueError("Ticker must be a non-empty string.")
        if not isinstance(self.action_type, str) or not self.action_type.strip():
            raise ValueError("Action type must be a non-empty string.")
        if not isinstance(self.ex_date, date):
            raise ValueError("ex_date must be a date.")
        if (
            isinstance(self.adjustment_factor, bool)
            or not isinstance(self.adjustment_factor, (int, float))
            or not 0 < float(self.adjustment_factor) <= 1
        ):
            raise ValueError("Adjustment factor must be in the range (0, 1].")


def apply_adjustment_once(
    adjusted_price: float,
    action: CorporateAction,
) -> float:
    if isinstance(adjusted_price, bool) or adjusted_price <= 0:
        raise ValueError("Adjusted price must be positive.")
    if not isinstance(action, CorporateAction):
        raise TypeError("action must be a CorporateAction.")
    if not action.verified:
        raise ValueError("Corporate action must be verified before adjustment.")
    return adjusted_price * action.adjustment_factor


def adjustment_factor_for_verified_actions(actions: list[CorporateAction]) -> float:
    factor = 1.0
    for action in actions:
        if not isinstance(action, CorporateAction):
            raise TypeError("actions must contain only CorporateAction values.")
        if action.verified:
            factor *= action.adjustment_factor
    return factor
