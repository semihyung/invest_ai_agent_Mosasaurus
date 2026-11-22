import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
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
    """HTTP 전송 결과의 상태 코드, 본문과 응답 헤더입니다."""

    status_code: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


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
                return KisHttpResponse(
                    response.status,
                    response.read(),
                    dict(response.headers.items()),
                )
        except HTTPError as exc:
            error_headers = dict(exc.headers.items()) if exc.headers is not None else {}
            return KisHttpResponse(exc.code, exc.read(), error_headers)
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


@dataclass(frozen=True)
class KisWebSocketApprovalKey:
    """WebSocket 연결에만 사용하는 메모리 전용 접속키입니다."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        """빈 키와 공백이 포함된 키를 거부합니다."""
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("KIS WebSocket approval key is required.")
        if self.value != self.value.strip():
            raise ValueError("KIS WebSocket approval key has invalid whitespace.")


class KisWebSocketTokenIssuer:
    """REST 토큰 캐시와 분리된 PAPER WebSocket 접속키를 발급합니다."""

    _APPROVAL_URL = "https://openapivts.koreainvestment.com:29443/oauth2/Approval"

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        *,
        transport: KisHttpTransport | None = None,
        timeout_seconds: float = 10,
    ) -> None:
        """모의투자 WebSocket 접속키 발급용 설정을 저장합니다."""
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

    def issue(self) -> KisWebSocketApprovalKey:
        """접속키를 발급하고 응답 원문은 반환하거나 기록하지 않습니다."""
        body = json.dumps(
            {
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "secretkey": self._app_secret,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        response = self._transport.request(
            "POST",
            self._APPROVAL_URL,
            {
                "Accept": "text/plain",
                "Content-Type": "application/json",
            },
            body,
            self._timeout_seconds,
        )
        if not 200 <= response.status_code < 300:
            raise KisApiError(
                f"KIS WebSocket approval endpoint returned HTTP {response.status_code}."
            )
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KisApiError(
                "KIS WebSocket approval endpoint returned invalid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise KisApiError(
                "KIS WebSocket approval endpoint returned an unexpected response."
            )
        approval_key = payload.get("approval_key")
        if not isinstance(approval_key, str) or not approval_key.strip():
            raise KisApiError(
                "KIS WebSocket approval response is missing a valid key."
            )
        return KisWebSocketApprovalKey(approval_key)


@dataclass(frozen=True)
class KisNavTick:
    """ETF NAV 값과 로컬 수신 시각을 함께 보관합니다."""

    ticker: str
    nav: float
    observed_at: datetime
    received_at: datetime

    def __post_init__(self) -> None:
        """티커, NAV 값과 수신 시각의 기본 무결성을 확인합니다."""
        if (
            not isinstance(self.ticker, str)
            or len(self.ticker) != 6
            or not self.ticker.isascii()
            or not self.ticker.isdigit()
        ):
            raise ValueError("KIS ticker must contain exactly six digits.")
        if (
            isinstance(self.nav, bool)
            or not isinstance(self.nav, (int, float))
            or not math.isfinite(self.nav)
            or self.nav <= 0
        ):
            raise ValueError("NAV must be a positive finite number.")
        observed_at = _as_utc(self.observed_at, "observed_at")
        received_at = _as_utc(self.received_at, "received_at")
        if received_at < observed_at:
            raise ValueError("received_at must not precede observed_at.")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "received_at", received_at)


class KisWebSocketConnection(Protocol):
    """ETF 실시간 데이터 세션에 필요한 WebSocket 동작입니다."""

    def send(self, payload: str) -> Any: ...

    def recv(self) -> str | bytes: ...

    def pong(self, payload: bytes = b"") -> Any: ...

    def close(self) -> Any: ...


def _connect_kis_websocket(url: str, timeout_seconds: float) -> KisWebSocketConnection:
    """기본 WebSocket 연결을 만들며 패키지는 실제 연결 시점에만 불러옵니다."""
    import websocket

    return websocket.create_connection(
        url,
        timeout=timeout_seconds,
        enable_multithread=False,
    )


class KisEtfNavWebSocket:
    """PAPER 환경의 ETF NAV 채널을 구독하고 검증된 시세 틱을 반환합니다."""

    _URL = "ws://ops.koreainvestment.com:31000"
    _TR_ID = "H0STNAV0"

    def __init__(
        self,
        approval_key: KisWebSocketApprovalKey,
        *,
        connection_factory: Callable[
            [str, float], KisWebSocketConnection
        ] | None = None,
        timeout_seconds: float = 10,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """메모리 전용 접속키와 모의 가능한 WebSocket 연결기를 설정합니다."""
        if not isinstance(approval_key, KisWebSocketApprovalKey):
            raise TypeError("approval_key must be a KisWebSocketApprovalKey.")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("KIS WebSocket timeout must be positive.")
        self._approval_key = approval_key
        self._connection_factory = connection_factory or _connect_kis_websocket
        self._timeout_seconds = timeout_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._connection: KisWebSocketConnection | None = None
        self._ticker: str | None = None

    def connect(self) -> None:
        """PAPER WebSocket 서버에 한 번만 연결합니다."""
        if self._connection is not None:
            raise KisApiError("KIS WebSocket is already connected.")
        self._connection = self._connection_factory(self._URL, self._timeout_seconds)

    def subscribe(self, ticker: str) -> None:
        """지정한 ETF의 실시간 NAV 채널을 등록합니다."""
        self._validate_ticker(ticker)
        connection = self._require_connection()
        if self._ticker is not None:
            raise KisApiError("KIS WebSocket already has an active NAV subscription.")
        connection.send(self._subscription_message(ticker, "1"))
        self._ticker = ticker

    def unsubscribe(self) -> None:
        """현재 NAV 구독을 해제하고 활성 티커를 비웁니다."""
        if self._ticker is None:
            return
        self._require_connection().send(
            self._subscription_message(self._ticker, "2")
        )
        self._ticker = None

    def receive_nav(self, *, received_at: datetime | None = None) -> KisNavTick:
        """구독 확인·ping을 처리하고 다음 정상 NAV 메시지를 반환합니다."""
        connection = self._require_connection()
        if self._ticker is None:
            raise KisApiError("KIS ETF NAV subscription is not active.")

        while True:
            message = connection.recv()
            if isinstance(message, bytes):
                try:
                    message = message.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise KisApiError("KIS WebSocket returned invalid text data.") from exc
            if not isinstance(message, str) or not message:
                raise KisApiError("KIS WebSocket returned an empty or invalid message.")

            if message.startswith("{"):
                self._handle_system_message(message, connection)
                continue

            frame = message.split("|", 3)
            if len(frame) != 4 or frame[0] not in {"0", "1"} or frame[1] != self._TR_ID:
                raise KisApiError("KIS WebSocket returned an unexpected NAV frame.")
            try:
                record_count = int(frame[2])
            except ValueError as exc:
                raise KisApiError("KIS WebSocket returned an invalid NAV batch.") from exc
            if record_count != 1:
                raise KisApiError("KIS WebSocket returned an unsupported NAV batch.")
            values = frame[3].split("^")
            if len(values) < 2 or values[0] != self._ticker:
                raise KisApiError("KIS WebSocket NAV ticker does not match subscription.")
            try:
                nav = float(values[1])
            except ValueError as exc:
                raise KisApiError("KIS WebSocket returned an invalid NAV value.") from exc
            timestamp = _as_utc(
                received_at if received_at is not None else self._now(),
                "received_at",
            )
            return KisNavTick(
                ticker=self._ticker,
                nav=nav,
                observed_at=timestamp,
                received_at=timestamp,
            )

    def close(self) -> None:
        """WebSocket 연결을 종료하고 메모리의 구독 상태를 지웁니다."""
        connection = self._connection
        self._connection = None
        self._ticker = None
        if connection is not None:
            connection.close()

    def __enter__(self) -> "KisEtfNavWebSocket":
        """연결된 NAV 세션 컨텍스트를 시작합니다."""
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        """컨텍스트 종료 시 WebSocket 연결을 닫습니다."""
        self.close()
        return False

    @staticmethod
    def _validate_ticker(ticker: str) -> None:
        """NAV 구독 티커가 6자리 숫자인지 확인합니다."""
        if (
            not isinstance(ticker, str)
            or len(ticker) != 6
            or not ticker.isascii()
            or not ticker.isdigit()
        ):
            raise ValueError("KIS ticker must contain exactly six digits.")

    def _require_connection(self) -> KisWebSocketConnection:
        """연결 상태가 아니면 명시적 오류를 발생시킵니다."""
        if self._connection is None:
            raise KisApiError("KIS WebSocket is not connected.")
        return self._connection

    def _subscription_message(self, ticker: str, request_type: str) -> str:
        """구독 프레임을 직렬화합니다."""
        return json.dumps(
            {
                "header": {
                    "approval_key": self._approval_key.value,
                    "custtype": "P",
                    "tr_type": request_type,
                    "content-type": "utf-8",
                },
                "body": {"input": {"tr_id": self._TR_ID, "tr_key": ticker}},
            },
            separators=(",", ":"),
        )

    @staticmethod
    def _handle_system_message(
        message: str,
        connection: KisWebSocketConnection,
    ) -> None:
        """구독 응답과 서버 ping을 민감한 메시지 내용 없이 처리합니다."""
        try:
            payload = json.loads(message)
        except json.JSONDecodeError as exc:
            raise KisApiError("KIS WebSocket returned invalid JSON.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("header"), dict):
            raise KisApiError("KIS WebSocket returned an unexpected system message.")
        if payload["header"].get("tr_id") == "PINGPONG":
            connection.pong(message.encode("utf-8"))
            return
        body = payload.get("body")
        if not isinstance(body, dict) or str(body.get("rt_cd")) != "0":
            raise KisApiError("KIS WebSocket subscription was rejected.")


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
    """PAPER 환경의 시세·계좌·체결 조회 전용 KIS REST 클라이언트입니다."""

    _MAX_PAGINATION_PAGES = 20
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

    def fetch_account_balance(
        self,
        account_number: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """PAPER 계좌의 보유 종목과 잔고 요약을 모든 페이지에서 조회합니다."""
        cano, acnt_prdt_cd = self._split_account_number(account_number)
        return self._get_paginated(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            "VTTC8434R",
            {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            list_key="output1",
            summary_key="output2",
        )

    def fetch_daily_order_executions(
        self,
        account_number: str,
        trade_date: date,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """PAPER 계좌의 지정 거래일 주문·체결·미체결 내역을 모두 조회합니다."""
        cano, acnt_prdt_cd = self._split_account_number(account_number)
        if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
            raise ValueError("trade_date must be a date value.")
        date_string = trade_date.strftime("%Y%m%d")
        return self._get_paginated(
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            "VTTC0081R",
            {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "INQR_STRT_DT": date_string,
                "INQR_END_DT": date_string,
                "SLL_BUY_DVSN_CD": "00",
                "PDNO": "",
                "CCLD_DVSN": "00",
                "INQR_DVSN": "00",
                "INQR_DVSN_3": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_1": "",
                "EXCG_ID_DVSN_CD": "KRX",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            list_key="output1",
            summary_key="output2",
        )

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
        if (
            not isinstance(ticker, str)
            or len(ticker) != 6
            or not ticker.isascii()
            or not ticker.isdigit()
        ):
            raise ValueError("KIS ticker must contain exactly six digits.")

    @staticmethod
    def _split_account_number(account_number: str) -> tuple[str, str]:
        """KIS 계좌번호 10자리를 CANO 8자리와 상품코드 2자리로 분리합니다."""
        if not isinstance(account_number, str) or len(account_number) != 10:
            raise ValueError("KIS account number must contain exactly ten digits.")
        if not account_number.isascii() or not account_number.isdigit():
            raise ValueError("KIS account number must contain exactly ten digits.")
        return account_number[:8], account_number[8:]

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

    def _get_paginated(
        self,
        path: str,
        transaction_id: str,
        params: dict[str, str],
        *,
        list_key: str,
        summary_key: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """KIS 연속조회 표식을 확인하며 페이지 누락 없이 결과를 모읍니다."""
        query = dict(params)
        rows: list[dict[str, Any]] = []
        summary: dict[str, Any] = {}
        seen_cursors: set[tuple[str, str]] = set()
        continuation = ""

        for _ in range(self._MAX_PAGINATION_PAGES):
            payload, response_headers = self._get_page(
                path,
                transaction_id,
                query,
                tr_cont=continuation,
            )
            rows.extend(self._list_output(payload, list_key))
            summary = self._mapping_output(payload, summary_key)
            response_continuation = self._header_value(
                response_headers,
                "tr_cont",
            )
            if response_continuation not in {"M", "F"}:
                return rows, summary

            next_fk = payload.get(
                "ctx_area_fk100",
                payload.get("CTX_AREA_FK100"),
            )
            next_nk = payload.get(
                "ctx_area_nk100",
                payload.get("CTX_AREA_NK100"),
            )
            if (
                not isinstance(next_fk, str)
                or not isinstance(next_nk, str)
                or not next_fk
                or not next_nk
            ):
                raise KisApiError("KIS pagination response is missing its cursor.")
            cursor = (next_fk, next_nk)
            if cursor in seen_cursors:
                raise KisApiError("KIS pagination returned a repeated cursor.")
            seen_cursors.add(cursor)
            query["CTX_AREA_FK100"] = next_fk
            query["CTX_AREA_NK100"] = next_nk
            continuation = "N"

        raise KisApiError("KIS pagination exceeded the configured page limit.")

    @staticmethod
    def _header_value(headers: Mapping[str, str], name: str) -> str:
        """대소문자를 구분하지 않고 응답 헤더 값을 안전하게 찾습니다."""
        for header_name, value in headers.items():
            if (
                isinstance(header_name, str)
                and header_name.lower().replace("-", "_")
                == name.lower().replace("-", "_")
            ):
                return value.upper() if isinstance(value, str) else ""
        return ""

    def _get(
        self,
        path: str,
        transaction_id: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        """KIS GET 응답의 JSON 본문을 검증해 반환합니다."""
        payload, _ = self._get_page(
            path,
            transaction_id,
            params,
            tr_cont="",
        )
        return payload

    def _get_page(
        self,
        path: str,
        transaction_id: str,
        params: dict[str, str],
        *,
        tr_cont: str,
    ) -> tuple[dict[str, Any], Mapping[str, str]]:
        """KIS GET 한 페이지를 호출하고 검증된 본문과 응답 헤더를 반환합니다."""
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
                "tr_cont": tr_cont,
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
        if not isinstance(response.headers, Mapping):
            raise KisApiError("KIS API returned invalid response headers.")
        return payload, response.headers
