import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import sqlite3
from typing import Callable, Mapping
from uuid import uuid4

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from src.universe import (
    ProductMetadata,
    product_metadata_fingerprint,
    validate_product_metadata,
)


# 2단계 확인이 필요한 명령(명세 6.4절): 위험 노출을 늘리거나 상태를 해제·변경하는 명령은 모두 포함.
CONFIRMATION_REQUIRED = {
    "/emergency",
    "/emergency off",
    "/resume",
    "/training off",
    "/reconcile adopt",
}
CONFIRMATION_PREFIXES = (
    "/add ", "/remove ", "/training on", "/clear_halt ", "/classify ",
    "/ca_confirm ", "/approve_real ", "/approve_smoke ", "/approve_backtest ",
)
CONFIRM_CODE_LENGTH = 8
CONFIRM_MAX_FAILURES = 5
CONFIRM_LOCKOUT = timedelta(seconds=1800)
MAX_COMMANDS_PER_MINUTE = 10
_CODE_PATTERN = re.compile(r"[a-z2-7]{8}")
_ID_PATTERN = re.compile(r"[a-z0-9_-]{4,64}")

ALLOWED_COMMANDS = {
    "/status",
    "/preview",
    "/pool",
    "/trades",
    "/health",
    "/training",
    "/pause",
    "/stop",
    "/resume",
    "/emergency",
    "/reconcile",
    "/add",
    "/remove",
    "/confirm",
    "/clear_halt",
    "/classify",
    "/ca_confirm",
    "/approve_real",
    "/approve_smoke",
    "/approve_backtest",
}

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(access_token|app_key|app_secret|api_key|token|account(?:_no|_number)?)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_LONG_SECRET = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")
_LONG_NUMBER = re.compile(r"(?<!\d)\d{8,16}(?!\d)")


def mask_secret(value: str) -> str:
    """민감 식별값의 앞부분만 남기고 나머지를 가립니다."""
    if not isinstance(value, str):
        return "****"
    if len(value) <= 2:
        return "****"
    return value[:2] + "*" * 4


def sanitize_telegram_reply(value: str) -> str:
    """명령 콜백의 답변에서 토큰·계좌번호로 보이는 문자열을 마스킹합니다."""
    if not isinstance(value, str):
        raise TypeError("Telegram replies must be strings.")
    sanitized = _SECRET_ASSIGNMENT.sub(r"\1\2****", value)
    sanitized = _LONG_SECRET.sub("****", sanitized)
    return _LONG_NUMBER.sub(lambda match: "****" + match.group(0)[-4:], sanitized)[:4096]


def load_telegram_settings(
    environ: Mapping[str, str],
) -> tuple[str, set[str]]:
    """환경 변수에서 봇 토큰과 숫자형 허용 채팅 ID만 읽습니다."""
    if not isinstance(environ, Mapping):
        raise TypeError("environ must be a mapping.")
    token = environ.get("TELEGRAM_BOT_TOKEN")
    raw_chat_ids = environ.get("ALLOWED_CHAT_ID")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("TELEGRAM_BOT_TOKEN is required.")
    if not isinstance(raw_chat_ids, str) or not raw_chat_ids.strip():
        raise ValueError("ALLOWED_CHAT_ID is required.")
    allowed_chat_ids = {
        chat_id.strip() for chat_id in raw_chat_ids.split(",") if chat_id.strip()
    }
    if not allowed_chat_ids or any(not chat_id.isdigit() for chat_id in allowed_chat_ids):
        raise ValueError("ALLOWED_CHAT_ID must contain numeric IDs.")
    return token, allowed_chat_ids


@dataclass(frozen=True)
class PendingAction:
    """5분 안에 확인해야 하는 제어 요청입니다."""

    action_id: str
    command: str
    chat_id: str
    created_at: datetime
    expires_at: datetime
    product_metadata: ProductMetadata | None = None
    confirmation_summary: str | None = None
    confirm_code: str | None = None  # 사용자에게 한 번만 보여 주는 평문 코드(DB 에는 해시만 저장)
    user_id: str | None = None


def new_confirm_code() -> str:
    """암호학적 난수로 8자 Base32 확인 코드를 만듭니다(혼동 문자 없는 A-Z, 2-7)."""
    return base64.b32encode(secrets.token_bytes(5)).decode("ascii")[:CONFIRM_CODE_LENGTH]


def hash_confirm_code(code: str) -> str:
    """확인 코드를 대소문자 구분 없이 SHA-256 으로 해시합니다."""
    return hashlib.sha256(code.upper().encode("ascii")).hexdigest()


class PendingActionStore:
    """DB에 2단계 확인 요청과 승인 이력을 저장합니다."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        """저장에 사용할 SQLite 연결 생성 함수를 보관합니다."""
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable.")
        self._connection_factory = connection_factory

    def contains_asset(self, ticker: str) -> bool:
        """티커가 이미 DB 유니버스에 있는지 확인합니다."""
        connection = self._connection_factory()
        try:
            return connection.execute(
                "SELECT 1 FROM universe_assets WHERE ticker = ?",
                (ticker,),
            ).fetchone() is not None
        finally:
            connection.close()

    def create(self, action: PendingAction) -> None:
        """확인 요청을 PENDING 상태로 기록하고 Telegram 작업자를 감사 로그에 남깁니다."""
        connection = self._connection_factory()
        try:
            action_type, ticker = self._action_fields(action.command)
            if action_type == "UNIVERSE_ADD":
                if action.product_metadata is None or ticker is None:
                    raise ValueError("/add 승인에는 검증된 공식 메타데이터가 필요합니다.")
                validate_product_metadata(
                    action.product_metadata,
                    expected_ticker=ticker,
                )
            stored_ticker = ticker if action_type == "UNIVERSE_REMOVE" else None
            reason = "TWO_STEP_CONFIRMATION"
            if action_type == "UNIVERSE_ADD" and ticker is not None:
                reason += (
                    f";TICKER={ticker};METADATA_SHA256="
                    f"{product_metadata_fingerprint(action.product_metadata, category='RISK_ASSET')}"
                )
            with connection:
                connection.execute(
                    """
                    INSERT INTO pending_actions (
                        id, action_type, args_hash, code_hash, ticker, reason,
                        approval_status, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                    """,
                    (
                        action.action_id,
                        action_type,
                        hashlib.sha256(action.command.encode("utf-8")).hexdigest(),
                        hash_confirm_code(action.confirm_code or action.action_id),
                        stored_ticker,
                        reason,
                        action.created_at.isoformat(),
                        action.expires_at.isoformat(),
                    ),
                )
                self._audit(
                    connection,
                    chat_id=action.chat_id,
                    action="PENDING_ACTION_CREATED",
                    action_id=action.action_id,
                    created_at=action.created_at,
                )
        finally:
            connection.close()

    def confirm(self, action: PendingAction, *, now: datetime) -> bool:
        """만료 전 PENDING 요청만 승인하고 조건부 변경 결과를 반환합니다."""
        connection = self._connection_factory()
        try:
            action_type, ticker = self._action_fields(action.command)
            if action_type == "UNIVERSE_ADD":
                if action.product_metadata is None or ticker is None:
                    return False
                validate_product_metadata(
                    action.product_metadata,
                    expected_ticker=ticker,
                )
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE pending_actions
                    SET approval_status = 'APPROVED', confirmed_at = ?
                    WHERE id = ? AND approval_status = 'PENDING' AND expires_at > ?
                    """,
                    (now.isoformat(), action.action_id, now.isoformat()),
                )
                if cursor.rowcount == 1:
                    if action_type == "UNIVERSE_ADD":
                        metadata = action.product_metadata
                        if metadata is None:
                            raise RuntimeError("확인된 공식 상품 메타데이터가 없습니다.")
                        connection.execute(
                            """
                            INSERT INTO universe_assets (
                                ticker, name, category, region, underlying_index,
                                product_type, replication_method, leverage_multiplier,
                                is_inverse, is_futures_based, classification_verified,
                                classification_source, verified_by, verified_at,
                                status, data_status, blocked_until_next_rotation, added_at
                            ) VALUES (?, ?, 'RISK_ASSET', ?, ?, ?, ?, ?, ?, ?, 1,
                                      ?, ?, ?, 'ACTIVE', 'VALID', 0, ?)
                            """,
                            (
                                metadata.ticker,
                                metadata.name,
                                metadata.region,
                                metadata.underlying_index,
                                metadata.product_type,
                                metadata.replication_method,
                                metadata.leverage_multiplier,
                                int(metadata.is_inverse),
                                int(metadata.is_futures_based),
                                metadata.classification_source,
                                f"telegram:{mask_secret(action.chat_id)}",
                                now.isoformat(),
                                now.isoformat(),
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE pending_actions SET ticker = ?
                            WHERE id = ? AND action_type = 'UNIVERSE_ADD'
                            """,
                            (ticker, action.action_id),
                        )
                    self._audit(
                        connection,
                        chat_id=action.chat_id,
                        action="PENDING_ACTION_APPROVED",
                        action_id=action.action_id,
                        created_at=now,
                    )
                    return True
                connection.execute(
                    """
                    UPDATE pending_actions SET approval_status = 'EXPIRED'
                    WHERE id = ? AND approval_status = 'PENDING' AND expires_at <= ?
                    """,
                    (action.action_id, now.isoformat()),
                )
                return False
        finally:
            connection.close()

    def expire_pending_on_restart(self, *, now: datetime) -> int:
        """프로세스 재시작으로 채팅 소유권을 복원할 수 없는 요청을 만료 처리합니다."""
        connection = self._connection_factory()
        try:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE pending_actions SET approval_status = 'EXPIRED'
                    WHERE approval_status = 'PENDING'
                    """
                )
                if cursor.rowcount:
                    self._audit(
                        connection,
                        chat_id="system",
                        action="PENDING_ACTIONS_EXPIRED_ON_RESTART",
                        action_id="all",
                        created_at=now,
                    )
                return cursor.rowcount
        finally:
            connection.close()

    @staticmethod
    def _action_fields(command: str) -> tuple[str, str | None]:
        """사용자 입력을 저장 가능한 고정 작업 유형과 티커로 제한합니다."""
        parts = command.split()
        if parts[0] == "/emergency":
            return ("EMERGENCY_OFF" if len(parts) > 1 else "EMERGENCY_ON", None)
        if parts[0] in ("/add", "/remove") and len(parts) == 2:
            return ("UNIVERSE_ADD" if parts[0] == "/add" else "UNIVERSE_REMOVE", parts[1])
        fixed = {
            "/resume": "RESUME",
            "/clear_halt": "CLEAR_HALT",
            "/classify": "CLASSIFY_FLOW",
            "/ca_confirm": "CA_CONFIRM",
            "/approve_real": "APPROVE_REAL",
            "/approve_smoke": "APPROVE_SMOKE",
            "/approve_backtest": "APPROVE_BACKTEST",
        }
        if parts[0] in fixed:
            return (fixed[parts[0]], None)
        if parts[0] == "/training":
            return ("TRAINING_OFF" if parts[1] == "off" else "TRAINING_ON", None)
        if parts[0] == "/reconcile" and parts[1:] == ["adopt"]:
            return ("RECONCILE_ADOPT", None)
        raise ValueError("Unsupported confirmation action.")

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        chat_id: str,
        action: str,
        action_id: str,
        created_at: datetime,
    ) -> None:
        """감사 레코드에는 마스킹된 채팅 식별자와 비밀 없는 상태만 저장합니다."""
        connection.execute(
            """
            INSERT INTO audit_events (
                actor, action, target, diff_sanitized, correlation_id, created_at
            ) VALUES (?, ?, ?, '{}', ?, ?)
            """,
            (
                f"telegram:{mask_secret(chat_id)}",
                action,
                action_id,
                action_id,
                created_at.isoformat(),
            ),
        )


class TelegramCommandHandler:
    """화이트리스트 확인과 Phase 1 명령 파싱을 수행합니다."""

    def __init__(
        self,
        allowed_chat_ids: set[str],
        *,
        pending_store: PendingActionStore | None = None,
        product_metadata_lookup: Callable[[str], ProductMetadata] | None = None,
        allowed_user_ids: set[str] | None = None,
        summary_provider: Callable[[str], str | None] | None = None,
    ) -> None:
        """허용 채팅(과 선택적 허용 사용자)·DB 확인 저장소를 구성합니다."""
        self.summary_provider = summary_provider
        if not allowed_chat_ids:
            raise ValueError("At least one allowed chat ID is required.")
        if any(not isinstance(chat_id, str) or not chat_id.isdigit() for chat_id in allowed_chat_ids):
            raise ValueError("Allowed chat IDs must be numeric strings.")
        self.allowed_chat_ids = set(allowed_chat_ids)
        self.pending_store = pending_store
        if product_metadata_lookup is not None and not callable(product_metadata_lookup):
            raise TypeError("product_metadata_lookup must be callable.")
        self.product_metadata_lookup = product_metadata_lookup
        if allowed_user_ids is not None and (
            not allowed_user_ids
            or any(not isinstance(item, str) or not item.isdigit() for item in allowed_user_ids)
        ):
            raise ValueError("Allowed user IDs must be non-empty numeric strings.")
        self.allowed_user_ids = None if allowed_user_ids is None else set(allowed_user_ids)
        self.pending: dict[str, PendingAction] = {}  # 키 = 확인 코드 해시
        self._recent_commands: list[datetime] = []
        self._failed_confirmations = 0
        self._locked_until: datetime | None = None

    def handle(
        self,
        chat_id: str,
        text: str,
        now: datetime,
        *,
        user_id: str | None = None,
    ) -> tuple[str, PendingAction | None]:
        """채팅·사용자 화이트리스트, 속도 제한, 입력, 확인 기한을 검사하고 명령 상태를 반환합니다."""
        if not isinstance(chat_id, str) or not isinstance(text, str):
            raise ValueError("chat_id and text must be strings.")
        if chat_id not in self.allowed_chat_ids:
            return "UNAUTHORIZED", None
        if self.allowed_user_ids is not None and user_id not in self.allowed_user_ids:
            return "UNAUTHORIZED", None
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("now must be timezone-aware.")
        # 분당 명령 수 제한(슬라이딩 윈도우): 초과분은 무응답 취급이 아니라 명시적으로 거부합니다.
        window_start = now.astimezone(timezone.utc) - timedelta(minutes=1)
        self._recent_commands = [t for t in self._recent_commands if t > window_start]
        if len(self._recent_commands) >= MAX_COMMANDS_PER_MINUTE:
            return "RATE_LIMITED", None
        self._recent_commands.append(now.astimezone(timezone.utc))
        normalized = " ".join(text.strip().lower().split())
        if not normalized:
            return "UNKNOWN_COMMAND", None
        command = normalized.split(" ", 1)[0]
        if command not in ALLOWED_COMMANDS:
            return "UNKNOWN_COMMAND", None
        utc_now = now.astimezone(timezone.utc)
        if command == "/confirm":
            if self._locked_until is not None and utc_now < self._locked_until:
                return "CONFIRMATIONS_LOCKED", None
            code = normalized.split(" ", 1)[1] if " " in normalized else ""
            action = (
                self.pending.get(hash_confirm_code(code))
                if _CODE_PATTERN.fullmatch(code)
                else None
            )
            if (
                action is None
                or action.chat_id != chat_id
                or (self.allowed_user_ids is not None and action.user_id != user_id)
            ):
                # 잘못된 코드가 연속 5회 쌓이면 모든 2단계 명령을 30분 잠급니다.
                self._failed_confirmations += 1
                if self._failed_confirmations >= CONFIRM_MAX_FAILURES:
                    self._locked_until = utc_now + CONFIRM_LOCKOUT
                    self._failed_confirmations = 0
                return "CONFIRMATION_NOT_FOUND", None
            key = hash_confirm_code(code)
            if utc_now >= action.expires_at:
                del self.pending[key]
                if self.pending_store is not None:
                    self.pending_store.confirm(action, now=utc_now)
                return "CONFIRMATION_EXPIRED", None
            if self.pending_store is not None and not self.pending_store.confirm(
                action,
                now=utc_now,
            ):
                del self.pending[key]
                return "CONFIRMATION_EXPIRED", None
            del self.pending[key]  # 단일 사용: 같은 코드는 다시 쓸 수 없습니다.
            self._failed_confirmations = 0
            return "CONFIRMED", action
        if self._locked_until is not None and utc_now < self._locked_until and (
            normalized in CONFIRMATION_REQUIRED
            or normalized.startswith(CONFIRMATION_PREFIXES)
        ):
            return "CONFIRMATIONS_LOCKED", None
        if not self._valid_arguments(normalized):
            return "INVALID_ARGUMENTS", None
        requires_confirmation = (
            normalized in CONFIRMATION_REQUIRED
            or normalized.startswith(CONFIRMATION_PREFIXES)
        )
        if requires_confirmation:
            metadata = None
            summary = None
            if command == "/add":
                ticker = normalized.split()[1]
                if self.pending_store is None or self.product_metadata_lookup is None:
                    return "CLASSIFICATION_UNAVAILABLE", None
                if self.pending_store.contains_asset(ticker):
                    return "TICKER_ALREADY_EXISTS", None
                metadata = self.product_metadata_lookup(ticker)
                try:
                    validate_product_metadata(
                        metadata,
                        expected_ticker=ticker,
                    )
                except ValueError:
                    return "INELIGIBLE_PRODUCT", None
                summary = (
                    f"공식 상품: {metadata.name} ({metadata.ticker}), "
                    f"유형 {metadata.product_type}, 배수 {metadata.leverage_multiplier}, "
                    f"인버스 {int(metadata.is_inverse)}, "
                    f"선물형 {int(metadata.is_futures_based)}, "
                    f"기초지수 {metadata.underlying_index}, "
                    f"복제 {metadata.replication_method}, "
                    f"출처 {metadata.classification_source}"
                )
            if summary is None and self.summary_provider is not None:
                try:  # 6.4절: 확인 메시지에 실행될 효과(즉시 스탑 도달 종목, 해제될 원인 등)를 표시한다
                    summary = self.summary_provider(normalized)
                except Exception:  # noqa: BLE001 - 요약 실패가 확인 절차를 막지 않는다
                    summary = None
            action_id = uuid4().hex[:12]
            code = new_confirm_code()
            action = PendingAction(
                action_id=action_id,
                command=normalized,
                chat_id=chat_id,
                created_at=utc_now,
                expires_at=utc_now + timedelta(minutes=5),
                product_metadata=metadata,
                confirmation_summary=summary,
                confirm_code=code,
                user_id=user_id,
            )
            if self.pending_store is not None:
                self.pending_store.create(action)
            self.pending[hash_confirm_code(code)] = action
            return "CONFIRMATION_REQUIRED", action
        return "EXECUTE", None

    @staticmethod
    def _valid_arguments(command: str) -> bool:
        """Phase 1 명령마다 허용된 인자 형식만 통과시킵니다."""
        parts = command.split()
        name = parts[0]
        if name == "/reconcile":
            return len(parts) == 1 or parts[1:] == ["adopt"]
        if name in ("/status", "/preview", "/pool", "/health", "/pause", "/stop", "/resume"):
            return len(parts) == 1
        if name == "/clear_halt":
            return len(parts) == 2 and re.fullmatch(r"[a-z_]{3,40}", parts[1]) is not None
        if name == "/classify":
            return (
                len(parts) == 3
                and parts[1].isdigit()
                and parts[2] in ("deposit", "withdrawal", "distribution", "adjustment", "dismiss")
            )
        if name == "/ca_confirm":
            return len(parts) == 2 and parts[1].isdigit()
        if name in ("/approve_real", "/approve_smoke"):
            return len(parts) == 2 and _ID_PATTERN.fullmatch(parts[1]) is not None
        if name == "/approve_backtest":
            return (
                len(parts) == 3
                and _ID_PATTERN.fullmatch(parts[1]) is not None
                and re.fullmatch(r"[0-9a-f]{64}", parts[2]) is not None
            )
        if name == "/trades":
            return len(parts) == 1 or (
                len(parts) == 2 and parts[1].isdigit() and 1 <= int(parts[1]) <= 50
            )
        if name == "/training":
            return (
                len(parts) == 2 and parts[1] == "off"
            ) or (
                len(parts) in (2, 3)
                and parts[1] == "on"
                and (len(parts) == 2 or (parts[2].isdigit() and 1 <= int(parts[2]) <= 365))
            )
        if name == "/emergency":
            return len(parts) == 1 or (len(parts) == 2 and parts[1] == "off")
        if name in ("/add", "/remove"):
            return len(parts) == 2 and re.fullmatch(r"\d{6}", parts[1]) is not None
        return False


def _record_telegram_event(
    connection_factory: Callable[[], sqlite3.Connection],
    *,
    event_type: str,
) -> None:
    """Telegram 운영 이벤트를 비밀값 없이 DB에 저장합니다."""
    connection = connection_factory()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO system_events (
                    component, event_type, severity, message, created_at
                ) VALUES ('telegram', ?, 'WARN', ?, ?)
                """,
                (
                    event_type,
                    "Telegram 처리 중 오류가 발생했습니다. 로컬 감사 이벤트를 확인하세요.",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    finally:
        connection.close()


def _acquire_polling_lock(
    connection_factory: Callable[[], sqlite3.Connection],
    *,
    owner_id: str,
) -> None:
    """SQLite 쓰기 잠금으로 단일 Telegram polling 프로세스만 허용합니다."""
    connection = connection_factory()
    try:
        with connection:
            row = connection.execute(
                'SELECT value FROM system_state WHERE "key" = ?',
                ("telegram_polling_lock",),
            ).fetchone()
            if row is not None and row[0] != "false":
                raise RuntimeError("Telegram polling is already locked.")
            connection.execute(
                """
                INSERT INTO system_state ("key", value, updated_at)
                VALUES ('telegram_polling_lock', ?, ?)
                ON CONFLICT("key") DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at
                """,
                (owner_id, datetime.now(timezone.utc).isoformat()),
            )
    finally:
        connection.close()


def _release_polling_lock(
    connection_factory: Callable[[], sqlite3.Connection],
    *,
    owner_id: str,
) -> None:
    """소유자가 현재 프로세스인 경우에만 polling 잠금을 해제합니다."""
    connection = connection_factory()
    try:
        with connection:
            connection.execute(
                """
                UPDATE system_state SET value = 'false', updated_at = ?
                WHERE "key" = 'telegram_polling_lock' AND value = ?
                """,
                (datetime.now(timezone.utc).isoformat(), owner_id),
            )
    finally:
        connection.close()


def run_telegram_polling(
    *,
    token: str,
    allowed_chat_ids: set[str],
    pending_store: PendingActionStore,
    command_actions: Mapping[str, Callable[[str, str], str]],
    connection_factory: Callable[[], sqlite3.Connection],
    product_metadata_lookup: Callable[[str], ProductMetadata] | None = None,
    allowed_user_ids: set[str] | None = None,
    summary_provider: Callable[[str], str | None] | None = None,
) -> None:
    """Phase 1 Telegram polling을 시작하고 채팅·사용자·단일 프로세스 잠금을 강제합니다."""
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Telegram bot token is required.")
    if not callable(connection_factory):
        raise TypeError("connection_factory must be callable.")
    required_actions = ALLOWED_COMMANDS - {"/confirm"}
    if set(command_actions) != required_actions or any(
        not callable(action) for action in command_actions.values()
    ):
        raise ValueError("A callable action is required for every Phase 1 command.")
    handler = TelegramCommandHandler(
        allowed_chat_ids,
        pending_store=pending_store,
        product_metadata_lookup=product_metadata_lookup,
        allowed_user_ids=allowed_user_ids,
        summary_provider=summary_provider,
    )
    owner_id = uuid4().hex
    _acquire_polling_lock(connection_factory, owner_id=owner_id)
    pending_store.expire_pending_on_restart(now=datetime.now(timezone.utc))
    application = Application.builder().token(token).build()

    async def dispatch(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        """화이트리스트 통과 후에만 명령 콜백을 실행하고 안전한 답변을 보냅니다."""
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None or not isinstance(message.text, str):
            return
        chat_id = str(chat.id)
        user = update.effective_user
        status, action = handler.handle(
            chat_id,
            message.text,
            datetime.now(timezone.utc),
            user_id=str(user.id) if user is not None else None,
        )
        if status == "UNAUTHORIZED":
            return
        if status == "CONFIRMATION_REQUIRED" and action is not None:
            details = (
                f"\n{action.confirmation_summary}"
                if action.confirmation_summary is not None
                else ""
            )
            await message.reply_text(
                sanitize_telegram_reply(
                    f"확인이 필요합니다. 5분 안에 /confirm {action.confirm_code} 를 입력하세요."
                    f"{details}"
                )
            )
            return
        if status == "CONFIRMED" and action is not None:
            command = action.command.split(" ", 1)[0]
            if command == "/add":
                if action.product_metadata is None:
                    raise RuntimeError("확인된 공식 상품 메타데이터가 없습니다.")
                reply = (
                    f"{action.product_metadata.ticker} 종목을 공식 분류와 "
                    "운영자 승인 이력에 따라 유니버스에 추가했습니다."
                )
            else:
                reply = command_actions[command](chat_id, action.command)
        elif status == "EXECUTE":
            command = message.text.strip().split(" ", 1)[0].lower()
            if command == "/stop":
                command = "/pause"
            reply = command_actions[command](chat_id, message.text.strip())
        else:
            reply = {
                "CONFIRMATION_EXPIRED": "확인 시간이 만료되었습니다. 명령을 다시 요청하세요.",
                "CONFIRMATIONS_LOCKED": "확인 코드를 연속으로 잘못 입력해 30분간 2단계 명령이 잠겼습니다.",
                "RATE_LIMITED": "명령이 너무 잦습니다. 잠시 후 다시 시도하세요.",
                "CLASSIFICATION_UNAVAILABLE": (
                    "공식 상품정보 조회 경로가 구성되지 않아 종목 추가를 차단했습니다."
                ),
                "INELIGIBLE_PRODUCT": (
                    "공식 메타데이터가 현물·비레버리지·비인버스·비선물 기준을 통과하지 못했습니다."
                ),
                "TICKER_ALREADY_EXISTS": "이미 유니버스에 등록된 티커입니다.",
                "CONFIRMATION_NOT_FOUND": "확인 요청을 찾을 수 없거나 다른 채팅에서 생성되었습니다.",
                "INVALID_ARGUMENTS": "명령 인자를 확인하세요.",
                "UNKNOWN_COMMAND": "지원하지 않는 명령입니다.",
            }.get(status, "명령을 처리할 수 없습니다.")
        await message.reply_text(sanitize_telegram_reply(reply))

    async def record_error(
        _update: object,
        _context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """오류 내용을 노출하지 않고 고정 이벤트만 로컬 DB에 남깁니다."""
        _record_telegram_event(
            connection_factory,
            event_type="TELEGRAM_HANDLER_FAILED",
        )

    application.add_handler(MessageHandler(filters.TEXT, dispatch))
    application.add_error_handler(record_error)
    try:
        application.run_polling()
    finally:
        _release_polling_lock(connection_factory, owner_id=owner_id)
