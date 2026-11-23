"""스케줄러가 기존 전략·DB 모듈을 안전하게 호출하도록 연결합니다."""

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from src.portfolio_manager import SlotCandidate, build_weekly_preview
from src.quant_engine import CandidateProfile, assess_risk_candidate, rank_candidates
from src.scheduler import KST, ScheduleJob
from src.universe import has_approved_classification, load_universe_allowlist


_REQUIRED_RULE_SECTIONS = ("portfolio", "momentum", "universe")


def _read_candles(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    base_date: str,
) -> list[sqlite3.Row]:
    """기준 거래일까지의 완료 일봉을 최신 200개만 가져옵니다."""
    return connection.execute(
        """
        SELECT trade_date, raw_close, adjusted_close, volume
        FROM daily_candles
        WHERE ticker = ? AND trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT 200
        """,
        (ticker, base_date),
    ).fetchall()


def create_weekly_preview_handler(
    connection_factory: Callable[[], sqlite3.Connection],
    rules: Mapping[str, Any],
    *,
    now: Callable[[], datetime] | None = None,
) -> Callable[[], None]:
    """완료 일봉으로 순위를 산출하고 멱등하게 weekly_previews에 저장합니다."""
    if not callable(connection_factory):
        raise TypeError("connection_factory must be callable.")
    if not isinstance(rules, Mapping) or any(
        section not in rules or not isinstance(rules[section], Mapping)
        for section in _REQUIRED_RULE_SECTIONS
    ):
        raise ValueError("Rules must include portfolio, momentum, and universe mappings.")
    portfolio = rules["portfolio"]
    momentum = rules["momentum"]
    universe = rules["universe"]
    if universe.get("require_verified_classification") is not True:
        raise ValueError("Verified product classification must be required.")
    if universe.get("forbid_name_keyword_heuristic") is not True:
        raise ValueError("Product-name keyword heuristics must be disabled.")
    synthetic_parking_tickers = universe.get(
        "allowlisted_synthetic_parking_tickers", ()
    )
    if not isinstance(synthetic_parking_tickers, (list, tuple)) or any(
        not isinstance(ticker, str) or not ticker
        for ticker in synthetic_parking_tickers
    ):
        raise ValueError("Synthetic parking tickers must be a string list.")
    allowlist_value = universe.get("allowlist_file")
    if not isinstance(allowlist_value, str) or not allowlist_value:
        raise ValueError("A universe allowlist path is required.")
    allowlist_path = Path(allowlist_value).expanduser()
    if not allowlist_path.is_absolute():
        allowlist_path = Path(__file__).resolve().parent.parent / allowlist_path
    allowlist_assets = load_universe_allowlist(
        allowlist_path,
        allowed_synthetic_parking_tickers=synthetic_parking_tickers,
    )
    allowlist_by_ticker = {
        asset["ticker"]: asset for asset in allowlist_assets
    }
    parking_ticker = portfolio.get("primary_parking_ticker")
    if not isinstance(parking_ticker, str) or not parking_ticker:
        raise ValueError("A primary parking ticker is required.")
    if now is not None and not callable(now):
        raise TypeError("now must be callable.")
    clock = now or (lambda: datetime.now(timezone.utc))

    def handler() -> None:
        """계산 결과가 기존 프리뷰와 다르면 조용히 덮어쓰지 않습니다."""
        timestamp = clock()
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise ValueError("now must return a timezone-aware datetime.")
        cutoff_date = (
            timestamp.astimezone(KST).date() - timedelta(days=1)
        ).isoformat()
        connection = connection_factory()
        try:
            asset_rows = connection.execute(
                """
                SELECT ticker, name, category, region, underlying_index,
                       product_type, status, data_status,
                       blocked_until_next_rotation, replication_method,
                       leverage_multiplier, is_inverse, is_futures_based,
                       classification_verified, classification_source,
                       verified_by, verified_at
                FROM universe_assets
                WHERE status = 'ACTIVE'
                ORDER BY ticker
                """
            ).fetchall()
            parking_asset = next(
                (asset for asset in asset_rows if asset["ticker"] == parking_ticker),
                None,
            )
            if (
                parking_asset is None
                or parking_asset["category"] != "SAFE_PARKING"
                or parking_asset["product_type"] != "SPOT_ETF"
                or parking_asset["data_status"] != "VALID"
                or parking_asset["classification_verified"] != 1
                or parking_asset["leverage_multiplier"] != 1.0
                or parking_asset["is_inverse"] != 0
                or parking_asset["is_futures_based"] != 0
                or (
                    parking_asset["replication_method"] == "SYNTHETIC"
                    and parking_ticker not in synthetic_parking_tickers
                )
                or not has_approved_classification(
                    connection,
                    parking_asset,
                    allowlist_by_ticker=allowlist_by_ticker,
                    allowed_synthetic_parking_tickers=synthetic_parking_tickers,
                )
            ):
                raise RuntimeError("Parking asset is missing or invalid.")
            parking_rows = _read_candles(
                connection,
                ticker=parking_ticker,
                base_date=cutoff_date,
            )
            if len(parking_rows) < 200:
                raise RuntimeError("Parking asset does not have 200 completed candles.")
            parking_rows.reverse()
            base_date = parking_rows[-1]["trade_date"]
            unverified_parking_action = connection.execute(
                """
                SELECT 1 FROM corporate_actions
                WHERE ticker = ? AND ex_date <= ? AND verified = 0
                LIMIT 1
                """,
                (parking_ticker, base_date),
            ).fetchone()
            if unverified_parking_action:
                raise RuntimeError("Parking asset has an unverified corporate action.")
            parking_closes = [float(row["adjusted_close"]) for row in parking_rows]
            held_tickers = {
                row["ticker"]
                for row in connection.execute(
                    "SELECT ticker FROM positions WHERE quantity > 0"
                ).fetchall()
            }
            candidates = []
            by_ticker = {}
            rejected_assets = []
            for asset in asset_rows:
                ticker = asset["ticker"]
                if ticker == parking_ticker or asset["category"] != "RISK_ASSET":
                    continue
                if not has_approved_classification(
                    connection,
                    asset,
                    allowlist_by_ticker=allowlist_by_ticker,
                    allowed_synthetic_parking_tickers=synthetic_parking_tickers,
                ):
                    rejection_code = (
                        "UNVERIFIED_CLASSIFICATION"
                        if asset["classification_verified"] != 1
                        else "MISSING_CLASSIFICATION_APPROVAL"
                    )
                    rejected_assets.append((ticker, rejection_code))
                    continue
                if (
                    asset["leverage_multiplier"] != 1.0
                    or asset["is_inverse"] != 0
                    or asset["is_futures_based"] != 0
                    or asset["replication_method"] != "PHYSICAL"
                ):
                    rejected_assets.append((ticker, "INELIGIBLE_PRODUCT"))
                    continue
                rows = _read_candles(
                    connection,
                    ticker=ticker,
                    base_date=base_date,
                )
                if len(rows) < 200:
                    continue
                rows.reverse()
                if [row["trade_date"] for row in rows] != [
                    row["trade_date"] for row in parking_rows
                ]:
                    continue
                if any(row["raw_close"] is None for row in rows[-20:]):
                    raise RuntimeError("Raw closing prices are missing from turnover history.")
                avg_turnover = sum(
                    float(row["raw_close"]) * int(row["volume"])
                    for row in rows[-20:]
                ) / 20
                unverified_action = connection.execute(
                    """
                    SELECT 1 FROM corporate_actions
                    WHERE ticker = ? AND ex_date <= ? AND verified = 0
                    LIMIT 1
                    """,
                    (ticker, base_date),
                ).fetchone()
                profile = CandidateProfile(
                    ticker=ticker,
                    category=asset["category"],
                    status=asset["status"],
                    data_status=(
                        "DATA_INVALID"
                        if asset["data_status"] != "VALID" or unverified_action
                        else "VALID"
                    ),
                    product_type=asset["product_type"],
                    is_leveraged=float(asset["leverage_multiplier"]) != 1.0,
                    is_inverse=bool(asset["is_inverse"]),
                    is_futures=bool(asset["is_futures_based"]),
                    is_etn=asset["product_type"] != "SPOT_ETF",
                    adjusted_closes=[float(row["adjusted_close"]) for row in rows],
                    parking_adjusted_closes=parking_closes,
                    avg_turnover_20d=avg_turnover,
                    estimated_order_amount=0,
                    currently_held=ticker in held_tickers,
                    blocked_until_next_rotation=bool(
                        asset["blocked_until_next_rotation"]
                    ),
                    classification_verified=asset["classification_verified"] == 1,
                    leverage_multiplier=float(asset["leverage_multiplier"]),
                    replication_method=asset["replication_method"],
                )
                assessment = assess_risk_candidate(
                    profile,
                    min_history_sessions=int(universe["min_history_sessions"]),
                    min_avg_turnover_20d=float(
                        universe["min_avg_turnover_20d_krw"]
                    ),
                    max_order_participation_rate=float(
                        rules["portfolio"]["max_order_participation_rate"]
                    ),
                    min_volatility_floor=float(momentum["min_volatility_floor"]),
                )
                if assessment.candidate is not None:
                    candidates.append(assessment.candidate)
                    by_ticker[ticker] = SlotCandidate(
                        ticker=ticker,
                        region=asset["region"],
                        underlying_index=asset["underlying_index"],
                    )

            ranked = rank_candidates(candidates)
            existing_slots = {
                row["slot_id"]: row["ticker"]
                for row in connection.execute(
                    "SELECT slot_id, ticker FROM portfolio_slots WHERE ticker IS NOT NULL"
                ).fetchall()
            }
            preview = build_weekly_preview(
                base_date,
                (by_ticker[item.ticker] for item in ranked),
                parking_ticker=parking_ticker,
                existing_slot_tickers=existing_slots,
                max_slots_per_region=int(
                    rules["portfolio"]["concentration"]["max_slots_per_region"]
                ),
            )
            payloads = (
                json.dumps(
                    [asdict(by_ticker[item.ticker]) for item in preview.ranked_candidates],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(
                    [asdict(item) for item in preview.projected_slots],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(
                    [asdict(item) for item in preview.projected_trades],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            preview_id = "weekly-" + hashlib.sha256(
                base_date.encode("ascii")
            ).hexdigest()[:24]
            existing = connection.execute(
                """
                SELECT ranked_candidates_json, projected_slots_json,
                       projected_trades_json
                FROM weekly_previews WHERE preview_id = ?
                """,
                (preview_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != payloads:
                    raise RuntimeError("WEEKLY_PREVIEW_CONFLICT")
                return
            with connection:
                connection.execute(
                    """
                    INSERT INTO weekly_previews (
                        preview_id, base_candle_date, ranked_candidates_json,
                        projected_slots_json, projected_trades_json, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'CREATED', ?)
                    """,
                    (preview_id, base_date, *payloads, timestamp.isoformat()),
                )
                for ticker, rejection_code in rejected_assets:
                    connection.execute(
                        """
                        INSERT INTO system_events (
                            component, event_type, severity, message, created_at
                        ) VALUES ('scheduler', 'CANDIDATE_REJECTED', 'WARN', ?, ?)
                        """,
                        (
                            f"티커 {ticker}: {rejection_code} 사유로 프리뷰에서 제외했습니다.",
                            timestamp.isoformat(),
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO system_events (
                        component, event_type, severity, message, created_at
                    ) VALUES ('scheduler', ?, 'INFO', ?, ?)
                    """,
                    (
                        ScheduleJob.WEEKLY_RANKING_PREVIEW.value,
                        "주간 순위 프리뷰가 저장되었습니다.",
                        timestamp.isoformat(),
                    ),
                )
        finally:
            connection.close()

    return handler
