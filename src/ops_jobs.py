"""운영 보조 잡: Telegram 알림 전송기, 18:05 일일 브리핑, 일요일 03:00 DB 백업(명세 9.1절).

- 알림 전송기는 표준 라이브러리 HTTP 만 사용하고, 실패해도 매매 잡을 막지 않습니다(알림은 `system_events` 에도 남음).
- 메시지는 `sanitize_telegram_reply` 로 계좌번호·토큰 형태를 가린 뒤에만 보냅니다.
- 백업은 SQLite Online Backup API 로 일관된 사본을 만들고 `integrity_check` 로 검증합니다.
  암호화·원격 복제는 호스트 스크립트(`scripts/backup_db.sh`)의 몫입니다.
"""

import json
import sqlite3
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

from src.safety_flags import raise_flag
from src.telegram_bot import sanitize_telegram_reply, telegram_credentials

KST = timezone(timedelta(hours=9), name="KST")
PREFIX = "[모사사우르스]"


def _urllib_post_json(url: str, payload: Mapping[str, str]) -> None:
    """Telegram Bot API 로 JSON 을 한 번 POST 한다(타임아웃 10초)."""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers={"content-type": "application/json"}, method="POST")
    urllib.request.urlopen(request, timeout=10).close()  # noqa: S310 - 고정된 https Telegram 호스트


def build_notifier(
    environ: Mapping[str, str],
    *,
    post: Callable[[str, Mapping[str, str]], None] = _urllib_post_json,
) -> Callable[[str, str], None]:
    """`(심각도, 메시지)` → Telegram. 토큰·채팅 ID 가 없으면 아무것도 보내지 않는 함수를 돌려준다."""
    token, chat_id = telegram_credentials(environ)
    if not token or not chat_id:
        return lambda severity, message: None
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def notify(severity: str, message: str) -> bool:
        """메시지를 보내고 성공 여부를 돌려준다(실패 세부는 URL 의 토큰이 섞일 수 있어 예외로 퍼뜨리지 않는다)."""
        text = sanitize_telegram_reply(f"{PREFIX} [{severity}] {message}")
        try:
            post(url, {"chat_id": chat_id, "text": text})
            return True
        except Exception:  # noqa: BLE001
            return False

    return notify


def xirr(flows: list[tuple[date, float]]) -> float | None:
    """불규칙 현금흐름의 연환산 내부수익률(이분법). 부호가 한쪽뿐이거나 기간이 0이면 None."""
    if len(flows) < 2 or not (any(v < 0 for _, v in flows) and any(v > 0 for _, v in flows)):
        return None
    first = min(d for d, _ in flows)
    npv = lambda rate: sum(v / (1.0 + rate) ** ((d - first).days / 365.0) for d, v in flows)
    low, high = -0.99, 100.0
    if npv(low) * npv(high) > 0:
        return None
    for _ in range(200):
        mid = (low + high) / 2.0
        if npv(low) * npv(mid) <= 0:
            high = mid
        else:
            low = mid
    return (low + high) / 2.0


def performance_summary(connection: sqlite3.Connection) -> str:
    """현재 성과 기간(epoch)의 MDD 와 XIRR 요약(스냅샷이 2개 미만이면 계산 불가)."""
    rows = connection.execute("SELECT snapshot_date, total_equity, flow_adjusted_unit_nav FROM daily_portfolio "
                              "WHERE performance_epoch = (SELECT MAX(performance_epoch) FROM daily_portfolio) "
                              "ORDER BY snapshot_date").fetchall()
    navs = [r["flow_adjusted_unit_nav"] for r in rows if r["flow_adjusted_unit_nav"]]
    if len(navs) < 2:
        return "MDD/XIRR 계산에 필요한 스냅샷이 부족합니다."
    peak, mdd = navs[0], 0.0
    for nav in navs:
        peak = max(peak, nav)
        mdd = min(mdd, nav / peak - 1.0)
    # 투자자 관점 현금흐름: 최초 평가액·입금은 음수, 출금과 현재 평가액은 양수
    flows = [(date.fromisoformat(rows[0]["snapshot_date"]), -float(rows[0]["total_equity"]))]
    for f in connection.execute("SELECT flow_date, amount FROM cash_flows WHERE flow_type IN ('DEPOSIT','WITHDRAWAL') "
                                "AND classification_status = 'CONFIRMED' AND flow_date > ? ORDER BY flow_date",
                                (rows[0]["snapshot_date"],)):
        flows.append((date.fromisoformat(f["flow_date"]), -float(f["amount"])))
    flows.append((date.fromisoformat(rows[-1]["snapshot_date"]), float(rows[-1]["total_equity"])))
    rate = xirr(flows)
    return f"MDD {mdd * 100:.2f}% / XIRR " + (f"{rate * 100:+.2f}%" if rate is not None else "계산 불가")


def compose_briefing(
    connection: sqlite3.Connection,
    now: datetime,
    *,
    token_expiry: datetime | None = None,
    api_calls_today: int | None = None,
) -> str:
    """DB 상태를 한 장의 브리핑 문장으로 만든다(숫자·상태만, 계좌 식별자 없음)."""
    snap = connection.execute("SELECT * FROM daily_portfolio ORDER BY snapshot_date DESC LIMIT 1").fetchone()
    count = lambda sql: connection.execute(sql).fetchone()[0]
    flags = connection.execute("SELECT flag_type, code, ticker FROM safety_flags WHERE cleared_at IS NULL "
                               "ORDER BY flag_type, code").fetchall()
    halts = [r["code"] for r in flags if r["flag_type"] == "HALT"]
    blocks = [r["code"] + (f"({r['ticker']})" if r["ticker"] else "") for r in flags if r["flag_type"] != "HALT"]
    lines = [f"일일 브리핑 {now.astimezone(KST).date().isoformat()}"]
    if snap:
        lines.append(f"총자산 {snap['total_equity']:,}원 / TWR {snap['twr_cumulative'] * 100:+.2f}% / "
                     f"낙폭 단계 {snap['drawdown_stage']} / 스냅샷 {snap['snapshot_date']}")
    else:
        lines.append("일간 스냅샷 없음(성과 지수 미초기화)")
    lines.append("SAFE_HALT 원인: " + (", ".join(halts) if halts else "없음"))
    lines.append("매수 차단·종목 격리: " + (", ".join(blocks) if blocks else "없음"))
    open_obligations = count("SELECT COUNT(*) FROM exit_obligations WHERE status = 'OPEN'")
    unclassified = count("SELECT COUNT(*) FROM cash_flows WHERE classification_status IN "
                         "('PROVISIONAL', 'UNCLASSIFIED_CASH_FLOW')")
    unverified = count("SELECT COUNT(*) FROM corporate_actions WHERE verified = 0")
    held = count("SELECT COUNT(*) FROM positions WHERE quantity > 0")
    slots = connection.execute("SELECT slot_id, ticker FROM portfolio_slots ORDER BY slot_id").fetchall()
    slot_text = ", ".join(f"{r['slot_id']}={r['ticker'] or '파킹'}" for r in slots) or "미배정"
    lines.append(f"보유 종목 {held}개 / 열린 매도 의무 {open_obligations}건 / 슬롯 {slot_text}")
    lines.append(performance_summary(connection))
    status = []
    if token_expiry is not None:
        status.append("토큰 만료 " + token_expiry.astimezone(KST).strftime("%m-%d %H:%M"))
    if api_calls_today is not None:
        status.append(f"오늘 KIS API 호출 {api_calls_today}회")
    if status:
        lines.append(" / ".join(status))
    lines.append(f"미분류 현금흐름 {unclassified}건 / 미검증 기업행위 {unverified}건")
    return "\n".join(lines)


def create_daily_briefing_handler(
    connection_factory: Callable[[], sqlite3.Connection],
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    notify: Callable[[str, str], None] = lambda severity, message: None,
    token_expiry: Callable[[], datetime | None] = lambda: None,
    api_calls_today: Callable[[], int | None] = lambda: None,
) -> Callable[[], None]:
    """18:05 브리핑을 만들어 `system_events` 에 기록하고 Telegram 으로 보낸다."""

    def handler() -> None:
        """18:05 브리핑을 만들어 기록하고 발송한다."""
        now = clock()
        connection = connection_factory()
        try:
            text = compose_briefing(connection, now, token_expiry=token_expiry(), api_calls_today=api_calls_today())
            with connection:
                connection.execute(
                    "INSERT INTO system_events (component, event_type, severity, message, created_at) "
                    "VALUES ('ops_jobs', 'DAILY_BRIEFING', 'INFO', ?, ?)", (text, now.isoformat()))
            notify("INFO", text)
        finally:
            connection.close()

    return handler


_WEEKLY_BACKUP_PATTERN = "mosasaurus-????????.db"             # 이 잡이 만드는 주간 백업(KST 날짜)
_PREDEPLOY_BACKUP_PATTERN = "mosasaurus-????????T??????Z.db"  # scripts/backup_db.sh 의 배포 전 백업(UTC 시각)


def prune_backups(directory: Path, pattern: str, keep: int) -> None:
    """`pattern` 에 맞는 백업을 이름순(= 시간순)으로 `keep` 개만 남기고, 지운 파일의 `.sha256` 도 함께 지운다.

    주간 백업과 배포 전 백업은 따로 센다: 한꺼번에 이름순으로 세면 배포가 잦은 달에 주간 백업이 먼저 밀려나고
    체크섬 파일은 고아로 남는다(감사 12번).
    """
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("keep must be a positive integer.")
    for old in sorted(directory.glob(pattern))[:-keep]:
        old.unlink()
        old.with_name(old.name + ".sha256").unlink(missing_ok=True)


def create_weekly_maintenance_handler(
    connection_factory: Callable[[], sqlite3.Connection],
    backup_dir: str | Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    notify: Callable[[str, str], None] = lambda severity, message: None,
    keep: int = 8,
    keep_predeploy: int = 10,
) -> Callable[[], None]:
    """SQLite 일관 백업 → 무결성 검사 → 오래된 사본 정리. 검사 실패는 `DB_INTEGRITY` 정지 원인으로 올린다.

    `keep`(주간 백업)·`keep_predeploy`(배포 전 백업) 는 `rules.yaml` 의 `backup.keep_weekly`·`backup.keep_predeploy` 이다.
    """

    def handler() -> None:
        """일요일 DB 백업과 무결성 검사를 수행한다."""
        now = clock()
        target_dir = Path(backup_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"mosasaurus-{now.astimezone(KST).strftime('%Y%m%d')}.db"
        connection = connection_factory()
        try:
            destination = sqlite3.connect(target)
            try:
                connection.backup(destination)
                verdict = destination.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                destination.close()
            if verdict != "ok":
                target.unlink(missing_ok=True)
                raise_flag(connection, "HALT", "DB_INTEGRITY", now=now, detail="BACKUP_INTEGRITY_CHECK_FAILED")
                notify("CRITICAL", "주간 백업 무결성 검사 실패: DB_INTEGRITY 정지")
                return
            prune_backups(target_dir, _WEEKLY_BACKUP_PATTERN, keep)
            prune_backups(target_dir, _PREDEPLOY_BACKUP_PATTERN, keep_predeploy)
            with connection:
                connection.execute(
                    "INSERT INTO system_events (component, event_type, severity, message, created_at) "
                    "VALUES ('ops_jobs', 'WEEKLY_BACKUP_OK', 'INFO', ?, ?)", (f"백업 완료: {target.name}", now.isoformat()))
        finally:
            connection.close()

    return handler
