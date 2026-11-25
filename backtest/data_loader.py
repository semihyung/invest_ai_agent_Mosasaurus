from pathlib import Path
import csv


def load_market_csv(path: str | Path):
    """시가·저가·종가·거래량·거래대금이 있는 일봉 CSV 를 `backtest.engine.Bar` 로 읽습니다.

    필수 열: trade_date, open, low, adjusted_close, volume, turnover_krw. 거래일은 오름차순·
    중복 없음이어야 하며 가격은 모두 양수여야 합니다.
    """
    from backtest.engine import Bar

    bars = []
    previous = ""
    for row in csv.DictReader(Path(path).read_text(encoding="utf-8").splitlines()):
        bar = Bar(
            trade_date=row["trade_date"],
            open=float(row["open"]),
            low=float(row["low"]),
            close=float(row["adjusted_close"]),
            volume=int(row["volume"]),
            turnover_krw=float(row["turnover_krw"]),
        )
        if bar.trade_date <= previous:
            raise ValueError("Trade dates must be strictly increasing.")
        if min(bar.open, bar.low, bar.close) <= 0 or bar.volume < 0 or bar.turnover_krw < 0:
            raise ValueError("Prices must be positive and volume/turnover non-negative.")
        if bar.low > min(bar.open, bar.close) * (1 + 1e-9):
            raise ValueError("Low must not exceed the open or close.")
        previous = bar.trade_date
        bars.append(bar)
    if len(bars) < 200:
        raise ValueError("Backtest requires at least 200 daily bars.")
    return bars
