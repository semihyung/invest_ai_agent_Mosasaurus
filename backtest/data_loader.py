from dataclasses import dataclass
from pathlib import Path
import csv


@dataclass(frozen=True)
class BacktestBar:
    trade_date: str
    close: float
    volume: int


def load_csv_bars(path: str | Path) -> list[BacktestBar]:
    bars = []
    for row in csv.DictReader(Path(path).read_text(encoding="utf-8").splitlines()):
        close = float(row["adjusted_close"])
        volume = int(row["volume"])
        if close <= 0 or volume < 0:
            raise ValueError("Backtest close must be positive and volume non-negative.")
        bars.append(
            BacktestBar(
                trade_date=row["trade_date"],
                close=close,
                volume=volume,
            )
        )
    if len(bars) < 200:
        raise ValueError("Backtest requires at least 200 daily bars.")
    return bars
