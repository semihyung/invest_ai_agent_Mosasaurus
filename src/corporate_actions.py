from dataclasses import dataclass
from datetime import date
import math


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
            or not math.isfinite(self.adjustment_factor)
            or self.adjustment_factor <= 0
        ):
            raise ValueError("Adjustment factor must be a finite positive number.")
        if not isinstance(self.verified, bool):
            raise ValueError("verified must be a boolean.")
