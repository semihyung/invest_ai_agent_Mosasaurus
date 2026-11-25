"""승인용 백테스트 보고서 실행기(명세 11.1절): 데이터 → 엔진 → 민감도·Walk-Forward → 보고서 JSON.

`/approve_backtest <run_id> <report_sha256>` 가 읽는 `<run_id>.json` 을 만듭니다(감사 9번: 예전에는 이 파일을 만드는
도구가 없어 REAL 전략 주문의 필수 승인을 받을 방법이 없었습니다). 호스트에서 실행하고, 앱은 컨테이너의
`/app/data/backtests`(= 호스트 `./data/backtests`)에서 읽습니다.

사용:
    python -m backtest.run_report --data-dir <CSV 폴더> --universe-history <YAML> [--out-dir data/backtests]

- `<CSV 폴더>/<티커>.csv`: `load_market_csv` 형식(trade_date, open, low, adjusted_close, volume, turnover_krw).
  모든 종목(파킹 포함)은 같은 거래일 집합이어야 하고, 파킹의 상장 이전 구간은 무위험 프록시로 채워 둔 파일이어야 한다.
- 유니버스 이력 YAML: `parking_ticker`, `parking_proxy_note`(프록시 설명), `assets:` 목록
  (`ticker`, `region`, `index_family`, 선택 `listed_from`·`delisted_after`).
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from backtest.data_loader import load_market_csv
from backtest.engine import AssetSpec, BacktestRun, EngineConfig, run_strategy_backtest, with_params
from backtest.report import build_report, report_sha256, sha256_file
from backtest.walk_forward import walk_forward_evaluate
from src.config import load_config, strategy_params_hash

_RUN_ID_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789_-")


def engine_config_from_rules(rules: Mapping[str, Any]) -> EngineConfig:
    """`rules.yaml` 의 전략·체결 값으로 엔진 설정을 만듭니다(운영 설정과 같은 값을 쓰도록 한 곳에서 변환)."""
    portfolio, execution, momentum, universe = rules["portfolio"], rules["execution"], rules["momentum"], rules["universe"]
    weights = portfolio["slot_weights"]
    return EngineConfig(
        fee_rate=float(execution["fee_rate"]),
        stop_normal=float(rules["trailing_stop"]["normal"]),
        cash_buffer_ratio=float(portfolio["cash_buffer_ratio"]),
        slot_weights=(float(weights["S1"]), float(weights["S2"]), float(weights["S3"])),
        min_trade_amount_krw=int(portfolio["min_trade_amount_krw"]),
        rebalance_drift_ratio=float(portfolio["rebalance_drift_ratio"]),
        hold_rank_buffer=int(portfolio["hold_rank_buffer"]),
        min_daily_excess_volatility=float(momentum["min_daily_excess_volatility"]),
        min_avg_turnover_20d_krw=float(universe["min_avg_turnover_20d_krw"]),
        max_order_participation_rate=float(portfolio["max_order_participation_rate"]),
        max_pairwise_correlation=float(portfolio["concentration"]["max_pairwise_correlation_60d"]),
    )


def load_universe_history(path: str | Path) -> tuple[list[AssetSpec], str, str]:
    """유니버스 이력 YAML 을 읽어 (자산 목록, 파킹 티커, 파킹 프록시 설명) 을 돌려줍니다."""
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("assets"), list):
        raise ValueError("Universe history must contain an 'assets' list.")
    parking, note = payload.get("parking_ticker"), payload.get("parking_proxy_note")
    if not isinstance(parking, str) or not parking or not isinstance(note, str) or not note.strip():
        raise ValueError("Universe history needs parking_ticker and a parking_proxy_note.")
    assets = []
    for item in payload["assets"]:
        if not isinstance(item, dict) or not {"ticker", "region", "index_family"} <= set(item):
            raise ValueError("Each asset needs ticker, region and index_family.")
        assets.append(AssetSpec(
            str(item["ticker"]), str(item["region"]), str(item["index_family"]),
            str(item.get("listed_from", "0000-00-00")), str(item.get("delisted_after", "9999-99-99"))))
    return assets, parking, note


def _git_commit() -> str:
    """보고서에 묶을 코드 커밋(환경변수 CODE_COMMIT_SHA 가 우선, 없으면 현재 저장소의 HEAD)."""
    sha = os.environ.get("CODE_COMMIT_SHA", "").strip()
    if not sha:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    if len(sha) != 40 or set(sha) - set("0123456789abcdef"):
        raise ValueError("code commit SHA must be 40 lowercase hex characters.")
    return sha


def generate_report(
    *,
    data_dir: str | Path,
    universe_history: str | Path,
    rules: Mapping[str, Any],
    code_commit_sha: str,
    training_sessions: int = 756,
    test_sessions: int = 252,
) -> dict:
    """엔진을 기준·비용·파라미터 민감도·Walk-Forward 로 돌려 승인 보고서(dict)를 만듭니다."""
    assets, parking, note = load_universe_history(universe_history)
    config = engine_config_from_rules(rules)
    tickers = sorted({asset.ticker for asset in assets} | {parking})
    files = {ticker: Path(data_dir) / f"{ticker}.csv" for ticker in tickers}
    bars = {ticker: load_market_csv(path) for ticker, path in files.items()}
    total = len(bars[parking])

    def run(changes: Mapping[str, float], start: int = 200, end: int | None = None) -> BacktestRun:
        return run_strategy_backtest(assets, bars, parking, with_params(config, **changes), start_index=start, end_index=end)

    base = run({})
    cost = {
        "fee_x2": run({"fee_rate": config.fee_rate * 2}),
        "slippage_x2": run({"slippage_ratio": config.slippage_ratio * 2}),
        "slippage_x3": run({"slippage_ratio": config.slippage_ratio * 3}),
    }
    params = {
        "stop_-8pct": run({"stop_normal": -0.08}),
        "stop_-12pct": run({"stop_normal": -0.12}),
        "hold_rank_buffer_3": run({"hold_rank_buffer": 3}),
        "hold_rank_buffer_7": run({"hold_rank_buffer": 7}),
    }
    grid = [{"hold_rank_buffer": buffer, "stop_normal": stop} for buffer in (3, 5, 7) for stop in (-0.08, -0.10, -0.12)]

    def window_run(chosen: Mapping[str, float], start: int, end: int) -> tuple[float, float, Sequence[float]]:
        result = run(chosen, start, end)
        return result.cagr, result.mdd, result.equity

    windows = walk_forward_evaluate(
        window_run, grid, history_sessions=200, total_sessions=total,
        training_sessions=training_sessions, test_sessions=test_sessions)
    walk_forward = [
        {"train_range": list(w.train_range), "test_range": list(w.test_range), "chosen_params": dict(w.chosen_params),
         "train_score": round(w.train_score, 6), "test_cagr": round(w.test_cagr, 6), "test_mdd": round(w.test_mdd, 6)}
        for w in windows
    ]
    return build_report(
        base=base, cost_sensitivity=cost, parameter_sensitivity=params, walk_forward=walk_forward,
        strategy_params_hash=strategy_params_hash(rules), code_commit_sha=code_commit_sha,
        data_files={path.name: sha256_file(path) for path in files.values()},
        universe_history_sha256=sha256_file(universe_history), parking_proxy_note=note)


def write_report(report: Mapping[str, Any], out_dir: str | Path, run_id: str | None = None) -> tuple[str, str, Path]:
    """보고서를 `<run_id>.json` 으로 저장하고 (run_id, SHA-256, 경로) 를 돌려줍니다."""
    digest = report_sha256(report)
    run_id = run_id or f"bt-{datetime.now(timezone.utc):%Y%m%d}-{digest[:8]}"
    if not 4 <= len(run_id) <= 64 or set(run_id) - _RUN_ID_ALLOWED:
        raise ValueError("run_id must be 4-64 chars of a-z, 0-9, '_' or '-' (the /approve_backtest format).")
    path = Path(out_dir) / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True), encoding="utf-8")
    return run_id, digest, path


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: 보고서를 만들고 텔레그램에 입력할 승인 명령을 출력합니다."""
    parser = argparse.ArgumentParser(description="Build the backtest approval report (spec 11.1).")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--universe-history", required=True)
    parser.add_argument("--out-dir", default="data/backtests")
    parser.add_argument("--run-id")
    parser.add_argument("--training-sessions", type=int, default=756)
    parser.add_argument("--test-sessions", type=int, default=252)
    args = parser.parse_args(argv)
    report = generate_report(
        data_dir=args.data_dir, universe_history=args.universe_history, rules=load_config(),
        code_commit_sha=_git_commit(), training_sessions=args.training_sessions, test_sessions=args.test_sessions)
    run_id, digest, path = write_report(report, args.out_dir, args.run_id)
    print(f"보고서: {path}")
    print(f"텔레그램에서 입력: /approve_backtest {run_id} {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
