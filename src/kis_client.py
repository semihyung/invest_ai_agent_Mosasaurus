import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


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


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """외부 보안 디렉터리에 JSON을 권한 제한과 원자적 교체로 저장합니다."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            if os.name != "nt":
                os.chmod(temporary_path, 0o600)
            json.dump(payload, temporary_file, separators=(",", ":"))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_path, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


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
                else Path.home() / ".secrets" / "mosasaurus" / "token-cache" / "token.json"
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

        payload = {
            "access_token": token.access_token,
            "issued_at": _format_utc(token.issued_at),
            "expires_at": _format_utc(token.expires_at),
        }
        _write_private_json(self.path, payload)


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


class KisEnvironment(str, Enum):
    PAPER = "PAPER"
    REAL = "REAL"


class KisApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class KisHttpResponse:
    status_code: int
    body: bytes


class KisHttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> KisHttpResponse: ...


class UrllibKisTransport:
    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> KisHttpResponse:
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return KisHttpResponse(response.status, response.read())
        except HTTPError as exc:
            return KisHttpResponse(exc.code, exc.read())
        except (URLError, TimeoutError, OSError) as exc:
            raise KisApiError("KIS API transport failed.") from exc


class KisTokenIssuer:
    """모의투자 OAuth 엔드포인트에서 REST 접근 토큰을 발급합니다."""

    _TOKEN_URL = "https://openapivts.koreainvestment.com:29443/oauth2/tokenP"

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        *,
        transport: KisHttpTransport | None = None,
        timeout_seconds: float = 10,
    ) -> None:
        """PAPER 환경 발급에 필요한 자격 증명과 전송기를 설정합니다."""
        if not isinstance(app_key, str) or not app_key.strip():
            raise ValueError("KIS app key is required.")
        if not isinstance(app_secret, str) or not app_secret.strip():
            raise ValueError("KIS app secret is required.")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("KIS timeout must be positive.")

        self._app_key = app_key
        self._app_secret = app_secret
        self._transport = transport or UrllibKisTransport()
        self._timeout_seconds = timeout_seconds

    def issue(self, *, now: datetime) -> CachedAccessToken:
        """토큰 API를 호출해 토큰과 만료 메타데이터만 반환합니다."""
        issued_at = _as_utc(now, "now")
        request_body = json.dumps(
            {
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "appsecret": self._app_secret,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        response = self._transport.request(
            "POST",
            self._TOKEN_URL,
            {
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
            },
            request_body,
            self._timeout_seconds,
        )
        if not 200 <= response.status_code < 300:
            raise KisApiError(
                f"KIS token endpoint returned HTTP {response.status_code}."
            )
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KisApiError("KIS token endpoint returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise KisApiError("KIS token endpoint returned an unexpected response.")

        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, str) and expires_in.isdigit():
            expires_in = int(expires_in)
        if (
            not isinstance(access_token, str)
            or not access_token.strip()
            or access_token != access_token.strip()
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or expires_in <= 0
        ):
            raise KisApiError("KIS token endpoint response is missing valid token data.")

        return CachedAccessToken(
            access_token=access_token,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=expires_in),
        )


class KisTokenRefreshStateStore:
    """마지막 토큰 발급 시각만 토큰 캐시와 같은 외부 금고에 저장합니다."""

    _FIELDS = {"last_request_at"}

    def __init__(
        self,
        path: str | Path,
        *,
        project_root: str | Path | None = None,
    ) -> None:
        self.project_root = Path(
            project_root or Path(__file__).resolve().parent.parent
        ).expanduser().resolve()
        self.path = Path(path).expanduser().resolve()
        try:
            self.path.relative_to(self.project_root)
        except ValueError:
            pass
        else:
            raise ValueError("KIS refresh metadata must be outside the project workspace.")

    def load_last_request_at(self) -> datetime | None:
        """저장된 마지막 발급 시각을 읽고 잘못된 메타데이터는 거부합니다."""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise ValueError("KIS refresh metadata contains invalid JSON.") from exc
        if not isinstance(payload, dict) or set(payload) != self._FIELDS:
            raise ValueError("KIS refresh metadata has an unexpected structure.")
        return _parse_utc(payload["last_request_at"], "last_request_at")

    def save_last_request_at(self, requested_at: datetime) -> None:
        """요청 시각을 토큰 없이 권한 제한·원자적 교체로 저장합니다."""
        _write_private_json(
            self.path,
            {"last_request_at": _format_utc(requested_at)},
        )


class KisTokenRefreshLock:
    """여러 호스트 프로세스가 동시에 토큰 발급을 요청하지 못하게 합니다."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle: Any | None = None

    def __enter__(self) -> "KisTokenRefreshLock":
        """외부 보안 디렉터리에서 비차단 프로세스 잠금을 획득합니다."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        handle = self.path.open("a+b")
        try:
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        except OSError:
            handle.close()
            raise
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise KisApiError("Another KIS token refresh is already in progress.") from exc
        self._handle = handle
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        """프로세스 잠금을 해제하고 파일 핸들을 닫습니다."""
        if self._handle is None:
            return False
        handle = self._handle
        self._handle = None
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        return False


@dataclass(frozen=True)
class TokenRefreshResult:
    """호스트 갱신기의 처리 결과를 비밀값 없이 보관합니다."""

    action: str
    expires_at: datetime | None


class KisTokenRefreshService:
    """쿨다운 정책을 적용하고 발급된 토큰을 외부 캐시에 원자적으로 저장합니다."""

    def __init__(
        self,
        token_cache: KisTokenCache,
        issuer: KisTokenIssuer,
        policy: TokenRefreshPolicy,
        *,
        state_store: KisTokenRefreshStateStore | None = None,
    ) -> None:
        if not isinstance(token_cache, KisTokenCache):
            raise TypeError("token_cache must be a KisTokenCache.")
        if not isinstance(policy, TokenRefreshPolicy):
            raise TypeError("policy must be a TokenRefreshPolicy.")
        if not callable(getattr(issuer, "issue", None)):
            raise TypeError("issuer must provide an issue method.")
        if state_store is not None and not isinstance(
            state_store, KisTokenRefreshStateStore
        ):
            raise TypeError("state_store must be a KisTokenRefreshStateStore.")

        self._token_cache = token_cache
        self._issuer = issuer
        self._policy = policy
        self._state_store = state_store or KisTokenRefreshStateStore(
            token_cache.path.with_name("refresh-state.json"),
            project_root=token_cache.project_root,
        )

    def refresh_if_needed(
        self,
        *,
        trigger: RefreshTrigger,
        now: datetime,
    ) -> TokenRefreshResult:
        """정기·복구 정책 및 발급 쿨다운에 따라 토큰을 재사용하거나 교체합니다."""
        current_time = _as_utc(now, "now")
        lock_path = self._state_store.path.with_suffix(".lock")
        with KisTokenRefreshLock(lock_path):
            token = self._token_cache.load()
            last_request_at = self._state_store.load_last_request_at()
            refresh_due = should_refresh_access_token(
                token,
                trigger=trigger,
                now=current_time,
                policy=self._policy,
                last_request_at=None,
            )
            if not refresh_due:
                return TokenRefreshResult(
                    action="reuse",
                    expires_at=token.expires_at if token is not None else None,
                )
            if not should_refresh_access_token(
                token,
                trigger=trigger,
                now=current_time,
                policy=self._policy,
                last_request_at=last_request_at,
            ):
                raise KisApiError("KIS token refresh is blocked by the 65-second cooldown.")

            self._state_store.save_last_request_at(current_time)
            refreshed_token = self._issuer.issue(now=current_time)
            self._token_cache.save(refreshed_token)
            return TokenRefreshResult(
                action="refreshed",
                expires_at=refreshed_token.expires_at,
            )


class KisReadOnlyClient:
    _BASE_URLS = {
        KisEnvironment.PAPER: "https://openapivts.koreainvestment.com:29443",
        KisEnvironment.REAL: "https://openapi.koreainvestment.com:9443",
    }

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        token_cache: KisTokenCache,
        *,
        environment: KisEnvironment = KisEnvironment.PAPER,
        transport: KisHttpTransport | None = None,
        timeout_seconds: float = 10,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(app_key, str) or not app_key.strip():
            raise ValueError("KIS app key is required.")
        if not isinstance(app_secret, str) or not app_secret.strip():
            raise ValueError("KIS app secret is required.")
        if not isinstance(environment, KisEnvironment):
            raise ValueError("Unsupported KIS environment.")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("KIS timeout must be positive.")
        if not isinstance(token_cache, KisTokenCache):
            raise TypeError("token_cache must be a KisTokenCache.")
        if environment is KisEnvironment.REAL:
            raise ValueError(
                "REAL KIS access is disabled until token-cache environment binding is implemented."
            )

        self._app_key = app_key
        self._app_secret = app_secret
        self._environment = environment
        self._token_cache = token_cache
        self._transport = transport or UrllibKisTransport()
        self._timeout_seconds = timeout_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._holiday_cache: dict[date, bool] = {}

    def fetch_etf_quote(self, ticker: str) -> dict[str, Any]:
        self._validate_ticker(ticker)
        payload = self._get(
            "/uapi/etfetn/v1/quotations/inquire-price",
            "FHPST02400000",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
            },
        )
        return self._mapping_output(payload, "output")

    def fetch_orderbook(self, ticker: str) -> dict[str, Any]:
        self._validate_ticker(ticker)
        payload = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            "FHKST01010200",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
            },
        )
        return self._mapping_output(payload, "output1")

    def fetch_daily_candles(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
        *,
        adjusted: bool,
    ) -> list[dict[str, Any]]:
        self._validate_ticker(ticker)
        if (
            not isinstance(start_date, date)
            or isinstance(start_date, datetime)
            or not isinstance(end_date, date)
            or isinstance(end_date, datetime)
        ):
            raise ValueError("Daily candle dates must be date values.")
        if start_date > end_date:
            raise ValueError("start_date must not be later than end_date.")
        if not isinstance(adjusted, bool):
            raise ValueError("adjusted must be a boolean.")

        records: list[dict[str, Any]] = []
        current_start = start_date
        while current_start <= end_date:
            current_end = min(current_start + timedelta(days=89), end_date)
            payload = self._get(
                "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                "FHKST03010100",
                {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": ticker,
                    "FID_INPUT_DATE_1": current_start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": current_end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "0" if adjusted else "1",
                },
            )
            records.extend(self._list_output(payload, "output2"))
            current_start = current_end + timedelta(days=1)
        return records

    def is_open_day(self, trading_date: date) -> bool:
        if not isinstance(trading_date, date) or isinstance(trading_date, datetime):
            raise ValueError("trading_date must be a date value.")
        if trading_date in self._holiday_cache:
            return self._holiday_cache[trading_date]
        payload = self._get(
            "/uapi/domestic-stock/v1/quotations/chk-holiday",
            "CTCA0903R",
            {"BASS_DT": trading_date.strftime("%Y%m%d")},
        )
        holiday_output = payload.get("output")
        if isinstance(holiday_output, dict):
            rows = [holiday_output]
        elif isinstance(holiday_output, list) and all(
            isinstance(row, dict) for row in holiday_output
        ):
            rows = holiday_output
        else:
            raise KisApiError("KIS response is missing holiday output.")
        for row in rows:
            if row.get("bass_dt") == trading_date.strftime("%Y%m%d"):
                open_flag = row.get("opnd_yn")
                if open_flag in ("Y", "N"):
                    is_open = open_flag == "Y"
                    self._holiday_cache[trading_date] = is_open
                    return is_open
        raise KisApiError("KIS holiday response is missing the requested date.")

    @staticmethod
    def _validate_ticker(ticker: str) -> None:
        if not isinstance(ticker, str) or len(ticker) != 6 or not ticker.isdigit():
            raise ValueError("KIS ticker must contain exactly six digits.")

    @staticmethod
    def _mapping_output(payload: dict[str, Any], key: str) -> dict[str, Any]:
        value = payload.get(key)
        if not isinstance(value, dict):
            raise KisApiError(f"KIS response is missing object output '{key}'.")
        return value

    @staticmethod
    def _list_output(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
        value = payload.get(key)
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise KisApiError(f"KIS response is missing list output '{key}'.")
        return value

    def _get(
        self,
        path: str,
        transaction_id: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        token = self._token_cache.load()
        if token is None or _as_utc(self._now(), "now") >= token.expires_at:
            raise KisApiError("KIS access token is unavailable or expired.")

        url = f"{self._BASE_URLS[self._environment]}{path}?{urlencode(params)}"
        response = self._transport.request(
            "GET",
            url,
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {token.access_token}",
                "appkey": self._app_key,
                "appsecret": self._app_secret,
                "custtype": "P",
                "tr_id": transaction_id,
            },
            None,
            self._timeout_seconds,
        )
        if not 200 <= response.status_code < 300:
            raise KisApiError(f"KIS API returned HTTP {response.status_code}.")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KisApiError("KIS API returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise KisApiError("KIS API returned an unexpected response shape.")
        if str(payload.get("rt_cd")) != "0":
            error_code = payload.get("msg_cd")
            safe_code = error_code if isinstance(error_code, str) else "unknown"
            raise KisApiError(f"KIS API rejected the request (code={safe_code}).")
        return payload
