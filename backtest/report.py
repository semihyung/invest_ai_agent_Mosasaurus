"""백테스트 승인 산출물(명세 11.1절)을 만들고 `/approve_backtest` 에 쓰이는 해시를 계산합니다."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from backtest.engine import BacktestRun


def sha256_file(path: str | Path) -> str:
    """입력 데이터 파일의 SHA-256 입니다(데이터 출처·유니버스 이력 고정용)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def summarize(run: BacktestRun) -> dict:
    """단일 실행의 CAGR·MDD·회전율·비용·매매 수 요약입니다."""
    return {
        "cagr": round(run.cagr, 6),
        "mdd": round(run.mdd, 6),
        "annual_turnover": round(run.turnover, 6),
        "trades": run.trades,
        "stops": run.stops,
        "rotations": run.rotations,
        "fees_paid_krw": round(run.fees_paid, 2),
        "slippage_cost_krw": round(run.slippage_cost, 2),
        "sessions": len(run.equity),
    }


def build_report(
    *,
    base: BacktestRun,
    cost_sensitivity: Mapping[str, BacktestRun],
    parameter_sensitivity: Mapping[str, BacktestRun],
    walk_forward: Sequence[Mapping[str, object]],
    strategy_params_hash: str,
    code_commit_sha: str,
    data_files: Mapping[str, str],
    universe_history_sha256: str,
    parking_proxy_note: str,
) -> dict:
    """승인 보고서 본문을 만듭니다(키 순서·반올림이 고정되어 같은 입력이면 같은 해시)."""
    return {
        "schema": "mosasaurus-backtest-report/1",
        "strategy_params_hash": strategy_params_hash,
        "code_commit_sha": code_commit_sha,
        "data_files_sha256": dict(sorted(data_files.items())),
        "universe_history_sha256": universe_history_sha256,
        "data_range": [base.dates[0], base.dates[-1]],
        "assumptions": [
            "신호는 직전 거래일까지 완료된 수정 종가로만 계산(미래 참조 금지)",
            "주간 첫 개장일 15:00 체결을 당일 종가 ± 슬리피지로 근사",
            "트레일링 스탑 고점=완료 종가 최고치, 트리거=일봉 저가(INTRADAY) 및 다음 날 시가(NEXT_OPEN) 시나리오",
            "수정주가 기준 체결(원주가·분배락 보정 미반영)",
            parking_proxy_note,
        ],
        "base": summarize(base),
        "cost_sensitivity": {name: summarize(run) for name, run in sorted(cost_sensitivity.items())},
        "parameter_sensitivity": {name: summarize(run) for name, run in sorted(parameter_sensitivity.items())},
        "walk_forward": [dict(window) for window in walk_forward],
    }


def report_sha256(report: Mapping[str, object]) -> str:
    """보고서의 정규화 JSON SHA-256 입니다(`/approve_backtest` 의 `report_sha256` 인자)."""
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
