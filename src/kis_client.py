import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class RefreshTrigger(str, Enum):
    MORNING_PREP = "morning_prep"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class TokenRefreshPolicy:
    proactive_refresh_min_age_hours: float
    hard_guard_before_expiry_hours: float
    min_refresh_cooldown_seconds: float

    def __post_init__(self) -> None:
        values = (
            self.proactive_refresh_min_age_hours,
            self.hard_guard_before_expiry_hours,
            self.min_refresh_cooldown_seconds,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in values
        ):
            raise ValueError("Token refresh policy values must be positive numbers.")


@dataclass(frozen=True)
class CachedAccessToken:
    access_token: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.access_token, str) or not self.access_token.strip():
            raise ValueError("Access token must be a non-empty string.")

        issued_at = _as_utc(self.issued_at, "issued_at")
        expires_at = _as_utc(self.expires_at, "expires_at")
        if expires_at <= issued_at:
            raise ValueError("Token expiration must be later than issuance.")

        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)


def _as_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime.")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime.")
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return _as_utc(value, "timestamp").isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _parse_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Token cache {field_name} must be a string.")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Token cache {field_name} is not valid ISO-8601.") from exc
    return _as_utc(parsed, field_name)


class KisTokenCache:
    _FIELDS = {"access_token", "issued_at", "expires_at"}

    def __init__(
        self,
        cache_path: str | Path | None = None,
        project_root: str | Path | None = None,
    ) -> None:
        if cache_path is None:
            configured_path = os.environ.get("TOKEN_CACHE_PATH")
            cache_path = (
                Path(configured_path)
                if configured_path
                else Path.home() / ".secrets" / "mosasaurus" / "kis_token.json"
            )

        self.project_root = Path(
            project_root or Path(__file__).resolve().parent.parent
        ).expanduser().resolve()
        self.path = Path(cache_path).expanduser().resolve()
        try:
            self.path.relative_to(self.project_root)
        except ValueError:
            pass
        else:
            raise ValueError("KIS token cache must be outside the project workspace.")

    def load(self) -> CachedAccessToken | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise ValueError("KIS token cache contains invalid JSON.") from exc

        if not isinstance(payload, dict) or set(payload) != self._FIELDS:
            raise ValueError("KIS token cache has an unexpected structure.")

        return CachedAccessToken(
            access_token=payload["access_token"],
            issued_at=_parse_utc(payload["issued_at"], "issued_at"),
            expires_at=_parse_utc(payload["expires_at"], "expires_at"),
        )

    def save(self, token: CachedAccessToken) -> None:
        if not isinstance(token, CachedAccessToken):
            raise TypeError("token must be a CachedAccessToken.")

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)

        payload = {
            "access_token": token.access_token,
            "issued_at": _format_utc(token.issued_at),
            "expires_at": _format_utc(token.expires_at),
        }
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                if os.name != "nt":
                    os.chmod(temporary_path, 0o600)
                json.dump(payload, temporary_file, separators=(",", ":"))
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            os.replace(temporary_path, self.path)
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def should_refresh_access_token(
    token: CachedAccessToken | None,
    *,
    trigger: RefreshTrigger,
    now: datetime,
    policy: TokenRefreshPolicy,
    last_request_at: datetime | None,
) -> bool:
    if not isinstance(trigger, RefreshTrigger):
        raise ValueError("Unsupported token refresh trigger.")

    current_time = _as_utc(now, "now")
    if last_request_at is not None:
        last_request = _as_utc(last_request_at, "last_request_at")
        cooldown = timedelta(seconds=policy.min_refresh_cooldown_seconds)
        if current_time < last_request + cooldown:
            return False

    if token is None:
        return True

    if trigger is RefreshTrigger.MORNING_PREP:
        minimum_age = timedelta(hours=policy.proactive_refresh_min_age_hours)
        return current_time >= token.issued_at + minimum_age

    hard_guard = timedelta(hours=policy.hard_guard_before_expiry_hours)
    return current_time >= token.expires_at - hard_guard
