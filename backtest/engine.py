"""운영 코드와 같은 함수를 호출하는 전략 백테스트 엔진(명세 11.1절).

백테스트 전용으로 순위·필터·슬롯 배정·낙폭 규칙을 다시 만들지 않고, `src.quant_engine`
(모멘텀·필터·정렬)과 `src.portfolio_manager.select_final_slots`(순위 버퍼·집중도)를 그대로
호출합니다. 따라서 운영 규칙이 바뀌면 백테스트 결과도 함께 바뀝니다.

체결 가정(보수적 근사, 보고서에 그대로 기재):
- 신호는 직전 거래일까지 완료된 수정 종가로만 계산하고(미래 참조 금지), 주문은 주간 첫 개장일
  15:00 에 `당일 종가 × (1 ± 슬리피지)` 로 체결된다고 근사한다(분봉 데이터가 없을 때).
- 트레일링 스탑의 고점은 6.1절대로 "보유 시작 이후 완료 종가 최고치"이며, 장중 트리거는 일봉
  저가가 스탑 가격 이하인 날 `min(시가, 스탑 가격)` 에 체결(`INTRADAY`)된 것으로 본다.
  15:10 이후 도달분을 반영하는 `NEXT_OPEN` 시나리오(다음 날 시가 체결)도 함께 평가할 수 있다.
- 가격은 수정주가로 통일한다(원주가·분배락 보정 데이터가 없으므로).
"""

import math
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Callable, Mapping, Sequence

from backtest.run_backtest import calculate_cagr, calculate_mdd, calculate_turnover
from src.portfolio_manager import SlotCandidate, select_final_slots
from src.quant_engine import (
    CandidateProfile,
    assess_risk_candidate,
    rank_candidates,
)


@dataclass(frozen=True)
class Bar:
    """하루치 일봉(수정 기준 가격)입니다."""

    trade_date: str
    open: float
    low: float
    close: float
    volume: int
    turnover_krw: float


@dataclass(frozen=True)
class AssetSpec:
    """유니버스 종목의 정적 속성입니다(시점별 편입 기간 포함)."""

    ticker: str
    region: str
    index_family: str
    listed_from: str = "0000-00-00"      # 생존편향 통제: 이 날짜 이전에는 후보가 아님
    delisted_after: str = "9999-99-99"   # 상장폐지 후에는 후보가 아니며 보유분은 마지막 종가로 청산


@dataclass(frozen=True)
class EngineConfig:
    """rules.yaml 의 전략·체결 파라미터 중 백테스트에 쓰는 값입니다."""

    fee_rate: float = 0.00015
    slippage_ratio: float = 0.002
    stop_normal: float = -0.10
    stop_execution: str = "INTRADAY"           # INTRADAY | NEXT_OPEN
    cash_buffer_ratio: float = 0.05
    slot_weights: tuple[float, float, float] = (0.45, 0.30, 0.20)
    min_trade_amount_krw: int = 150_000
    rebalance_drift_ratio: float = 0.05
    hold_rank_buffer: int = 5
    min_daily_excess_volatility: float = 0.005
    min_avg_turnover_20d_krw: float = 5_000_000_000
    max_order_participation_rate: float = 0.01
    max_pairwise_correlation: float = 0.90


@dataclass
class _Position:
    shares: int
    peak: float
    entry_week: str


@dataclass(frozen=True)
class BacktestRun:
    """백테스트 결과: 자산 곡선과 비용·회전율·매매 통계입니다."""

    dates: tuple[str, ...]
    equity: tuple[float, ...]
    trades: int
    traded_notional: float
    fees_paid: float
    slippage_cost: float
    stops: int
    rotations: int
    log: tuple[str, ...] = field(default=())
    final_positions: tuple[tuple[str, int], ...] = field(default=())

    @property
    def cagr(self) -> float:
        """거래일 252일 기준 연복리 수익률입니다."""
        years = max(len(self.equity) - 1, 1) / 252
        return calculate_cagr(self.equity[0], self.equity[-1], years)

    @property
    def mdd(self) -> float:
        """최대 낙폭(음수)입니다."""
        return calculate_mdd(self.equity)

    @property
    def turnover(self) -> float:
        """평균 자산 대비 매매대금 비율의 연환산 값입니다."""
        years = max(len(self.equity) - 1, 1) / 252
        return calculate_turnover(self.traded_notional, sum(self.equity) / len(self.equity)) / years


def _iso_week(trade_date: str) -> str:
    """거래일의 ISO 주 식별자(예: 2026-W48)입니다."""
    year, week, _ = date.fromisoformat(trade_date).isocalendar()
    return f"{year}-W{week:02d}"


def _sma(values: Sequence[float], length: int) -> float:
    """마지막 `length` 개 값의 단순평균입니다."""
    return math.fsum(values[-length:]) / length


def pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    """피어슨 상관계수이며 분산이 0이면 None 입니다(NaN 대신 계산 불가 신호)."""
    count = min(len(left), len(right))
    if count < 3:
        return None
    a, b = left[-count:], right[-count:]
    mean_a, mean_b = math.fsum(a) / count, math.fsum(b) / count
    cov = math.fsum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = math.fsum((x - mean_a) ** 2 for x in a)
    var_b = math.fsum((y - mean_b) ** 2 for y in b)
    if var_a <= 0 or var_b <= 0:
        return None
    return cov / math.sqrt(var_a * var_b)


def _returns(closes: Sequence[float]) -> list[float]:
    """종가 시계열의 일간 수익률입니다."""
    return [c / p - 1 for p, c in zip(closes, closes[1:])]


def run_strategy_backtest(
    assets: Sequence[AssetSpec],
    bars: Mapping[str, Sequence[Bar]],
    parking_ticker: str,
    config: EngineConfig = EngineConfig(),
    *,
    starting_cash: float = 10_000_000,
    start_index: int = 200,
    end_index: int | None = None,
) -> BacktestRun:
    """공통 거래일 달력에서 주간 교체·일일 120MA 퇴출·스탑·파킹을 시뮬레이션합니다.

    모든 종목(파킹 포함)의 `bars` 는 같은 거래일 집합이어야 합니다. `start_index` 부터
    운용하며(그 이전 200거래일은 신호 계산용 이력), `end_index` 는 포함되지 않습니다.
    """
    if parking_ticker not in bars:
        raise ValueError("Parking bars are required.")
    dates = [bar.trade_date for bar in bars[parking_ticker]]
    for ticker, series in bars.items():
        if [bar.trade_date for bar in series] != dates:
            raise ValueError(f"{ticker} does not share the common trading calendar.")
    last = len(dates) if end_index is None else min(end_index, len(dates))
    if start_index < 200 or last - start_index < 2:
        raise ValueError("Need 200 sessions of history and at least two simulated sessions.")
    if config.stop_execution not in ("INTRADAY", "NEXT_OPEN"):
        raise ValueError("stop_execution must be INTRADAY or NEXT_OPEN.")

    spec_by_ticker = {asset.ticker: asset for asset in assets}
    cash = float(starting_cash)
    positions: dict[str, _Position] = {}
    blocked_week: dict[str, str] = {}
    pending_stop: set[str] = set()
    equity_curve: list[float] = []
    log: list[str] = []
    counters = {"trades": 0, "notional": 0.0, "fees": 0.0, "slip": 0.0, "stops": 0, "rotations": 0}
    rotated_weeks: set[str] = set()

    def trade(ticker: str, shares: int, price: float, *, buy: bool) -> None:
        """슬리피지·수수료를 반영해 체결하고 현금·포지션을 갱신합니다."""
        nonlocal cash
        fill = price * (1 + config.slippage_ratio) if buy else price * (1 - config.slippage_ratio)
        notional = shares * fill
        fee = notional * config.fee_rate
        counters["trades"] += 1
        counters["notional"] += notional
        counters["fees"] += fee
        counters["slip"] += shares * abs(fill - price)
        cash += -(notional + fee) if buy else (notional - fee)

    def affordable(price: float, budget: float) -> int:
        """수수료·슬리피지를 포함해 예산 안에서 살 수 있는 최대 주수입니다."""
        unit = price * (1 + config.slippage_ratio) * (1 + config.fee_rate)
        return max(0, int(budget // unit))

    for index in range(start_index, last):
        today = dates[index]
        week = _iso_week(today)

        # 1) 상장폐지: 마지막 종가로 청산(생존편향 통제).
        for ticker in list(positions):
            if today > spec_by_ticker.get(ticker, AssetSpec(ticker, "", "")).delisted_after:
                trade(ticker, positions[ticker].shares, bars[ticker][index - 1].close, buy=False)
                del positions[ticker]
                log.append(f"{today} DELIST {ticker}")

        # 2) 전일 예약된 NEXT_OPEN 스탑 체결.
        for ticker in sorted(pending_stop):
            if ticker in positions:
                trade(ticker, positions[ticker].shares, bars[ticker][index].open, buy=False)
                del positions[ticker]
                counters["stops"] += 1
                blocked_week[ticker] = week
                log.append(f"{today} STOP_NEXT_OPEN {ticker}")
        pending_stop.clear()

        # 3) 트레일링 스탑: 고점 = 보유 시작 이후 완료 종가 최고치(어제까지), 트리거 = 오늘 저가.
        for ticker in sorted(positions):
            position = positions[ticker]
            stop_price = position.peak * (1 + config.stop_normal)
            bar = bars[ticker][index]
            if bar.low <= stop_price:
                if config.stop_execution == "INTRADAY":
                    trade(ticker, position.shares, min(bar.open, stop_price), buy=False)
                    del positions[ticker]
                    counters["stops"] += 1
                    blocked_week[ticker] = week
                    log.append(f"{today} STOP {ticker}")
                else:
                    pending_stop.add(ticker)

        # 4) 15:00 파이프라인: 일일 120MA 퇴출 + (주간 첫 개장일) 교체·리밸런싱 + 파킹.
        is_first_open_day = week not in rotated_weeks
        history = {t: [b.close for b in series[:index]] for t, series in bars.items()}
        for ticker in sorted(positions):
            if ticker == parking_ticker:
                continue
            if bars[ticker][index].close <= _sma(history[ticker], 120):
                trade(ticker, positions[ticker].shares, bars[ticker][index].close, buy=False)
                del positions[ticker]
                log.append(f"{today} MA120_EXIT {ticker}")

        risk_value = sum(
            p.shares * bars[t][index].close for t, p in positions.items()
        )
        total_value = cash + risk_value

        if is_first_open_day:
            rotated_weeks.add(week)
            counters["rotations"] += 1
            candidates = []
            held_tickers = [t for t in positions if t != parking_ticker]
            for asset in assets:
                ticker = asset.ticker
                if ticker == parking_ticker or ticker not in bars:
                    continue
                if not asset.listed_from <= today <= asset.delisted_after:
                    continue
                series = bars[ticker]
                turnovers = [b.turnover_krw for b in series[index - 20:index]]
                est_amount = int(total_value * config.slot_weights[0])
                profile = CandidateProfile(
                    ticker=ticker, category="RISK_ASSET", status="ACTIVE", data_status="VALID",
                    product_type="SPOT_ETF", is_leveraged=False, is_inverse=False,
                    is_futures=False, is_etn=False,
                    adjusted_closes=history[ticker][-200:],
                    parking_adjusted_closes=history[parking_ticker][-200:],
                    avg_turnover_20d=math.fsum(turnovers) / 20,
                    estimated_order_amount=est_amount,
                    currently_held=ticker in positions,
                    blocked_until_next_rotation=blocked_week.get(ticker, "") >= week and ticker in blocked_week,
                    classification_verified=True,
                )
                try:
                    assessment = assess_risk_candidate(
                        profile,
                        min_avg_turnover_20d=config.min_avg_turnover_20d_krw,
                        max_order_participation_rate=config.max_order_participation_rate,
                        min_daily_excess_volatility=config.min_daily_excess_volatility,
                    )
                except ValueError:
                    continue  # 데이터 품질 불량(±30% 이상치 등)은 후보 제외
                if assessment.candidate is None:
                    continue
                if not assessment.eligible_for_new_entry and ticker not in positions:
                    continue
                # 진입 게이트: 실시간(당일 종가 근사) 가격도 120MA 위여야 합니다.
                if bars[ticker][index].close <= _sma(history[ticker], 120):
                    continue
                candidates.append(assessment.candidate)
            ranked = rank_candidates(candidates)
            slot_candidates = [
                SlotCandidate(c.ticker, spec_by_ticker[c.ticker].region,
                              spec_by_ticker[c.ticker].index_family,
                              spec_by_ticker[c.ticker].index_family)
                for c in ranked
            ]

            def correlation(a: str, b: str) -> float | None:
                """최근 60일 일간 수익률 상관계수(분산 0 이면 None)."""
                return pearson(_returns(history[a][-61:]), _returns(history[b][-61:]))

            slots = select_final_slots(
                slot_candidates, held_tickers=held_tickers,
                hold_rank_buffer=config.hold_rank_buffer,
                max_pairwise_correlation=config.max_pairwise_correlation,
                correlation=correlation,
                slot_weights=dict(zip(("S1", "S2", "S3"), config.slot_weights)),
            )
            targets = {s.ticker: s.target_weight for s in slots if s.ticker}
            investable = total_value * (1 - config.cash_buffer_ratio)
            parking_weight = (1 - config.cash_buffer_ratio) - sum(targets.values())

            # 교체 매도: 최종 슬롯에 없는 보유 위험자산 전량 매도(파킹은 아래에서 목표 조정).
            for ticker in sorted(held_tickers):
                if ticker not in targets:
                    trade(ticker, positions[ticker].shares, bars[ticker][index].close, buy=False)
                    del positions[ticker]
                    log.append(f"{today} ROTATE_OUT {ticker}")
            # 파킹 현금화(필요 시): 위험자산 매수 재원이 모자라면 파킹을 먼저 판다.
            risk_value = sum(p.shares * bars[t][index].close for t, p in positions.items() if t != parking_ticker)
            parking_shares = positions[parking_ticker].shares if parking_ticker in positions else 0
            needed = sum(
                max(0.0, total_value * w - (positions[t].shares * bars[t][index].close if t in positions else 0.0))
                for t, w in targets.items()
            )
            if needed > cash * (1 - config.cash_buffer_ratio) and parking_shares:
                price = bars[parking_ticker][index].close
                shortfall = needed - cash * (1 - config.cash_buffer_ratio)
                sell_shares = min(parking_shares, math.ceil(shortfall / (price * (1 - config.slippage_ratio))))
                if sell_shares * price >= config.min_trade_amount_krw:
                    trade(parking_ticker, sell_shares, price, buy=False)
                    positions[parking_ticker].shares -= sell_shares
                    if positions[parking_ticker].shares == 0:
                        del positions[parking_ticker]
            # 매수(슬롯 순서) + 유지 종목 Drift 리밸런싱.
            total_value = cash + sum(p.shares * bars[t][index].close for t, p in positions.items())
            for slot in slots:
                ticker = slot.ticker
                if not ticker:
                    continue
                price = bars[ticker][index].close
                held_value = positions[ticker].shares * price if ticker in positions else 0.0
                target_value = total_value * slot.target_weight
                gap = target_value - held_value
                if abs(gap) < config.min_trade_amount_krw:
                    continue
                if ticker in positions and abs(gap) / total_value < config.rebalance_drift_ratio:
                    continue
                if gap > 0:
                    budget = min(gap, cash - config.cash_buffer_ratio * total_value)
                    quantity = affordable(price, budget)
                    if quantity > 0 and quantity * price >= config.min_trade_amount_krw:
                        trade(ticker, quantity, price, buy=True)
                        if ticker in positions:
                            positions[ticker].shares += quantity
                        else:
                            positions[ticker] = _Position(
                                quantity, price * (1 + config.slippage_ratio), week)
                        log.append(f"{today} BUY {ticker} {quantity}")
                else:
                    quantity = min(positions[ticker].shares, int(-gap // price))
                    if quantity > 0:
                        trade(ticker, quantity, price, buy=False)
                        positions[ticker].shares -= quantity
                        log.append(f"{today} TRIM {ticker} {quantity}")
            _ = parking_weight  # 파킹 목표 비중은 아래 공통 파킹 단계에서 현금 잔여분으로 채워집니다.

        # 5) 파킹: 5% 현금 버퍼를 넘는 현금은 대표 파킹에만 배정(최소 주문 금액 이상일 때).
        total_value = cash + sum(p.shares * bars[t][index].close for t, p in positions.items())
        excess = cash - config.cash_buffer_ratio * total_value
        parking_price = bars[parking_ticker][index].close
        if excess >= config.min_trade_amount_krw:
            quantity = affordable(parking_price, excess)
            if quantity > 0:
                trade(parking_ticker, quantity, parking_price, buy=True)
                if parking_ticker in positions:
                    positions[parking_ticker].shares += quantity
                else:
                    positions[parking_ticker] = _Position(
                        quantity, parking_price * (1 + config.slippage_ratio), week)

        # 6) 마감: 고점 갱신(완료 종가 기준) 후 자산 기록.
        for ticker, position in positions.items():
            position.peak = max(position.peak, bars[ticker][index].close)
        equity_curve.append(
            cash + sum(p.shares * bars[t][index].close for t, p in positions.items())
        )

    return BacktestRun(
        dates=tuple(dates[start_index:last]),
        equity=tuple(equity_curve),
        trades=counters["trades"],
        traded_notional=counters["notional"],
        fees_paid=counters["fees"],
        slippage_cost=counters["slip"],
        stops=counters["stops"],
        rotations=counters["rotations"],
        log=tuple(log),
        final_positions=tuple(sorted((t, p.shares) for t, p in positions.items())),
    )


def with_params(config: EngineConfig, **changes: float) -> EngineConfig:
    """EngineConfig 의 일부 값만 바꾼 복사본을 만듭니다(민감도 실험용)."""
    return replace(config, **changes)


def parking_proxy_closes(
    trade_dates: Sequence[str],
    annual_rate: float,
    *,
    start_value: float = 100.0,
) -> list[float]:
    """상장 이전 구간의 무위험 프록시(CD/KOFR 연율을 일할 계산한 지수)를 만듭니다."""
    if annual_rate <= -1:
        raise ValueError("annual_rate must be greater than -100%.")
    daily = (1 + annual_rate) ** (1 / 252) - 1
    return [start_value * (1 + daily) ** i for i in range(len(trade_dates))]


Runner = Callable[[EngineConfig, int, int], BacktestRun]
