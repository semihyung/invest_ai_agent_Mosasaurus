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
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Mapping, Protocol

from src.job_handlers import AccountBrokerPort, _clear_transient_stale, _log, _mark_stale, _safe_notify, reconcile_cash
from src.order_executor import (
    KST, SellRequest, SPREAD_NAV, parking_cycle_id, risk_cycle_id, submit_order, weekly_cycle_id,
)
from src.order_manager import OrderSide, calculate_limit_price
from src.order_policy import OrderKind
from src.order_sync import sync_orders
from src.portfolio_manager import SlotCandidate, select_final_slots
from src.risk_manager import should_trailing_sell
from src.safety_flags import has_buy_block, is_safe_halt, load_risk_flags, ticker_is_blocked

_ACTIVE_SELL = ("CREATED", "SUBMITTING", "PENDING_CONFIRMATION", "ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING")


class PipelineBroker(AccountBrokerPort, Protocol):
    """파이프라인 추가 조회: 매수가능금액(`nrcvb_buy_amt`)."""

    def buyable_cash(self) -> int:
        """매수가능금액(`nrcvb_buy_amt`)을 돌려준다."""


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
    min_history_sessions: int = 200
    min_avg_turnover_20d: float = 0.0
    rotation_carryover_days: int = 1
    max_order_participation_rate: float = 0.01

    @classmethod
    def from_rules(cls, rules: Mapping) -> "PipelineConfig":
        """rules.yaml 에서 파이프라인 파라미터를 읽어 만든다."""
        p, e = rules["portfolio"], rules["execution"]
        hms = lambda text: time.fromisoformat(text)
        return cls(
            p["primary_parking_ticker"], p["slot_weights"], float(p["cash_buffer_ratio"]),
            int(p["min_trade_amount_krw"]), float(p["rebalance_drift_ratio"]), int(p["hold_rank_buffer"]),
            p["concentration"]["max_slots_per_region"], p["concentration"]["max_region_weight"],
            float(p["concentration"]["max_pairwise_correlation_60d"]), float(e["fee_rate"]),
            hms(e["sell_first_deadline"]), hms(e["new_order_cutoff"]),
            int(rules["universe"]["min_history_sessions"]),
            float(rules["universe"]["min_avg_turnover_20d_krw"]),
            int(rules["market"]["weekly_rotation_carryover_days"]),
            float(p["max_order_participation_rate"]))


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


def db_correlation(connection: sqlite3.Connection) -> Callable[[str, str], float | None]:
    """최근 60거래일 일간 수익률의 피어슨 상관계수(데이터 부족·일자 불일치면 None → 집중도 검사에서 건너뜀)."""

    def series(ticker: str) -> dict[str, float]:
        """종목의 최근 일간 수익률 시계열(일자 → 수익률)."""
        rows = connection.execute("SELECT trade_date, adjusted_close FROM daily_candles WHERE ticker = ? "
                                  "ORDER BY trade_date DESC LIMIT 61", (ticker,)).fetchall()
        rows.reverse()
        return {rows[i]["trade_date"]: rows[i]["adjusted_close"] / rows[i - 1]["adjusted_close"] - 1
                for i in range(1, len(rows))}

    def correlation(a: str, b: str) -> float | None:
        """두 종목 수익률의 피어슨 상관계수(계산 불가면 None)."""
        x, y = series(a), series(b)
        if len(x) < 60 or x.keys() != y.keys():
            return None
        xs, ys = [x[d] for d in sorted(x)], [y[d] for d in sorted(x)]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        cov = sum((p - mx) * (q - my) for p, q in zip(xs, ys))
        var = math.sqrt(sum((p - mx) ** 2 for p in xs) * sum((q - my) ** 2 for q in ys))
        return cov / var if var else None

    return correlation


def _passes_live_filters(connection: sqlite3.Connection, ticker: str, cfg: "PipelineConfig") -> bool:
    """5.2절 신규 진입 재평가 중 DB 로 판정 가능한 항목: 거래 이력, 20일 평균 거래대금, 재진입 차단 해제."""
    rows = connection.execute("SELECT raw_close, volume FROM daily_candles WHERE ticker = ? "
                              "ORDER BY trade_date DESC LIMIT 200", (ticker,)).fetchall()
    if len(rows) < cfg.min_history_sessions:
        return False
    recent = rows[:20]
    if any(r["raw_close"] is None for r in recent):
        return False
    if sum(r["raw_close"] * r["volume"] for r in recent) / 20 < cfg.min_avg_turnover_20d:
        return False
    blocked = connection.execute("SELECT blocked_until_next_rotation FROM universe_assets WHERE ticker = ?",
                                 (ticker,)).fetchone()
    return blocked is not None and blocked[0] == 0


def _rotation_allowed(connection: sqlite3.Connection, preview: sqlite3.Row, day: str, carryover_days: int) -> bool:
    """주간 교체를 시도하는 개장일 수를 세어 첫 개장일 + `carryover_days` 일까지만 허용한다(5.3절 6번).

    이미 센 날짜는 그대로 허용(같은 날 재실행), 새 날짜가 한도를 넘으면 프리뷰를 `EXPIRED` 로 닫는다.
    """
    key = f"rotation_days:{preview['preview_id']}:{preview['preview_revision']}"
    row = connection.execute('SELECT value FROM system_state WHERE "key" = ?', (key,)).fetchone()
    days = [d for d in (row[0].split(",") if row and row[0] else [])]
    if day in days:
        return True
    with connection:
        if len(days) >= 1 + carryover_days:
            connection.execute("UPDATE weekly_previews SET status = 'EXPIRED' WHERE preview_id = ? AND "
                               "preview_revision = ?", (preview["preview_id"], preview["preview_revision"]))
            return False
        connection.execute(
            'INSERT INTO system_state ("key", value, updated_at) VALUES (?, ?, ?) '
            'ON CONFLICT("key") DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at',
            (key, ",".join(days + [day]), day))
    return True


def _avg_turnover_20d(connection: sqlite3.Connection, ticker: str) -> float:
    """최근 20거래일 평균 거래대금(원주가 종가 × 거래량, 이력이 20일 미만이면 0)."""
    rows = connection.execute("SELECT raw_close, volume FROM daily_candles WHERE ticker = ? AND raw_close IS NOT NULL "
                              "ORDER BY trade_date DESC LIMIT 20", (ticker,)).fetchall()
    return sum(r["raw_close"] * r["volume"] for r in rows) / 20 if len(rows) == 20 else 0.0


def _load_preview(connection: sqlite3.Connection, now: datetime) -> sqlite3.Row | None:
    """최근 8일 안의 가장 큰 revision 의 미소비 프리뷰(`CREATED`/`SENT`)."""
    since = (now - timedelta(days=8)).isoformat()
    return connection.execute(
        "SELECT * FROM weekly_previews WHERE status IN ('CREATED','SENT','DELIVERY_FAILED') AND created_at >= ? "
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
        """15:00 파이프라인 한 번 실행."""
        now = clock()
        connection = connection_factory()
        try:
            _run(connection, broker, config, now, clock, sleep, notify,
                 correlation or db_correlation(connection), protective_enabled)
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
    # 5.4절: 매 개장일 15:00 시작 시에도 예수금 대사(정지 상태일 때만). 기준 예수금은 옮기지 않는다.
    if connection.execute("SELECT 1 FROM orders WHERE status IN (%s) LIMIT 1" % ",".join("?" * len(_ACTIVE_SELL)),
                          _ACTIVE_SELL).fetchone() is None:
        reconcile_cash(connection, current_cash=cash, now=now, trade_day=now.astimezone(KST).date(),
                       advance_baseline=False)
    _clear_transient_stale(connection, now)  # 계좌 조회 성공: 일시 실패로 걸린 매수 차단 해제
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
                            risk_cycle_id(day, "REMOVE"), "REMOVED_EXIT"), now=now, account_fresh=True,
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
                                  risk_cycle_id(day, "MA120"), "MA120_EXIT"), now=now, account_fresh=True,
                                  protective_enabled=protective_enabled)
            if result == "SUBMITTED":
                exited.add(ticker)
                submitted += 1
                _log(connection, "daily_pipeline", "MA120_EXIT", "WARN", f"{ticker} 퇴출 매도 제출", now)

    # 2. 주간 첫 개장일: 프리뷰로 최종 슬롯 확정 → 교체 매도
    preview = _load_preview(connection, now)
    if preview is not None and not _rotation_allowed(connection, preview, day, cfg.rotation_carryover_days):
        preview = None  # 승계 한도 초과: 이번 주 교체는 더 이상 시도하지 않는다
    ranked_payload = json.loads(preview["ranked_candidates_json"]) if preview else None
    rotation = isinstance(ranked_payload, list)  # RF_INVALID 프리뷰(dict)는 교체·신규 진입 모두 생략
    final_risk: list = []
    rotation_sells = []
    planned_sells: list[SellRequest] = []
    if rotation:
        eligible = []
        for item in ranked_payload:
            t = item["ticker"]
            info = meta.get(t)
            if info is None or info["data_status"] != "VALID" or info["classification_verified"] != 1:
                continue
            if ticker_is_blocked(connection, t) or t in exited or not _passes_live_filters(connection, t, cfg):
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
            planned_sells.append(SellRequest(t, positions[t], OrderKind.RISK_ROTATION_SELL, weekly_cycle_id(day),
                                             "WEEKLY_ROTATION", region=meta[t]["region"]))

        # 리밸런싱 초과분 매도: 유지 종목의 Drift ≥ 5%p AND 최소 주문 금액. 목표보다 많이 팔지 않도록 내림.
        for a in final_risk:
            qty = positions.get(a.ticker, 0)
            if qty <= 0 or a.ticker in rotation_sells:
                continue
            try:
                price = broker.last_price(a.ticker)
                limit = calculate_limit_price(broker.quote(a.ticker), OrderSide.SELL)
            except Exception:  # noqa: BLE001
                continue
            excess = qty * price - equity * a.target_weight
            if excess >= cfg.min_trade_amount and excess / equity >= cfg.rebalance_drift:
                sell_qty = min(math.floor(excess / limit), qty)
                if sell_qty > 0:
                    planned_sells.append(SellRequest(a.ticker, sell_qty, OrderKind.RISK_ROTATION_SELL,
                                                     weekly_cycle_id(day), "WEEKLY_REBALANCE",
                                                     region=meta[a.ticker]["region"]))
        # 파킹 현금화 매도: 위험자산 매수에 필요한 현금이 부족하면 부족분 N 만큼 올림 수량으로 판다.
        needed = 0
        for a in final_risk:
            try:
                have = positions.get(a.ticker, 0) * broker.last_price(a.ticker)
            except Exception:  # noqa: BLE001
                continue
            gap = equity * a.target_weight - have
            if gap >= cfg.min_trade_amount and (have == 0 or gap / equity >= cfg.rebalance_drift):
                needed += gap
        parking_qty = positions.get(cfg.parking_ticker, 0)
        if needed and parking_qty > 0:
            try:
                spare = max(0, min(broker.buyable_cash(), cash) - cfg.cash_buffer_ratio * equity)
                shortfall = needed - spare
                if shortfall > 0:
                    bid = calculate_limit_price(broker.quote(cfg.parking_ticker), OrderSide.SELL)
                    sell_qty = min(math.ceil(shortfall / bid), parking_qty)
                    region = meta[cfg.parking_ticker]["region"] if cfg.parking_ticker in meta else "KR"
                    planned_sells.append(SellRequest(cfg.parking_ticker, sell_qty, OrderKind.PARKING_SELL,
                                                     parking_cycle_id(day), "PARKING_LIQUIDATION",
                                                     region=region))
            except Exception as error:  # noqa: BLE001 - 자금 조회 불가: 현금화 보류(매수도 같은 이유로 막힘)
                _log(connection, "daily_pipeline", "PARKING_SELL_SKIPPED", "WARN", type(error).__name__, now)

    # 3. 매도 선행: 계획한 매도를 제출한다. 스프레드·iNAV 에 막힌 매도는 30초 간격으로 시한까지 다시 평가한다(5.3절 5번).
    deadline = datetime.combine(now.astimezone(KST).date(), cfg.sell_first_deadline, tzinfo=KST)
    todo, attempt = planned_sells, 0
    while todo:
        blocked = []
        for request in todo:
            result = submit_order(connection, broker, request, now=clock(), account_fresh=True,
                                  protective_enabled=protective_enabled)
            if result == "SUBMITTED":
                submitted += 1
            elif result == SPREAD_NAV:
                blocked.append(request)
        todo = blocked
        if not todo or clock() >= deadline:
            break
        sleep(30)
        attempt += 1
    if todo:  # 시한까지 제출하지 못한 교체 매도: 이월하지 않는다. 슬롯 밖 종목이 남으므로 위험자산 신규 매수는 하지 않는다.
        _log(connection, "daily_pipeline", "ROTATION_SELL_UNSUBMITTED", "WARN",
             ",".join(r.ticker for r in todo) + " 교체 매도 미제출: 위험자산 매수 생략", now)
        final_risk = []
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
    uncertain_buy = connection.execute(
        "SELECT 1 FROM orders WHERE side = 'BUY' AND status IN ('SUBMITTING','PENDING_CONFIRMATION') LIMIT 1"
    ).fetchone() is not None  # 불확실한 매수 주문이 있으면 계좌 신규 매수 대기(D3)
    if clock() < cutoff and reconciled and not has_buy_block(connection) and not uncertain_buy:
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
        # 5.2절 6번: 이 프리뷰의 기준일이 손절일 이후이면 재진입 차단을 푼다.
        connection.execute("UPDATE universe_assets SET blocked_until_next_rotation = 0 "
                           "WHERE blocked_until_next_rotation = 1 AND blocked_since <= ?",
                           (preview["base_candle_date"],))


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
        # 7.1절 게이트 5 유동성 한도: 주문 금액이 20일 평균 거래대금의 participation 비율을 넘지 않게 줄인다
        qty = min(qty, math.floor(cfg.max_order_participation_rate * _avg_turnover_20d(connection, ticker) / limit))
        if qty <= 0:
            continue
        reason = "WEEKLY_BUY" if kind is not OrderKind.PARKING_BUY else "PARKING_ALLOCATION"
        result = submit_order(connection, broker, SellRequest(ticker, qty, kind, parking_cycle_id(day) if kind is OrderKind.PARKING_BUY else weekly_cycle_id(day), reason,
                              region=meta[ticker]["region"] if ticker in meta else "KR"),
                              now=clock(), account_fresh=True)
        _log(connection, "daily_pipeline", "BUY_ORDER", "INFO", f"{ticker} {qty}주 → {result}", now)
