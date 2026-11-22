from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4


CONFIRMATION_REQUIRED = {
    "/emergency",
    "/emergency off",
    "/add",
    "/remove",
    "/override",
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


def mask_secret(value: str) -> str:
    if not isinstance(value, str):
        return "****"
    if len(value) <= 2:
        return "****"
    return value[:2] + "*" * 4


@dataclass(frozen=True)
class PendingAction:
    action_id: str
    command: str
    chat_id: str
    created_at: datetime
    expires_at: datetime


class TelegramCommandHandler:
    def __init__(self, allowed_chat_ids: set[str]) -> None:
        if not allowed_chat_ids:
            raise ValueError("At least one allowed chat ID is required.")
        self.allowed_chat_ids = set(allowed_chat_ids)
        self.pending: dict[str, PendingAction] = {}

    def handle(self, chat_id: str, text: str, now: datetime) -> tuple[str, PendingAction | None]:
        if chat_id not in self.allowed_chat_ids:
            return "UNAUTHORIZED", None
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware.")
        normalized = " ".join(text.strip().lower().split())
        command = normalized.split(" ", 1)[0]
        if command not in ALLOWED_COMMANDS:
            return "UNKNOWN_COMMAND", None
        if command == "/confirm":
            action_id = normalized.split(" ", 1)[1] if " " in normalized else ""
            action = self.pending.get(action_id)
            if action is None or action.chat_id != chat_id:
                return "CONFIRMATION_NOT_FOUND", None
            if now.astimezone(timezone.utc) >= action.expires_at:
                del self.pending[action_id]
                return "CONFIRMATION_EXPIRED", None
            del self.pending[action_id]
            return "CONFIRMED", action
        requires_confirmation = (
            normalized in CONFIRMATION_REQUIRED
            or normalized.startswith("/add ")
            or normalized.startswith("/remove ")
            or normalized.startswith("/override ")
            or normalized == "/emergency"
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
            self.pending[action_id] = action
            return "CONFIRMATION_REQUIRED", action
        return "EXECUTE", None
