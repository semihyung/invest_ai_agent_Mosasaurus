from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class HealthSnapshot:
    database_ok: bool
    heartbeat_ok: bool
    token_valid: bool
    reconciliation_ok: bool
    cpu_percent: float
    disk_percent: float
    temperature_c: float
    checked_at: datetime


def is_healthy(snapshot: HealthSnapshot) -> bool:
    if (
        snapshot.cpu_percent < 0
        or snapshot.disk_percent < 0
        or snapshot.disk_percent > 100
        or snapshot.temperature_c < -273
    ):
        raise ValueError("Health values are outside valid ranges.")
    return (
        snapshot.database_ok
        and snapshot.heartbeat_ok
        and snapshot.token_valid
        and snapshot.reconciliation_ok
        and snapshot.cpu_percent < 95
        and snapshot.disk_percent < 90
        and snapshot.temperature_c < 85
    )
