"""`daily_pipeline`(개장일 15:00): 보유 유지 판정 → 매도 선행 → 매수(명세 5.3 Step B·5.4절).

매 개장일: 120MA 이탈 퇴출과 대표 파킹 배정만. **주간 첫 개장일**(미소비 프리뷰가 있는 날)에만 순위 버퍼 슬롯 확정,
교체 매도, 위험자산 매수를 수행합니다. 모든 주문은 `order_executor.submit_order` 한 곳을 지납니다.

의도적으로 구현하지 않은 것(명세에 있으나 별도 단계): 리밸런싱 초과분 매도(Drift 매도), 파킹 현금화 매도,
상관계수 집중도 검사(상관 함수 미주입 시 건너뜀), 5.2절 필터 전체의 15:00 재평가(120MA·검증·격리·데이터 상태만 재확인).
`buyable_cash`(`nrcvb_buy_amt`)·iNAV 가 없으면 매수는 fail-closed 로 막힙니다.
"""

import json
import math
import sqlite3
import time as _time
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Mapping, Protocol

from src.job_handlers import AccountBrokerPort, _log, _mark_stale, _safe_notify
from src.order_executor import KST, SellRequest, submit_order
from src.order_manager import OrderSide, calculate_limit_price
from src.order_policy import OrderKind
from src.order_sync import sync_orders
from src.portfolio_manager import SlotCandidate, select_final_slots
from src.risk_manager import should_trailing_sell
from src.safety_flags import has_buy_block, is_safe_halt, load_risk_flags, ticker_is_blocked

_ACTIVE_SELL = ("CREATED", "SUBMITTING", "PENDING_CONFIRMATION", "ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING")


class PipelineBroker(AccountBrokerPort, Protocol):
    """파이프라인 추가 조회: 매수가능금액(`nrcvb_buy_amt`)."""

    def buyable_cash(self) -> int: ...


@dataclass(frozen=True)
class PipelineConfig:
    """`rules.yaml` 에서 읽은 파이프라인 파라미터."""

    parking_ticker: str
    slot_weights: Mapping[str, float]
    cash_buffer_ratio: float
    min_trade_amount: int
    rebalance_drift: float
    hold_rank_buffer: int
    max_slots_per_region: Mapping[str, int]
    max_region_weight: Mapping[str, float]
    max_pairwise_correlation: float
    fee_rate: float
    sell_first_deadline: time
    new_order_cutoff: time

    @classmethod
    def from_rules(cls, rules: Mapping) -> "PipelineConfig":
        p, e = rules["portfolio"], rules["execution"]
        hms = lambda text: time.fromisoformat(text)
        return cls(
            p["primary_parking_ticker"], p["slot_weights"], float(p["cash_buffer_ratio"]),
            int(p["min_trade_amount_krw"]), float(p["rebalance_drift_ratio"]), int(p["hold_rank_buffer"]),
            p["concentration"]["max_slots_per_region"], p["concentration"]["max_region_weight"],
            float(p["concentration"]["max_pairwise_correlation_60d"]), float(e["fee_rate"]),
            hms(e["sell_first_deadline"]), hms(e["new_order_cutoff"]))


def buy_quantity(*, target: int, held_value: int, active_buy_value: int, limit_price: int,
                 buyable: int, cash: int, active_buy_remaining: int, total_equity: int,
                 cash_buffer_ratio: float, fee_rate: float) -> int:
    """5.4절 최종 매수 수량 Q = max(0, min(⌊(T−H−P)/p⌋, ⌊(C−buffer·V)/(p(1+fee))⌋)). C = min(nrcvb, 예수금−활성매수잔액)."""
    if limit_price <= 0 or total_equity <= 0:
        return 0
    available = min(buyable, cash - active_buy_remaining)
    by_target = math.floor((target - held_value - active_buy_value) / limit_price)
    by_cash = math.floor((available - cash_buffer_ratio * total_equity) / (limit_price * (1 + fee_rate)))
    return max(0, min(by_target, by_cash))


def _sma120(connection: sqlite3.Connection, ticker: str) -> float | None:
    """가장 최근 완료 거래일을 포함한 직전 120개 수정 종가의 단순평균(부족하면 None → 판정 불가)."""
    rows = connection.execute("SELECT adjusted_close FROM daily_candles WHERE ticker = ? "
                              "ORDER BY trade_date DESC LIMIT 120", (ticker,)).fetchall()
    return sum(r[0] for r in rows) / 120 if len(rows) == 120 else None


def _load_preview(connection: sqlite3.Connection, now: datetime) -> sqlite3.Row | None:
    """최근 8일 안의 가장 큰 revision 의 미소비 프리뷰(`CREATED`/`SENT`)."""
    since = (now - timedelta(days=8)).isoformat()
    return connection.execute(
        "SELECT * FROM weekly_previews WHERE status IN ('CREATED','SENT') AND created_at >= ? "
        "ORDER BY created_at DESC, preview_revision DESC LIMIT 1", (since,)).fetchone()


def create_daily_pipeline_handler(
    connection_factory: Callable[[], sqlite3.Connection],
    broker: PipelineBroker,
    config: PipelineConfig,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = _time.sleep,
    notify: Callable[[str, str], None] = lambda severity, message: None,
    correlation: Callable[[str, str], float | None] | None = None,
    protective_enabled: bool = True,
) -> Callable[[], None]:
    """15:00 파이프라인 콜백을 만든다. 한 번 실행에 최대 `new_order_cutoff` 까지 블로킹한다(max_instances=1)."""

    def handler() -> None:
        now = clock()
        connection = connection_factory()
        try:
            _run(connection, broker, config, now, clock, sleep, notify, correlation, protective_enabled)
        finally:
            connection.close()

    return handler


def _run(connection, broker, cfg, now, clock, sleep, notify, correlation, protective_enabled) -> None:
    """파이프라인 본체(테스트에서는 위 팩토리를 통해 호출)."""
    day = now.astimezone(KST).date().isoformat()
    if is_safe_halt(connection):
        _log(connection, "daily_pipeline", "SKIPPED_SAFE_HALT", "WARN", "SAFE_HALT: 파이프라인 생략", now)
        return
    if connection.execute("SELECT 1 FROM daily_portfolio LIMIT 1").fetchone() is None:
        _log(connection, "daily_pipeline", "SKIPPED_NO_PERFORMANCE", "WARN", "성과 지수 미초기화: 주문 생략", now)
        return
    try:
        equity, cash = broker.total_equity(), broker.cash_d2()
        positions = {p.ticker: p.quantity for p in broker.positions()}
    except Exception as error:  # noqa: BLE001
        _mark_stale(connection, now, f"pipeline:{type(error).__name__}")
        return
    if equity <= 0:
        _log(connection, "daily_pipeline", "SKIPPED_V_LE_0", "WARN", "총평가액 ≤ 0: 주문 생략", now)
        return
    flags = load_risk_flags(connection, protective_exit_enabled=protective_enabled)
    meta = {r["ticker"]: r for r in connection.execute(
        "SELECT ticker, category, region, underlying_index, underlying_index_family, classification_verified, "
        "data_status FROM universe_assets WHERE status = 'ACTIVE'")}
    submitted = 0
    removed = {r[0] for r in connection.execute("SELECT ticker FROM universe_assets WHERE status = 'REMOVED'")}

    # 1. 보유 유지 판정 — 120MA 이탈·트레일링 스탑·검증 상실만 퇴출 사유. 격리(TickerBlock)는 판정 불가(건드리지 않음).
    exited: set[str] = set()
    for ticker, qty in positions.items():
        if ticker in removed and qty > 0:  # /remove 된 보유 종목: 검증 상실 → 퇴출(5.3절 2번)
            if submit_order(connection, broker, SellRequest(ticker, qty, OrderKind.REDUCE_ONLY_SELL,
                            f"M{ticker}:{day}", "REMOVED_EXIT"), now=now, account_fresh=True,
                            protective_enabled=protective_enabled) == "SUBMITTED":
                exited.add(ticker)
                submitted += 1
            continue
        info = meta.get(ticker)
        position = connection.execute("SELECT tracking_status, peak_price_adj FROM positions WHERE ticker = ?",
                                      (ticker,)).fetchone()
        if info is None or info["category"] != "RISK_ASSET" or position is None or position[0] != "MANAGED":
            continue
        if ticker_is_blocked(connection, ticker) or qty <= 0:
            continue
        try:
            price = broker.last_price(ticker)
        except Exception:  # noqa: BLE001
            continue
        sma = _sma120(connection, ticker)
        stop = position[1] is not None and should_trailing_sell(price, float(position[1]),
                                                                training_mode=flags.training_mode)
        if info["classification_verified"] != 1 or stop or (sma is not None and price <= sma):
            result = submit_order(connection, broker, SellRequest(ticker, qty, OrderKind.REDUCE_ONLY_SELL,
                                  f"M{ticker}:{day}", "MA120_EXIT"), now=now, account_fresh=True,
                                  protective_enabled=protective_enabled)
            if result == "SUBMITTED":
                exited.add(ticker)
                submitted += 1
                _log(connection, "daily_pipeline", "MA120_EXIT", "WARN", f"{ticker} 퇴출 매도 제출", now)

    # 2. 주간 첫 개장일: 프리뷰로 최종 슬롯 확정 → 교체 매도
    preview = _load_preview(connection, now)
    ranked_payload = json.loads(preview["ranked_candidates_json"]) if preview else None
    rotation = isinstance(ranked_payload, list)  # RF_INVALID 프리뷰(dict)는 교체·신규 진입 모두 생략
    final_risk: list = []
    rotation_sells = []
    if rotation:
        eligible = []
        for item in ranked_payload:
            t = item["ticker"]
            info = meta.get(t)
            if info is None or info["data_status"] != "VALID" or info["classification_verified"] != 1:
                continue
            if ticker_is_blocked(connection, t) or t in exited:
                continue
            sma = _sma120(connection, t)
            try:
                live = broker.last_price(t)
            except Exception:  # noqa: BLE001
                continue
            if sma is None or live <= sma:
                continue
            eligible.append(SlotCandidate(t, item["region"], item["underlying_index"],
                                          item.get("underlying_index_family", "")))
        held_risk = [t for t, q in positions.items() if q > 0 and t in meta and meta[t]["category"] == "RISK_ASSET"
                     and t not in exited and not ticker_is_blocked(connection, t)]
        final = select_final_slots(
            eligible, held_tickers=held_risk, hold_rank_buffer=cfg.hold_rank_buffer,
            max_slots_per_region=cfg.max_slots_per_region, max_region_weight=cfg.max_region_weight,
            max_pairwise_correlation=cfg.max_pairwise_correlation, correlation=correlation,
            slot_weights=cfg.slot_weights)
        final_risk = [a for a in final if a.ticker]
        keep = {a.ticker for a in final_risk}
        _save_slots(connection, final, now)
        for t in held_risk:
            # 격리 종목은 위에서 제외됨. 최종 슬롯에 없는 보유 위험자산만 전량 교체 매도.
            if t not in keep:
                rotation_sells.append(t)
        for t in rotation_sells:
            result = submit_order(connection, broker, SellRequest(t, positions[t], OrderKind.RISK_ROTATION_SELL,
                                  f"R{t}:{day}", "WEEKLY_ROTATION", region=meta[t]["region"]),
                                  now=now, account_fresh=True, protective_enabled=protective_enabled)
            if result == "SUBMITTED":
                submitted += 1

    # 3. 매도 선행: 이번 실행의 매도가 모두 종결돼야 매수 단계로 진행(시한 초과 시 매수 생략)
    deadline = datetime.combine(now.astimezone(KST).date(), cfg.sell_first_deadline, tzinfo=KST)
    while connection.execute("SELECT 1 FROM orders WHERE side = 'SELL' AND status IN (%s) AND submitted_at >= ? LIMIT 1"
                             % ",".join("?" * len(_ACTIVE_SELL)), (*_ACTIVE_SELL, now.isoformat())).fetchone():
        if clock() >= deadline:
            _log(connection, "daily_pipeline", "BUY_PHASE_SKIPPED", "WARN", "매도 미종결: 매수 단계 생략", now)
            if rotation and submitted:
                _consume(connection, preview, now)
            return
        sleep(30)
        sync_orders(connection, broker, now=clock(), market_closed=False)

    # 4. 매수: 계좌 게이트(데이터·대조·현금흐름·낙폭 단계)와 신규 주문 시한
    cutoff = datetime.combine(now.astimezone(KST).date(), cfg.new_order_cutoff, tzinfo=KST)
    reconciled = (connection.execute("SELECT value FROM system_state WHERE \"key\"='reconciliation_ok'").fetchone()
                  or ("false",))[0] == "true"
    if clock() < cutoff and reconciled and not has_buy_block(connection):
        if rotation:
            risk_weight = sum(a.target_weight for a in final_risk)
        else:  # 교체가 없는 날: 이미 보유 중인 위험자산 수만큼 슬롯 비중(S1부터)이 위험자산 몫이다
            held_now = sum(1 for t, q in positions.items() if q > 0 and t in meta
                           and meta[t]["category"] == "RISK_ASSET" and t not in exited)
            risk_weight = sum(list(cfg.slot_weights.values())[:held_now])
        _buy_phase(connection, broker, cfg, meta, final_risk if rotation else [], risk_weight, day, clock, notify)
    if rotation and submitted + connection.execute(
            "SELECT COUNT(*) FROM orders WHERE strategy_reason IN ('WEEKLY_BUY','WEEKLY_ROTATION') "
            "AND submitted_at >= ?", (now.isoformat(),)).fetchone()[0]:
        _consume(connection, preview, now)


def _save_slots(connection: sqlite3.Connection, final, now: datetime) -> None:
    """확정 슬롯을 `portfolio_slots` 에 저장한다(같은 종목 중복은 유일 인덱스가 막으므로 전체 교체)."""
    with connection:
        connection.execute("DELETE FROM portfolio_slots")
        connection.executemany(
            "INSERT INTO portfolio_slots (slot_id, target_weight, ticker, assigned_at, assignment_reason) "
            "VALUES (?, ?, ?, ?, ?)",
            [(a.slot_id, a.target_weight, a.ticker or None, now.isoformat(), a.reason) for a in final])


def _consume(connection: sqlite3.Connection, preview: sqlite3.Row, now: datetime) -> None:
    """프리뷰를 사용 완료(`CONSUMED`)로 표시한다(부분 실행도 소비 → 승계 안 함, 5.3절 6번)."""
    with connection:
        connection.execute("UPDATE weekly_previews SET status = 'CONSUMED' WHERE preview_id = ? AND "
                           "preview_revision = ?", (preview["preview_id"], preview["preview_revision"]))


def _buy_phase(connection, broker, cfg, meta, final_risk, risk_weight, day, clock, notify) -> None:
    """슬롯 목표액까지 위험자산을 순위순으로 1종목씩, 그다음 대표 파킹을 매수한다(매수 전 자금 재조회)."""
    now = clock()
    try:
        equity = broker.total_equity()
        held = {p.ticker: p.quantity for p in broker.positions()}
    except Exception as error:  # noqa: BLE001
        _mark_stale(connection, now, f"buy:{type(error).__name__}")
        return
    plan = [(a.ticker, equity * a.target_weight, OrderKind.RISK_BUY) for a in final_risk]
    parking_weight = (1 - cfg.cash_buffer_ratio) - risk_weight
    plan.append((cfg.parking_ticker, equity * parking_weight, OrderKind.PARKING_BUY))
    for ticker, target, kind in plan:
        try:
            price_now = broker.last_price(ticker)
            held_value = held.get(ticker, 0) * price_now
            gap = target - held_value
            if gap < cfg.min_trade_amount:
                continue
            if held.get(ticker, 0) > 0 and kind is OrderKind.RISK_BUY:
                if gap / equity < cfg.rebalance_drift:
                    continue
                kind = OrderKind.RISK_TOP_UP_BUY
            quote = broker.quote(ticker)
            limit = calculate_limit_price(quote, OrderSide.BUY)
            active = connection.execute(
                "SELECT COALESCE(SUM(remaining_qty * order_price), 0) FROM orders WHERE side='BUY' AND status IN "
                "('ACCEPTED','PARTIALLY_FILLED','PENDING_CONFIRMATION','CANCEL_PENDING')").fetchone()[0]
            qty = buy_quantity(
                target=int(target), held_value=int(held_value), active_buy_value=0, limit_price=limit,
                buyable=broker.buyable_cash(), cash=broker.cash_d2(), active_buy_remaining=int(active),
                total_equity=equity, cash_buffer_ratio=cfg.cash_buffer_ratio, fee_rate=cfg.fee_rate)
        except Exception as error:  # noqa: BLE001 - 조회 실패·미확정 TR: 매수만 보류
            _log(connection, "daily_pipeline", "BUY_SKIPPED", "WARN", f"{ticker}: {type(error).__name__}", now)
            continue
        if qty <= 0:
            continue
        reason = "WEEKLY_BUY" if kind is not OrderKind.PARKING_BUY else "PARKING_ALLOCATION"
        result = submit_order(connection, broker, SellRequest(ticker, qty, kind, f"B{ticker}:{day}", reason,
                              region=meta[ticker]["region"] if ticker in meta else "KR"),
                              now=clock(), account_fresh=True)
        _log(connection, "daily_pipeline", "BUY_ORDER", "INFO", f"{ticker} {qty}주 → {result}", now)
