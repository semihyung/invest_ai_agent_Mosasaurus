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
