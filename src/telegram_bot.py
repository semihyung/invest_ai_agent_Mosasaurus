from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import sqlite3
from typing import Callable, Mapping
from uuid import uuid4

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters


CONFIRMATION_REQUIRED = {
    "/emergency",
    "/emergency off",
    "/add",
    "/remove",
}

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


class PendingActionStore:
    """DB에 2단계 확인 요청과 승인 이력을 저장합니다."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        """저장에 사용할 SQLite 연결 생성 함수를 보관합니다."""
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable.")
        self._connection_factory = connection_factory

    def create(self, action: PendingAction) -> None:
        """확인 요청을 PENDING 상태로 기록하고 Telegram 작업자를 감사 로그에 남깁니다."""
        connection = self._connection_factory()
        try:
            action_type, ticker = self._action_fields(action.command)
            stored_ticker = ticker if action_type == "UNIVERSE_REMOVE" else None
            reason = "TWO_STEP_CONFIRMATION"
            if action_type == "UNIVERSE_ADD" and ticker is not None:
                reason += f";TICKER={ticker}"
            with connection:
                connection.execute(
                    """
                    INSERT INTO pending_actions (
                        id, action_type, ticker, reason, approval_status,
                        created_at, expires_at
                    ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?)
                    """,
                    (
                        action.action_id,
                        action_type,
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
    ) -> None:
        """허용 채팅과 선택적 DB 확인 저장소를 구성합니다."""
        if not allowed_chat_ids:
            raise ValueError("At least one allowed chat ID is required.")
        if any(not isinstance(chat_id, str) or not chat_id.isdigit() for chat_id in allowed_chat_ids):
            raise ValueError("Allowed chat IDs must be numeric strings.")
        self.allowed_chat_ids = set(allowed_chat_ids)
        self.pending_store = pending_store
        self.pending: dict[str, PendingAction] = {}

    def handle(self, chat_id: str, text: str, now: datetime) -> tuple[str, PendingAction | None]:
        """화이트리스트·입력·확인 기한을 검사하고 수행 가능한 명령 상태를 반환합니다."""
        if not isinstance(chat_id, str) or not isinstance(text, str):
            raise ValueError("chat_id and text must be strings.")
        if chat_id not in self.allowed_chat_ids:
            return "UNAUTHORIZED", None
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("now must be timezone-aware.")
        normalized = " ".join(text.strip().lower().split())
        if not normalized:
            return "UNKNOWN_COMMAND", None
        command = normalized.split(" ", 1)[0]
        if command not in ALLOWED_COMMANDS:
            return "UNKNOWN_COMMAND", None
        if command == "/confirm":
            action_id = normalized.split(" ", 1)[1] if " " in normalized else ""
            if not re.fullmatch(r"[a-f0-9]{12}", action_id):
                return "CONFIRMATION_NOT_FOUND", None
            action = self.pending.get(action_id)
            if action is None or action.chat_id != chat_id:
                return "CONFIRMATION_NOT_FOUND", None
            if now.astimezone(timezone.utc) >= action.expires_at:
                del self.pending[action_id]
                if self.pending_store is not None:
                    self.pending_store.confirm(action, now=now.astimezone(timezone.utc))
                return "CONFIRMATION_EXPIRED", None
            if self.pending_store is not None and not self.pending_store.confirm(
                action,
                now=now.astimezone(timezone.utc),
            ):
                del self.pending[action_id]
                return "CONFIRMATION_EXPIRED", None
            del self.pending[action_id]
            return "CONFIRMED", action
        if not self._valid_arguments(normalized):
            return "INVALID_ARGUMENTS", None
        requires_confirmation = (
            normalized in CONFIRMATION_REQUIRED
            or normalized.startswith("/add ")
            or normalized.startswith("/remove ")
        )
        if requires_confirmation:
            action_id = uuid4().hex[:12]
            utc_now = now.astimezone(timezone.utc)
            action = PendingAction(
                action_id=action_id,
                command=normalized,
                chat_id=chat_id,
                created_at=utc_now,
                expires_at=utc_now + timedelta(minutes=5),
            )
            if self.pending_store is not None:
                self.pending_store.create(action)
            self.pending[action_id] = action
            return "CONFIRMATION_REQUIRED", action
        return "EXECUTE", None

    @staticmethod
    def _valid_arguments(command: str) -> bool:
        """Phase 1 명령마다 허용된 인자 형식만 통과시킵니다."""
        parts = command.split()
        name = parts[0]
        if name in ("/status", "/preview", "/pool", "/health", "/pause", "/stop", "/resume", "/reconcile"):
            return len(parts) == 1
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
) -> None:
    """Phase 1 Telegram polling을 시작하고 채팅·단일 프로세스 잠금을 강제합니다."""
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
        status, action = handler.handle(
            chat_id,
            message.text,
            datetime.now(timezone.utc),
        )
        if status == "UNAUTHORIZED":
            return
        if status == "CONFIRMATION_REQUIRED" and action is not None:
            await message.reply_text(
                f"확인이 필요합니다. 5분 안에 /confirm {action.action_id} 를 입력하세요."
            )
            return
        if status == "CONFIRMED" and action is not None:
            command = action.command.split(" ", 1)[0]
            reply = command_actions[command](chat_id, action.command)
        elif status == "EXECUTE":
            command = message.text.strip().split(" ", 1)[0].lower()
            if command == "/stop":
                command = "/pause"
            reply = command_actions[command](chat_id, message.text.strip())
        else:
            reply = {
                "CONFIRMATION_EXPIRED": "확인 시간이 만료되었습니다. 명령을 다시 요청하세요.",
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
