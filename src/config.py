"""YAML 설정 파일과 런타임 환경 변수를 읽습니다."""

import os
import re
from pathlib import Path
from typing import Any, Mapping

import yaml


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """설정 파일을 읽거나 검증할 때 발생하는 오류입니다."""


def load_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """YAML 설정을 안전하게 읽고 `${NAME}`을 환경 변수로 치환합니다."""
    config_path = (
        Path(path).expanduser()
        if path is not None
        else _PROJECT_ROOT / "config" / "rules.yaml"
    )
    environment = os.environ if environ is None else environ

    try:
        contents = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Unable to read configuration file: {config_path}") from exc

    try:
        payload = yaml.safe_load(contents)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML configuration: {config_path}") from exc

    if not isinstance(payload, dict) or any(
        not isinstance(key, str) for key in payload
    ):
        raise ConfigError("Configuration root must be a string-keyed mapping.")

    expanded = _expand_environment(payload, environment)
    if not isinstance(expanded, dict):
        raise ConfigError("Configuration root must be a mapping.")
    return expanded


def _expand_environment(value: Any, environ: Mapping[str, str]) -> Any:
    """설정 트리의 문자열 값에서 필수 환경 변수 참조를 재귀적으로 치환합니다."""
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ConfigError("Configuration mappings must use string keys.")
        return {
            key: _expand_environment(item, environ)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_expand_environment(item, environ) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        replacement = environ.get(name)
        if not isinstance(replacement, str) or not replacement:
            raise ConfigError(f"Required environment variable '{name}' is not set.")
        return replacement

    expanded = _ENV_REFERENCE.sub(replace, value)
    if "${" in expanded:
        raise ConfigError("Malformed environment variable reference in configuration.")
    return expanded


# 명세 10.5절: 폐기된 키가 남아 있으면 부팅을 거부합니다(오타·구버전 설정 방지).
_DEPRECATED_KEYS = (
    "smoke_test_mode",
    "market.order_expire_time",
    "market.order_cancel_deadline",
    "momentum.min_volatility_floor",
    "security.mount_token_file_read_only",
    "deployment.require_backtest_calibration_for_real",
)
# 알려진 최상위 섹션: 이 밖의 섹션은 오타로 보고 거부합니다.
_KNOWN_SECTIONS = frozenset(
    {
        "project_name", "version", "is_calibrated_by_backtest", "smoke_test",
        "deployment", "features", "market", "account", "kis", "clock", "strategy",
        "portfolio", "momentum", "trailing_stop", "execution", "risk",
        "corporate_action", "accounting", "security", "telegram", "ups",
        "watchdog", "net_watchdog", "db", "backup", "ttl_seconds", "universe",
        "host_state",
    }
)
# 시각 키는 이 순서대로 증가해야 합니다(마감 타임라인 역전 방지).
_TIME_ORDER = (
    ("market", "morning_prep_time"),
    ("market", "regular_open"),
    ("market", "daily_pipeline_time"),
    ("execution", "sell_first_deadline"),
    ("execution", "new_order_cutoff"),
    ("market", "order_cancel_sweep_time"),
    ("market", "regular_close"),
    ("market", "post_close_reconcile_time"),
)


def _lookup(config: Mapping[str, Any], dotted: str) -> Any:
    """`a.b.c` 형태의 경로로 설정 값을 찾고 없으면 None을 반환합니다."""
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _seconds(value: Any) -> int | None:
    """`HH:MM:SS` 문자열을 하루 중 초로 바꾸고 형식이 틀리면 None을 반환합니다."""
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    hours, minutes, seconds = (int(part) for part in parts)
    if hours > 23 or minutes > 59 or seconds > 59:
        return None
    return hours * 3600 + minutes * 60 + seconds


def validate_config(config: Mapping[str, Any]) -> None:
    """명세 10.5절 정합성 검사: 하나라도 실패하면 모든 문제를 모아 ConfigError로 알립니다.

    설정이 본문 안전 규칙보다 느슨한지, 값 범위가 올바른지, 폐기된 키가 남았는지,
    마감 시각 순서가 뒤집히지 않았는지를 부팅 시점에 확인합니다.
    """
    if not isinstance(config, Mapping):
        raise ConfigError("Configuration must be a mapping.")
    problems: list[str] = []

    # 1) 안전 방향 검사: 설정이 본문보다 느슨하면 실패합니다.
    must_be_false = (
        "execution.allow_market_order",
        "risk.safe_halt.allow_reduce_only_sell",
        "execution.retry_unknown_order",
    )
    for key in must_be_false:
        if _lookup(config, key) is not False:
            problems.append(f"{key} must be false.")
    for key in (
        "universe.reject_derivatives", "universe.reject_inverse",
        "universe.reject_leverage", "universe.reject_etn",
        "universe.require_verified_classification",
        "universe.forbid_name_keyword_heuristic",
    ):
        if _lookup(config, key) is not True:
            problems.append(f"{key} must be true.")
    if _lookup(config, "execution.order_type") != "LIMIT":
        problems.append("execution.order_type must be LIMIT.")
    classes = _lookup(config, "universe.allowed_risk_asset_classes")
    if classes != ["EQUITY"]:
        problems.append("universe.allowed_risk_asset_classes must be ['EQUITY'].")

    # 2) 값 범위 검사
    slots = _lookup(config, "portfolio.slot_weights")
    buffer_ratio = _lookup(config, "portfolio.cash_buffer_ratio")
    if (
        not isinstance(slots, Mapping)
        or not isinstance(buffer_ratio, (int, float))
        or sum(slots.values()) + buffer_ratio > 1.0 + 1e-9
    ):
        problems.append("slot_weights + cash_buffer_ratio must not exceed 1.0.")
    weights = _lookup(config, "momentum.weights")
    if not isinstance(weights, list) or abs(sum(weights) - 1.0) > 1e-9:
        problems.append("momentum.weights must sum to 1.0.")
    caution = _lookup(config, "risk.portfolio_drawdown.caution")
    warning = _lookup(config, "risk.portfolio_drawdown.warning")
    critical = _lookup(config, "risk.portfolio_drawdown.critical")
    if not all(isinstance(v, (int, float)) for v in (caution, warning, critical)) or not (
        0 > caution > warning > critical >= -1
    ):
        problems.append("drawdown thresholds must satisfy 0 > caution > warning > critical.")
    normal_stop = _lookup(config, "trailing_stop.normal")
    training_stop = _lookup(config, "trailing_stop.training")
    if not all(isinstance(v, (int, float)) for v in (normal_stop, training_stop)) or not (
        -1 < normal_stop < training_stop < 0
    ):
        problems.append("trailing stops must satisfy -1 < normal < training < 0.")
    floor = _lookup(config, "momentum.min_daily_excess_volatility")
    if not isinstance(floor, (int, float)) or isinstance(floor, bool) or floor <= 0:
        problems.append("momentum.min_daily_excess_volatility must be positive.")

    # 3) 폐기 키·알 수 없는 섹션 검사
    for key in _DEPRECATED_KEYS:
        if _lookup(config, key) is not None:
            problems.append(f"deprecated key present: {key}")
    for section in config:
        if section not in _KNOWN_SECTIONS:
            problems.append(f"unknown configuration section: {section}")

    # 4) 환경 값 검사
    if _lookup(config, "deployment.environment") not in ("PAPER", "REAL"):
        problems.append("deployment.environment must be PAPER or REAL.")

    # 5) 시각 순서 검사(마감 타임라인)
    previous_name, previous_seconds = None, -1
    for section, key in _TIME_ORDER:
        value = _seconds(_lookup(config, f"{section}.{key}"))
        if value is None:
            problems.append(f"{section}.{key} must be HH:MM:SS.")
            continue
        if value <= previous_seconds:
            problems.append(f"{section}.{key} must be later than {previous_name}.")
        previous_name, previous_seconds = f"{section}.{key}", value

    if problems:
        raise ConfigError("Invalid configuration: " + "; ".join(problems))


# 백테스트 승인에 묶이는 전략 설정 영역(7.1절): 이 값이 바뀌면 재승인이 필요합니다.
_STRATEGY_HASH_PATHS = (
    "strategy", "momentum", "portfolio", "trailing_stop", "universe",
    "execution.fee_rate", "execution.emergency", "risk.portfolio_drawdown",
)


def strategy_params_hash(config: Mapping[str, Any]) -> str:
    """전략 영역 설정의 정규화(키 정렬) JSON SHA-256 을 반환합니다."""
    import hashlib
    import json

    selected = {path: _lookup(config, path) for path in _STRATEGY_HASH_PATHS}
    if any(value is None for value in selected.values()):
        raise ConfigError("Strategy configuration is incomplete.")
    canonical = json.dumps(selected, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
